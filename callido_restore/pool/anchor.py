"""Anchor-first pool mapping: locate every tagged record's first chunk in the bucket indexes (tag = md5 of the first K
chunk md5s + their total length), derive bucket owners from anchors, then place the untagged remainder by chunk counts."""
import os, json, hashlib, collections, time, bisect
from callido_restore.core.pool import SET_OF_WRITER, bucket_index_entries
from callido_restore.pool.poolmap2 import TAGK

def dump_json(path, obj):
    """Write JSON atomically (tmp file + os.replace): a crash never leaves a half-written map behind."""
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f: json.dump(obj, f)
    os.replace(tmp, path)

MAX_WORKERS = int(os.environ.get("HBK_SCAN_WORKERS", "4"))        # H9: never more than 4 scan processes
WORKER_RSS_MB = int(os.environ.get("HBK_SCAN_WORKER_RSS_MB", "1024"))  # a worker that grows past this aborts the scan
PARENT_RSS_MB = int(os.environ.get("HBK_SCAN_RSS_MB", "6144"))         # the parent (holds `writers`) likewise

def _rss_mb():
    import resource, sys
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / (1 << 20) if sys.platform == "darwin" else r / 1024

def build_tag_index(writers):
    """(K, tail) -> {tag: [(w, rec idx), ...]}. Identical files share one tag, so a tag may name thousands of records:
    hits are collected per *tag* and the records of a tag share one position list (memory stays linear; the old
    per-record cross product reached 26M entries for 97k records and blew the RAM on the full scan)."""
    idx = {}; ntag = 0
    for w, recs in writers.items():
        for r in recs:
            if r.tag and r.n > 0:
                K = min(r.n + r.nd, TAGK)
                if r.head_new < K: continue            # leading chunks include dedup refs: tag would match elsewhere
                idx.setdefault((K, r.tail), {}).setdefault(r.tag, []).append((w, r.idx)); ntag += 1
    return idx, ntag

def _scan_entries(b, ents, keys, hits, totals=None):
    """Scan one bucket's index entries; keys: (K, tail) -> set(tags); hits: tag -> [(b, i, K)]; totals (optional):
    bucket -> ulen sum, what the placer's byte checks need. Returns md5 checks."""
    ps = [0]
    for e in ents: ps.append(ps[-1] + e[2])
    if totals is not None: totals[b] = ps[-1]
    n = len(ents); checks = 0
    for i in range(n):
        base = ps[i]
        for K in range(1, TAGK + 1):
            if i + K > n: break
            tags = keys.get((K, ps[i + K] - base))
            if not tags: continue
            h = hashlib.md5(b"".join(e[3][:16] for e in ents[i:i + K])).hexdigest(); checks += 1
            if h in tags: hits.setdefault(h, []).append((b, i, K))
    return checks

def _worker_main(argv):
    """Child entry (`python -m callido_restore.pool.anchor --worker <job.pkl> <out.pkl>`): job = (root, names, keys, buckets); names[b] is
    the full path of bucket b's index when root is "" (the scan resolves them through the source), else a name under root/Pool/0/<d>/. A plain
    subprocess, not multiprocessing: a spawned Pool worker re-runs the caller's main module, which re-loaded the whole
    `writers` table (GBs) in every worker last time."""
    import pickle
    with open(argv[0], "rb") as f: root, names, keys, bl = pickle.load(f)
    import sys
    hits = {}; checks = 0; since = 0; totals = {}
    for b in bl:
        checks += _scan_entries(b, bucket_index_entries(names[b] if not root else os.path.join(root, f"Pool/0/{b // 2048}/{names[b]}")), keys, hits, totals)
        if _rss_mb() > WORKER_RSS_MB: raise MemoryError(f"scan worker RSS {_rss_mb():.0f} MB > {WORKER_RSS_MB} MB at bucket {b}")
        since += 1
        if since >= 50: print(f"progress {since}", file=sys.stderr, flush=True); since = 0      # the parent sums these into "scanned i/N"
    if since: print(f"progress {since}", file=sys.stderr, flush=True)
    with open(argv[1], "wb") as f: pickle.dump((hits, checks, totals), f)

def _worker_command(jp, op):
    """(argv, kwargs) for one scan worker: the frozen app binary with --scan-worker (app/main.py dispatches it to _worker_main),
    or from a checkout the module with -m, the folder holding callido_restore/ on PYTHONPATH so its imports resolve."""
    import sys
    if getattr(sys, "frozen", False): return [sys.executable, "--scan-worker", jp, op], {}
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env = dict(os.environ, PYTHONPATH=root + os.pathsep + os.environ.get("PYTHONPATH", ""))
    return [sys.executable, "-m", "callido_restore.pool.anchor", "--worker", jp, op], {"env": env}

def _run_worker(job, tmpdir, n, progress=None):
    """Run one scan worker; ``progress(k)`` is called for every "progress k" line it prints (buckets scanned since the last one)."""
    import pickle, subprocess
    jp = os.path.join(tmpdir, f"job{n}.pkl"); op = os.path.join(tmpdir, f"out{n}.pkl")
    with open(jp, "wb") as f: pickle.dump(job, f)
    argv, kw = _worker_command(jp, op)
    p = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True, **kw)
    tail = ""
    for line in p.stderr:
        line = line.rstrip("\n")
        if line.startswith("progress "):
            if progress:
                try: progress(int(line.split()[1]))
                except (ValueError, IndexError): pass
        elif line.strip(): tail = line
    p.wait()
    if p.returncode: raise MemoryError(f"scan worker {n} failed: {tail or p.returncode}")
    with open(op, "rb") as f: return pickle.load(f)

def _bucket_names(listing, max_bucket):
    names = {}; counts = {}
    for e in listing:
        if e["t"] == "index" and (e["b"] not in names or e["rev"] > names[e["b"]][1]): names[e["b"]] = (e["name"], e["rev"]); counts[e["b"]] = (e["size"] - 64) // 32
    names1 = {b: v[0] for b, v in names.items()}
    return names1, counts, sorted(b for b in counts if max_bucket is None or b <= max_bucket)

def bucket_revs(listing):
    """bucket -> revision of its newest index file: the placer's clock (bucket ids are reused, revisions only grow)."""
    revs = {}
    for e in listing:
        if e.get("t") == "index" and e.get("rev", 0) > revs.get(e["b"], -1): revs[e["b"]] = e.get("rev", 0)
    return revs

def expand_hits(idx, hits):
    """tag positions -> anchors {(w, idx): positions}; all records of a tag reference the *same* sorted list."""
    anchors = {}; multi = 0
    for tagmap in idx.values():
        for tag, recs in tagmap.items():
            pos = hits.get(tag)
            if not pos: continue
            pos.sort()
            if len(pos) > 1: multi += len(recs)
            for k in recs: anchors[k] = pos
    return anchors, multi

def save_anchors(outdir, idx, hits, totals=None):
    """anchors.json: {"positions": {tag: [[b, i, K], ...]}, "records": {"w:ri": tag}, "ubytes": {bucket: ulen total}}
    (linear size; see load_anchors)."""
    recs = {f"{w}:{ri}": tag for tagmap in idx.values() for tag, rl in tagmap.items() if tag in hits for (w, ri) in rl}
    dump_json(os.path.join(outdir, "anchors.json"), {"positions": hits, "records": recs, "ubytes": {str(b): v for b, v in (totals or {}).items()}})

def load_bucket_totals(outdir):
    """bucket -> ulen total from anchors.json ({} for a scan that predates them; the placer then reads the indexes)."""
    with open(os.path.join(outdir, "anchors.json")) as f: d = json.load(f)
    return {int(b): v for b, v in d.get("ubytes", {}).items()}

def load_anchors(outdir):
    with open(os.path.join(outdir, "anchors.json")) as f: d = json.load(f)
    pos = {t: sorted(tuple(h) for h in v) for t, v in d["positions"].items()}
    out = {}
    for k, t in d["records"].items():
        w, ri = k.split(":"); out[(int(w), int(ri))] = pos[t]
    return out

def scan_anchors(source, outdir, listing, writers, log=print, max_bucket=None, workers=None):
    """Locate every tagged record's first chunk in the bucket indexes. workers=1 scans in-process; otherwise at most
    MAX_WORKERS subprocesses (`python -m callido_restore.pool.anchor --worker`), each holding only the (K, tail) -> tags map and reading
    one bucket index at a time (< 1 MB). The parent keeps hits per tag; see build_tag_index.
    Returns (anchors {(w, idx): [(bucket, entry, K), ...]} with shared lists per tag, counts {bucket: entries})."""
    idx, ntag = build_tag_index(writers)
    keys = {k: set(tm) for k, tm in idx.items()}
    names1, counts, buckets = _bucket_names(listing, max_bucket)
    # Where each bucket index really is: the app cache or a read-only root (source.local may not even exist for a backup
    # read over a remote); ensure() returns that path, downloading only what is missing.
    root = ""
    names1 = {b: str(source.ensure(f"Pool/0/{b // 2048}/{names1[b]}")) for b in buckets}
    workers = max(1, min(workers or MAX_WORKERS, MAX_WORKERS, os.cpu_count() or 1))     # frozen too: the app binary is the worker
    log(f"anchor scan (x{workers}): {ntag:,} tagged records, {sum(len(v) for v in idx.values()):,} tags, {len(idx):,} keys, {len(buckets):,} buckets, rss {_rss_mb():.0f} MB")
    t0 = time.time(); hits = {}; checks = 0; totals = {}
    def merge(part):
        for t, v in part.items(): hits.setdefault(t, []).extend(v)
    last_t = time.time()
    if workers == 1:
        for n_done, b in enumerate(buckets):
            checks += _scan_entries(b, bucket_index_entries(names1[b]), keys, hits, totals)
            if (n_done + 1) % 500 == 0 or time.time() - last_t >= 2: last_t = time.time(); log(f"  scanned {n_done + 1}/{len(buckets)} buckets, tags hit={len(hits):,}, md5 checks={checks:,}, {time.time() - t0:.0f}s")
            if _rss_mb() > PARENT_RSS_MB: raise MemoryError(f"anchor scan RSS {_rss_mb():.0f} MB > {PARENT_RSS_MB} MB; aborted")
    else:
        import concurrent.futures, tempfile
        chunks = [c for c in (buckets[i::workers * 4] for i in range(workers * 4)) if c]      # interleaved so progress is even
        import threading
        done_n = [0]; lock = threading.Lock(); last = [time.time()]
        def progress(k):                               # from the worker reader threads: one "scanned i/N" line every 2 s
            with lock:
                done_n[0] += k
                if time.time() - last[0] >= 2 or done_n[0] >= len(buckets):
                    last[0] = time.time(); log(f"  scanned {done_n[0]}/{len(buckets)} buckets, tags hit={len(hits):,}, {time.time() - t0:.0f}s")
        with tempfile.TemporaryDirectory(prefix="hbk-scan-") as td, concurrent.futures.ThreadPoolExecutor(workers) as ex:
            futs = [ex.submit(_run_worker, (root, names1, keys, c), td, n, progress) for n, c in enumerate(chunks)]
            try:
                for done, fu in enumerate(concurrent.futures.as_completed(futs), 1):
                    part, c, tot = fu.result(); merge(part); checks += c; totals.update(tot)
                    log(f"  {done}/{len(chunks)} parts, tags hit={len(hits):,}, md5 checks={checks:,}, rss {_rss_mb():.0f} MB, {time.time() - t0:.0f}s")
                    if _rss_mb() > PARENT_RSS_MB: raise MemoryError(f"anchor scan RSS {_rss_mb():.0f} MB > {PARENT_RSS_MB} MB; aborted")
            except BaseException:
                for fu in futs: fu.cancel()
                raise
    anchors, multi = expand_hits(idx, hits)
    npos = sum(len(v) for v in hits.values())
    log(f"anchor scan done: {len(anchors):,} records anchored of {ntag:,} tagged ({len(hits):,} tags, {npos:,} positions); {multi:,} with multiple hits; rss {_rss_mb():.0f} MB; {time.time() - t0:.0f}s")
    save_anchors(outdir, idx, hits, totals)      # positions, records and the bucket ulen totals the placer reads back
    return anchors, counts

def scan_anchors_parallel(source, outdir, listing, writers, log=print, max_bucket=None, workers=None):
    """Kept for callers of the old name; same as scan_anchors (capped at MAX_WORKERS)."""
    return scan_anchors(source, outdir, listing, writers, log=log, max_bucket=max_bucket, workers=workers or MAX_WORKERS)

def _filter_hits(anchors, bset):
    """{(w, ri): [hits in bset]} keeping one filtered list per shared input list (records of a tag share it)."""
    memo = {}; out = {}
    for k, v in anchors.items():
        f = memo.get(id(v))
        if f is None: f = memo[id(v)] = sorted(h for h in v if h[0] in bset)
        out[k] = f
    return out

def _hits_between(hl, lo, hi):
    """hits (sorted by bucket) with lo <= bucket <= hi."""
    return hl[bisect.bisect_left(hl, (lo,)):bisect.bisect_right(hl, (hi, 1 << 62, 1 << 62))]

def rhythm(b): return ((b - 20) // 4) % 4

def place_from_anchors(writers, anchors, counts, log=print, max_bucket=None):
    """anchors: {(w, idx): [(bucket, entry, K), ...]}. Returns (owner dict bucket->w, placements {w: {idx: [(b, e, n)]}}, report)."""
    buckets = sorted(b for b in counts if max_bucket is None or b <= max_bucket); bset = set(buckets); bpos = {b: i for i, b in enumerate(buckets)}
    # 1. owners from unique anchors
    votes = collections.defaultdict(collections.Counter); fh = _filter_hits(anchors, bset)
    for (w, ri), hits in fh.items():
        if len(hits) == 1: votes[hits[0][0]][w] += 1
    owner = {}; conflicts = []
    for b, c in votes.items():
        w, n = c.most_common(1)[0]
        if len(c) > 1: conflicts.append((b, dict(c)))
        owner[b] = w
    log(f"owners from anchors: {len(owner):,} of {len(buckets):,} buckets, conflicts={len(conflicts)}")
    # 2. per writer: anchored records in storage order
    report = collections.Counter(); placements = {w: {} for w in range(4)}
    for w in range(4):
        recs = writers[w]
        seq = sorted((h[0][0], h[0][1], ri) for (ww, ri), h in fh.items() if ww == w and len(h) == 1)
        log(f"writer{w}: {len(seq):,} unique anchors")
        for k in range(len(seq)):
            bA, eA, ri = seq[k]; r = recs[ri]; need = r.n
            if k + 1 < len(seq): bB, eB, _ = seq[k + 1]
            else: bB, eB = None, None
            # walk: A's bucket tail, then mid buckets, then B's head
            segs = []; cur_b = bA; cur_e = eA; remaining = need; gap = 0
            mids = buckets[bpos[bA] + 1: bpos[bB]] if bB is not None else buckets[bpos[bA] + 1:]
            owned_mid = [b for b in mids if owner.get(b) == w]; free_mid = [b for b in mids if b not in owner]
            avail_fixed = (counts[bA] - eA) + sum(counts[b] for b in owned_mid) + (eB if bB is not None and bB != bA else 0)
            if bB == bA: avail_fixed = eB - eA
            extra = need - avail_fixed
            chosen_free = []
            if extra > 0 and free_mid:
                # choose free buckets (prefer rhythm) summing to exactly `extra` (allow a trailing untagged file gap later)
                pref = sorted(free_mid, key=lambda b: (0 if rhythm(b) == w else 1, b))
                tot = 0
                for b in pref:
                    if tot + counts[b] <= extra: chosen_free.append(b); tot += counts[b]
                    if tot == extra: break
                if tot != extra: report["unresolved-gap"] += 1
                else:
                    for b in chosen_free: owner[b] = w
                    report["free-assigned"] += len(chosen_free)
            elif extra > 0: report["short"] += 1
            elif extra < 0: gap = -extra; report["gap-untagged-after"] += 1
            # emit placement segments by walking owned buckets in order
            path = [bA] + sorted(owned_mid + chosen_free) + ([bB] if bB is not None and bB != bA else [])
            e = eA
            for b in path:
                take = min(remaining, (eB if b == bB else counts[b]) - e)     # never past the next anchor's start
                if take > 0: segs.append((b, e, take)); remaining -= take
                e = 0
                if remaining == 0: break
            if remaining == 0: placements[w][ri] = segs; report["placed"] += 1
            else: report["unplaced"] += 1
    log(f"placement: {dict(report)}; owners now {len(owner):,}")
    return owner, placements, report

def resolve_and_place(writers, anchors, counts, log=print, max_bucket=None):
    """Full phase 2: unique anchors -> owners; multi-hit records resolved by stream neighbours; gaps between consecutive
    anchors of a writer filled with its untagged records (by chunk count); free buckets assigned by count. Returns
    (owner, placements {w: {idx: [(b,e,n)]}}, report)."""
    buckets = sorted(b for b in counts if max_bucket is None or b <= max_bucket); bset = set(buckets); bpos = {b: i for i, b in enumerate(buckets)}
    hits = _filter_hits(anchors, bset)
    fixed = {k: v[0] for k, v in hits.items() if len(v) == 1}
    report = collections.Counter(); report["unique"] = len(fixed); report["multi"] = sum(1 for v in hits.values() if len(v) > 1)
    def owners_from(fixed):
        votes = collections.defaultdict(collections.Counter)
        for (w, ri), (b, e, K) in fixed.items(): votes[b][w] += 1
        return {b: c.most_common(1)[0][0] for b, c in votes.items()}, sum(1 for c in votes.values() if len(c) > 1)
    owner, conf = owners_from(fixed); report["owner-conflicts"] = conf
    # Progress every 2 s through the two long loops below (the UI shows the line; the job maps it to 95-100 %).
    tick_t = [time.time()]
    def tick(msg):
        if time.time() - tick_t[0] >= 2: tick_t[0] = time.time(); log(msg)
    # --- resolve multi-hit records by neighbours (iterate)
    for it in range(3):
        per_w = {w: sorted((ri, h[0]) for (ww, ri), h in fixed.items() if ww == w) for w in range(4)}
        idxs = {w: [x[0] for x in per_w[w]] for w in range(4)}; bks = {w: [x[1] for x in per_w[w]] for w in range(4)}
        newly = 0
        for n_rec, ((w, ri), hl) in enumerate(hits.items()):
            tick(f"  resolving copies: pass {it + 1}/3, {n_rec:,}/{len(hits):,} records, {newly:,} resolved")
            if (w, ri) in fixed or len(hl) < 2: continue
            k = bisect.bisect_left(idxs[w], ri)
            lo = bks[w][k - 1] if k > 0 else buckets[0]; hi = bks[w][k] if k < len(bks[w]) else buckets[-1]
            ok = [h for h in _hits_between(hl, lo, hi) if owner.get(h[0], w) == w]
            if len(ok) == 1: fixed[(w, ri)] = ok[0]; newly += 1
        owner, conf = owners_from(fixed); report[f"resolved-it{it}"] = newly
        if not newly: break
    report["owner-conflicts-final"] = conf
    # --- per writer: storage-ordered anchors; fill gaps
    placements = {w: {} for w in range(4)}
    for w in range(4):
        recs = writers[w]
        seq = sorted((h[0], h[1], ri) for (ww, ri), h in fixed.items() if ww == w)
        anchored = {ri for _, _, ri in seq}
        untagged = [r for r in recs if r.n > 0 and r.idx not in anchored]       # includes tagged-but-unanchored
        unt_idx = [r.idx for r in untagged]
        used = set()
        for k in range(len(seq)):
            tick(f"  placing writer {w}: {k:,}/{len(seq):,} anchors, placed={report['placed']:,}")
            bA, eA, ri = seq[k]; r = recs[ri]
            bB, eB, riB = seq[k + 1] if k + 1 < len(seq) else (None, None, None)
            mids = buckets[bpos[bA] + 1: bpos[bB]] if bB is not None else []
            owned_mid = [b for b in mids if owner.get(b) == w]; free_mid = [b for b in mids if b not in owner]
            if bB == bA: avail = eB - eA
            else: avail = (counts[bA] - eA) + sum(counts[b] for b in owned_mid) + (eB if bB is not None else 0)
            gap = avail - r.n           # chunks between A's end and B's start that are not A's
            chosen_free = []
            if gap < 0 and free_mid:    # A needs more room: take free buckets (rhythm first) to make up the deficit
                need = -gap; pref = sorted(free_mid, key=lambda b: (0 if rhythm(b) == w else 1, b)); tot = 0
                for b in pref:
                    if tot + counts[b] <= need: chosen_free.append(b); tot += counts[b]
                    if tot == need: break
                if tot == need:
                    for b in chosen_free: owner[b] = w
                    gap = 0; report["free-assigned"] += len(chosen_free)
                else:
                    # allow untagged files to pad: try need + small untagged sums
                    cands = [x for x in untagged if x.idx not in used and ri - 400 < x.idx < (riB if riB is not None else ri + 400) + 400]
                    found = None
                    for x in cands[:200]:
                        tot2 = 0; sel = []
                        for b in pref:
                            if tot2 + counts[b] <= need + x.n: sel.append(b); tot2 += counts[b]
                            if tot2 == need + x.n: break
                        if tot2 == need + x.n: found = (x, sel); break
                    if found:
                        x, sel = found
                        for b in sel: owner[b] = w
                        chosen_free = sel; gap = x.n; report["free-assigned"] += len(sel)
                    else: report["unresolved-deficit"] += 1
            gapfiles = []
            if gap > 0:
                # untagged records of this writer near A/B in stream order whose chunk counts sum to the gap
                near = [x for x in untagged if x.idx not in used and (ri - 300) < x.idx < ((riB if riB is not None else ri) + 300)]
                near.sort(key=lambda x: abs(x.idx - ri))
                one = [x for x in near if x.n == gap]
                if one: gapfiles = [one[0]]
                else:
                    # two-record combination
                    byn = {}
                    for x in near[:300]: byn.setdefault(x.n, x)
                    for x in near[:300]:
                        y = byn.get(gap - x.n)
                        if y is not None and y is not x: gapfiles = [x, y]; break
                if gapfiles:
                    for x in gapfiles: used.add(x.idx)
                    report["gap-filled"] += 1
                else: report["gap-unfilled"] += 1
            elif gap < 0: report["deficit"] += 1
            # emit placements: A, then gap files, walking the writer's buckets in order
            path = [bA] + sorted(owned_mid + chosen_free) + ([bB] if bB is not None and bB != bA else [])
            cursor = [0, eA]     # index into path, entry
            def take(n):
                segs = []
                while n > 0 and cursor[0] < len(path):
                    b = path[cursor[0]]; room = (eB if b == bB else counts[b]) - cursor[1]    # bB: only up to the next anchor
                    if room <= 0: cursor[0] += 1; cursor[1] = 0; continue
                    t = min(n, room); segs.append((b, cursor[1], t)); cursor[1] += t; n -= t
                return segs, n
            segs, rem = take(r.n)
            if rem == 0: placements[w][ri] = segs; report["placed"] += 1
            else: report["unplaced"] += 1
            for x in gapfiles:
                segs, rem = take(x.n)
                if rem == 0: placements[w][x.idx] = segs; report["placed-gap"] += 1
    log(f"phase2: {dict(report)}; owners {len(owner):,}/{len(buckets):,}")
    return owner, placements, report

def export_map(writers, owner, placements, counts, outdir, mapped_upto, flags=None, report=None, complete=False):
    """poolmap2.json for PoolReader: owners, entry counts, per-record placement segments and id ranges; ``guessed`` lists
    the records placed without a byte check (set -> [rec_off]) and ``aliased`` the copies restored from another record's
    entries (set -> [[rec_off, source set, source rec_off], ...], the source null for a copy of an untagged original);
    ``stats`` is the placer's report; ``map_complete`` says the placer ran over every bucket (the map is as good as the loaded
    snapshots allow: the buckets without an owner hold files it could not locate, rerunning changes nothing)."""
    out = {"buckets": {str(b): w for b, w in owner.items()}, "counts": {str(b): counts[b] for b in owner}, "placements": {}, "ranges": {}, "mapped_upto": mapped_upto, "map_complete": bool(complete)}
    flags = flags or {}
    out["guessed"] = {str(SET_OF_WRITER[w]): sorted(writers[w][ri].off for (ww, ri) in flags.get("guessed", ()) if ww == w) for w in range(4)}
    al = flags.get("aliased", {}); al = al if isinstance(al, dict) else {k: None for k in al}
    out["aliased"] = {str(SET_OF_WRITER[w]): sorted([writers[w][ri].off] + ([SET_OF_WRITER[src[0]], writers[src[0]][src[1]].off] if src else [None, None])
                                                     for (ww, ri), src in al.items() if ww == w) for w in range(4)}
    if report is not None: out["stats"] = {k: v for k, v in dict(report).items()}
    for w in range(4):
        s = SET_OF_WRITER[w]; recs = writers[w]
        out["placements"][str(s)] = {str(recs[i].off): segs for i, segs in placements[w].items()}
        out["ranges"][str(s)] = [[a, L, r.off] for r in recs for a, L in r.ranges]
    dump_json(os.path.join(outdir, "poolmap2.json"), out)
    return out

if __name__ == "__main__":
    import sys
    if len(sys.argv) == 4 and sys.argv[1] == "--worker": _worker_main(sys.argv[2:])
    else: sys.exit("usage: python -m callido_restore.pool.anchor --worker <job.pkl> <out.pkl>")
