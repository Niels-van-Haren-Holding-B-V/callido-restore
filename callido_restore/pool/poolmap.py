"""Build the bucket -> writer map for a Hyper Backup pool (see core/pool.py for the format notes)."""
import os, json, struct, bisect, collections, subprocess, sys, threading, time
from array import array
from callido_restore.core.pool import BLOCK, WRITER_OF_SET, SET_OF_WRITER, load_runs

class WriterModel:
    """One writer's stored chunk sequence = its file pieces (runs of newly stored chunks) in allocation order."""
    def __init__(self, w):
        self.w = w; self.blocks = []; self.pos = {}
        self.raw_start = array("q"); self.length = array("q"); self.stored_start = array("q"); self.rec = array("q")
        self.size = []; self.dedup = []
        self.total = 0

def _is_new(g, frontier):
    return g == frontier + 1 or (g % BLOCK == 0 and g > frontier)

def new_range(g, L, frontier):
    """Part of run [g, g+L) that is newly stored given the writer's allocation frontier: ids <= frontier are dedup refs
    (a file may reference the chunk it just stored again), ids continuing from frontier+1 are new; a run starting at a
    block boundary beyond the frontier is a new block."""
    end = g + L
    if g <= frontier + 1 < end: return frontier + 1, end
    if g > frontier + 1 and g % BLOCK == 0: return g, end
    return None

def build_models(outdir, log=print):
    owner = {}; conflicts = 0; runs = {}
    claims = collections.defaultdict(set); votes = collections.defaultdict(lambda: [0, 0, 0, 0])
    for s in (1, 2, 3, 4):
        w = WRITER_OF_SET[s]; rec, g0, ln = load_runs(outdir, s); runs[w] = (rec, g0, ln)
        frontier = -1
        for i in range(len(g0)):
            g = g0[i]; L = ln[i]; b0, b1 = g // BLOCK, (g + L - 1) // BLOCK
            for b in range(b0, b1 + 1):
                lo, hi = max(g, b * BLOCK), min(g + L, (b + 1) * BLOCK); votes[b][w] += hi - lo
            nr = new_range(g, L, frontier)
            if nr:
                for b in range(nr[0] // BLOCK, (nr[1] - 1) // BLOCK + 1): claims[b].add(w)
                frontier = nr[1] - 1
    for b, ws in claims.items():
        if len(ws) == 1: owner[b] = next(iter(ws))
        else: conflicts += 1; owner[b] = max(ws, key=lambda w: votes[b][w])   # the owner stores the whole block
    log(f"blocks owned: {len(owner):,} conflicts resolved by reference count={conflicts}")
    models = {w: WriterModel(w) for w in range(4)}
    for b, w in owner.items(): models[w].blocks.append(b)
    for m in models.values(): m.blocks.sort(); m.pos = {b: i for i, b in enumerate(m.blocks)}
    sizes = {s: {int(k): v for k, v in json.load(open(os.path.join(outdir, f"recsize_set{s}.json"))).items()} for s in (1, 2, 3, 4)}
    for w, m in models.items():
        rec, g0, ln = runs[w]; sz = sizes[SET_OF_WRITER[w]]
        i = 0; n = len(rec); frontier = -1; pieces = []   # (raw_start, len, rec, size, dedup)
        while i < n:
            r = rec[i]; j = i
            while j < n and rec[j] == r: j += 1
            cur = None; dedup = []
            for k in range(i, j):
                g = g0[k]; L = ln[k]
                nr = new_range(g, L, frontier)
                if nr and owner.get(nr[0] // BLOCK) == w:
                    ns, ne = nr
                    dedup.extend(range(g, ns))
                    # a new range may cross a block boundary only if the writer owns the next block too (consecutive ids)
                    while ns < ne:
                        blk = ns // BLOCK; seg_end = min(ne, (blk + 1) * BLOCK)
                        if owner.get(blk) != w: dedup.extend(range(ns, seg_end)); ns = seg_end; continue
                        p = m.pos[blk] * BLOCK + ns % BLOCK; Lp = seg_end - ns
                        if cur is not None and p == cur[0] + cur[1]: cur[1] += Lp
                        else:
                            if cur is not None: pieces.append(cur)
                            cur = [p, Lp, r]
                        ns = seg_end
                    frontier = ne - 1
                else:
                    dedup.extend(range(g, g + L))
            if cur is not None: pieces.append(cur)
            # attach size + dedup to the LAST piece of the record (check happens when it completes)
            if pieces and pieces[-1][2] == r:
                pieces[-1].extend([sz.get(r, -1), dedup, True])
            i = j
        pieces.sort(key=lambda x: x[0])
        stored = 0; overl = 0; prev_end = -1
        for pc in pieces:
            p, L, r = pc[0], pc[1], pc[2]
            if p < prev_end: overl += 1
            m.raw_start.append(p); m.length.append(L); m.stored_start.append(stored); m.rec.append(r)
            if len(pc) > 3: m.size.append(pc[3]); m.dedup.append(pc[4])
            else: m.size.append(None); m.dedup.append(None)     # not the record's last piece: no check
            stored += L; prev_end = max(prev_end, p + L)
        m.total = stored
        known = sum(1 for x in m.size if x is not None and x >= 0)
        log(f"writer{w}: blocks={len(m.blocks):,} pieces={len(pieces):,} stored chunks={stored:,} overlaps={overl} size-checks={known:,}")
    return models

class Mapper:
    def __init__(self, source, outdir, listing, models, log=print):
        self.src, self.outdir, self.models, self.log = source, outdir, models, log
        self.counts = {}; self.names = {}
        for e in listing:
            if e["t"] == "index" and (e["b"] not in self.names or e["rev"] > self.names[e["b"]][1]):
                self.names[e["b"]] = (e["name"], e["rev"]); self.counts[e["b"]] = (e["size"] - 64) // 32
        self.cache = collections.OrderedDict(); self.assigned = {w: [] for w in range(4)}; self.stats = collections.Counter()
    def ulens(self, b):
        u = self.cache.get(b)
        if u is None:
            p = self.src.ensure(f"Pool/0/{b // 2048}/{self.names[b][0]}")
            d = open(p, "rb").read()
            u = array("l", [struct.unpack_from(">I", d, q + 8)[0] for q in range(64, len(d) - 31, 32)])
            self.cache[b] = u
            if len(self.cache) > 6000: self.cache.popitem(last=False)
        return u
    def chunk_ulen(self, g):
        """ulen of stored chunk g if its bucket is assigned, else None."""
        blk = g // BLOCK
        for w, m in self.models.items():
            bp = m.pos.get(blk)
            if bp is None: continue
            p = bp * BLOCK + g % BLOCK
            i = bisect.bisect_right(m.raw_start, p) - 1
            if i < 0 or p >= m.raw_start[i] + m.length[i]: return None
            sp = m.stored_start[i] + (p - m.raw_start[i])
            lst = self.assigned[w]; k = bisect.bisect_right(lst, (sp, 1 << 62)) - 1
            if k < 0: return None
            s0, b = lst[k]
            return self.ulens(b)[sp - s0] if sp - s0 < self.counts[b] else None
        return None
    def stored_pos(self, g):
        """global chunk id -> (writer, stored position) via the piece tables, or None."""
        blk = g // BLOCK
        for w, m in self.models.items():
            bp = m.pos.get(blk)
            if bp is None: continue
            p = bp * BLOCK + g % BLOCK
            i = bisect.bisect_right(m.raw_start, p) - 1
            if i < 0 or p >= m.raw_start[i] + m.length[i]: return None
            return w, m.stored_start[i] + (p - m.raw_start[i])
        return None
    def try_assign(self, w, b, state):
        """state = (piece idx, offset in piece, running sum). Returns (new state, verified checks) or (None, reason)."""
        m = self.models[w]; pi, off, run = state
        u = self.ulens(b); c = len(u); k = 0; np_ = len(m.length); verified = 0
        sp0 = (m.stored_start[pi] + off) if pi < np_ else m.total      # stored position of this bucket's first entry
        while k < c:
            if pi >= np_: return None, "beyond last piece"
            L = m.length[pi]; take = min(L - off, c - k)
            run += sum(u[k:k + take]); off += take; k += take
            if off == L:
                size = m.size[pi]
                if size is not None and size >= 0:
                    extra = 0; ok = True
                    for g in m.dedup[pi]:
                        ul = self.chunk_ulen(g)
                        if ul is None:
                            sp = self.stored_pos(g)
                            if sp and sp[0] == w and sp0 <= sp[1] < sp0 + c: ul = u[sp[1] - sp0]
                        if ul is None: ok = False; self.stats["unverifiable"] += 1; break
                        extra += ul
                    if ok and run + extra != size: self.stats["reject"] += 1; return None, f"size mismatch piece {pi}: got {run + extra} want {size}"
                    if ok:
                        self.stats["verified"] += 1
                        # evidence weight: tiny 1-chunk files (741-byte @SynoResource etc.) match by coincidence; weigh by chunk count
                        verified += L if L >= 3 else 0
                if size is not None: run = 0
                pi += 1; off = 0
        return (pi, off, run), verified
    def run(self, max_bucket=None, max_disc=10, verbose=False, budget=400000):
        """Limited-discrepancy search: candidates are ordered by which writer is 'due' (writers fill buckets at steady
        rates), and at most D deviations from that order are allowed; D grows until the whole range is consistent."""
        buckets = sorted(b for b in self.counts if max_bucket is None or b <= max_bucket)
        last = ({}, 0)
        for D in range(0, max_disc + 1):
            res, upto, ok = self._search(buckets, D, verbose, budget)
            if ok: self.log(f"done: {len(res)} buckets with D={D} discrepancies"); return res, upto
            self.log(f"D={D}: reached bucket index {upto}/{len(buckets)}; retrying with more discrepancies")
            if upto > last[1]: last = (res, upto)
        return last
    def _search(self, buckets, D, verbose, budget):
        state = {w: (0, 0, 0) for w in range(4)}; self.assigned = {w: [] for w in range(4)}; result = {}
        stack = []; owners = []; i = 0; backtracks = 0; disc = 0; t0 = time.time(); last_log = 0; last_t = time.time()
        while i < len(buckets):
            b = buckets[i]; cands = []; reasons = {}
            for w in range(4):
                ns, why = self.try_assign(w, b, state[w])
                if ns is not None: cands.append((w, ns, why))
                else: reasons[w] = why
            strong = [c for c in cands if c[2] > 0]
            if strong: cands = sorted(strong, key=lambda c: -c[2])
            elif len(cands) > 1:
                # bucket ids are handed out in aligned groups of 4 per writer, round-robin: predict from the rhythm
                pred = None
                if b % 4 != 0 and (b - 1) in result: pred = result[b - 1][0]
                elif (b - 16) in result: pred = result[b - 16][0]
                if pred is not None and any(c[0] == pred for c in cands):
                    cands.sort(key=lambda c: 0 if c[0] == pred else 1)
                else:
                    win = owners[-24:]; n = len(win); active = [c[0] for c in cands]
                    cnt = collections.Counter(win)
                    cands.sort(key=lambda c: -((n / max(1, len(active))) - cnt[c[0]]))
            cands = [(c[0], c[1]) for c in cands]
            if not cands:
                if verbose: self.log(f"  bucket {b}: no candidate: {reasons}")
                while stack:
                    bi, alts, saved_state, saved_len, saved_own, fdisc = stack.pop()
                    state = dict(saved_state)
                    for w in range(4): del self.assigned[w][saved_len[w]:]
                    for bb in buckets[bi:i]: result.pop(bb, None)
                    del owners[saved_own:]; i = bi; backtracks += 1
                    # Backtracking can keep the search below the last logged index for minutes: report on time, not on count.
                    if time.time() - last_t >= 2: last_t = time.time(); self.log(f"  D={D}: mapped {i}/{len(buckets)} buckets, backtracks={backtracks}, {time.time()-t0:.0f}s (searching)")
                    if alts and fdisc + 1 <= D:
                        w, ns = alts.pop(0); disc = fdisc + 1
                        if alts: stack.append((bi, alts, saved_state, saved_len, saved_own, fdisc))
                        self._apply(w, buckets[bi], ns, state, result); owners.append(w); i += 1
                        break
                else:
                    return result, i, False
                if backtracks > budget: self.log("backtrack budget exhausted"); return result, i, False
                continue
            w, ns = cands[0]; alts = cands[1:]
            if alts and disc < D: stack.append((i, alts, dict(state), {ww: len(self.assigned[ww]) for ww in range(4)}, len(owners), disc))
            self._apply(w, b, ns, state, result); owners.append(w); i += 1
            if i - last_log >= 1000 or time.time() - last_t >= 2: last_log = i; last_t = time.time(); self.log(f"  D={D}: mapped {i}/{len(buckets)} buckets, backtracks={backtracks}, {time.time()-t0:.0f}s")
        return result, i, True
    def _apply(self, w, b, ns, state, result):
        s0 = self.models[w].stored_start[state[w][0]] + state[w][1] if state[w][0] < len(self.models[w].length) else self.models[w].total
        state[w] = ns; self.assigned[w].append((s0, b)); result[b] = (w, s0)

def build_poolmap(source, outdir, log=print, max_bucket=None):
    listing = json.load(open(os.path.join(outdir, "listing.json")))
    models = build_models(outdir, log)
    mp = Mapper(source, outdir, listing, models, log)
    result, upto = mp.run(max_bucket=max_bucket)
    out = {"blocks": {w: m.blocks for w, m in models.items()},
           "pieces": {w: {"raw_start": list(m.raw_start), "length": list(m.length), "stored_start": list(m.stored_start)} for w, m in models.items()},
           "counts": {b: mp.counts[b] for b in result}, "buckets": {b: list(v) for b, v in result.items()}, "mapped_upto": upto}
    json.dump(out, open(os.path.join(outdir, "poolmap.json"), "w"))
    return result, upto


# ----------------------------------------------------------------------------- the mapper as a child process
# The search is pure Python at 100 % CPU for minutes; inside the app process it holds the interpreter lock, so every API
# request and the Stop button wait behind it. As a child process it costs the app nothing and Stop simply ends it.
DETACHED = True
MAPPER_VERSION = "anchor+place-v3"               # bump when the map's meaning changes: finished preparations of older versions are redone
RUNNING = set()                                  # mapper children alive right now (Quit kills them with their workers)

def _kill_group(p, grace=3.0):
    """SIGTERM to the child's whole process group (the mapper and its scan workers), SIGKILL after ``grace`` seconds."""
    import signal
    try: os.killpg(p.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, AttributeError): p.terminate()
    try: p.wait(grace)
    except subprocess.TimeoutExpired:
        try: os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, AttributeError): p.kill()
        try: p.wait(3)
        except subprocess.TimeoutExpired: pass

def kill_running():
    """End every mapper child of this app (Quit): their process groups, workers included."""
    for p in list(RUNNING):
        if p.poll() is None: _kill_group(p)
        RUNNING.discard(p)

def _ps_commands():
    """[(pid, ppid, command line)] (patched in tests); [] where ps is not available."""
    if os.name == "nt": return []
    try: r = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True)
    except OSError: return []
    rows = []
    for line in r.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit(): rows.append((int(parts[0]), int(parts[1]), parts[2]))
    return rows

def kill_orphan_helpers():
    """At app start: mapper children and scan workers of a crashed or killed app (the app binary with --mapper or
    --scan-worker, parent gone) are ended, so they stop burning CPU. Returns the number ended."""
    import signal
    from callido_restore.about import APP_NAME
    marker = f"{APP_NAME}.app/Contents/MacOS/{APP_NAME} --"
    n = 0
    for pid, ppid, cmd in _ps_commands():
        if ppid != 1 or pid == os.getpid(): continue
        if marker not in cmd or not any(f" --{k} " in cmd + " " for k in ("mapper", "scan-worker")): continue
        try: os.kill(pid, signal.SIGTERM); n += 1
        except OSError: continue
    return n

def _mapper_command(source_id, outdir, max_bucket):
    """The child's command line: the frozen app binary with --mapper, or `python -m callido_restore --mapper` from a checkout."""
    args = ["--mapper", source_id, str(outdir), "-" if max_bucket is None else str(max_bucket)]
    if getattr(sys, "frozen", False): return [sys.executable] + args, None
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env = dict(os.environ, PYTHONPATH=root + os.pathsep + os.environ.get("PYTHONPATH", ""))
    return [sys.executable, "-m", "callido_restore"] + args, env

def build_poolmap_anchor(source, outdir, log=print, max_bucket=None):
    """The anchor-first map (T1, pool/anchor.py): tagged records located in the bucket indexes, owners and placements derived
    from them, exported as poolmap2.json which PoolReader prefers. Returns ({bucket: owner}, mapped_upto)."""
    from callido_restore.pool import anchor, poolmap2
    from callido_restore.core.pool import load_json
    listing = load_json(os.path.join(outdir, "listing.json"))
    log("loading the record tables of the 4 writers (a few GB, one to two minutes)…")
    writers = poolmap2.build_writers(outdir, log)
    log("scanning the bucket indexes for tagged records…")
    anchors, counts = anchor.scan_anchors(source, outdir, listing, writers, log=log, max_bucket=max_bucket)
    flags = None
    if os.environ.get("HBK_PLACER", "v3") == "v2":
        log("placing records: owners from anchors, copies by neighbours, gaps by chunk count…")
        owner, placements, report = anchor.resolve_and_place(writers, anchors, counts, log=log, max_bucket=max_bucket)
    else:
        from callido_restore.pool import place
        log("placing records: copies, pins, claims of large-file interiors by count and bytes, size checks…")
        names = {}
        for e in listing:
            if e.get("t") == "index" and e.get("name") and e.get("rev", 0) >= names.get(e["b"], ("", -1))[1]: names[e["b"]] = (e["name"], e["rev"])
        index = place.Index(counts, anchor.bucket_revs(listing), lambda b: str(source.ensure(f"Pool/0/{b // 2048}/{names[b][0]}")),
                            ubytes=anchor.load_bucket_totals(outdir), lru=1500)          # the scan just saved them
        owner, placements, report, flags = place.resolve_and_place_v3(writers, anchors, index, log=log, max_bucket=max_bucket)
    buckets = sorted(b for b in counts if max_bucket is None or b <= max_bucket)
    upto = len(buckets) if len(owner) >= len(buckets) else len(owner)        # partial when not every bucket got an owner
    anchor.export_map(writers, owner, placements, counts, outdir, mapped_upto=upto, flags=flags, report=report)
    log(f"mapped {len(owner)}/{len(buckets)} buckets (anchor-first); {dict(report)}")
    return dict(owner), upto

def build_map(source, outdir, log=print, max_bucket=None):
    """The app's mapper: anchor-first (TASKS.md T1: the sequential search below is obsolete), the v1 search only on request
    (HBK_MAPPER=v1) for comparisons."""
    if os.environ.get("HBK_MAPPER", "anchor") == "v1": return build_poolmap(source, outdir, log, max_bucket)
    return build_poolmap_anchor(source, outdir, log, max_bucket)

def read_map(outdir):
    """({bucket: ...}, mapped_upto) of the map a mapper left in outdir: poolmap2.json (anchor-first) first, else poolmap.json."""
    for name in ("poolmap2.json", "poolmap.json"):
        p = os.path.join(outdir, name)
        if os.path.isfile(p):
            out = json.load(open(p))
            return {int(b): (tuple(v) if isinstance(v, list) else v) for b, v in out["buckets"].items()}, out.get("mapped_upto", 0)
    raise RuntimeError("the mapper left no map file")

def mapper_main(argv):
    """Child entry: build the pool map of one source, every log line to stdout (the parent turns them into progress)."""
    from callido_restore.sources.source import load_sources, SOURCES
    load_sources()
    sid, outdir, mb = argv[0], argv[1], argv[2]
    src = SOURCES.get(sid)
    if src is None: raise SystemExit(f"mapper: unknown source {sid}")
    build_map(src, outdir, log=lambda m: print(m, flush=True), max_bucket=None if mb == "-" else int(mb))
    return 0

def build_poolmap_detached(source, outdir, log=print, max_bucket=None, cancelled=None):
    """``build_poolmap`` in a child process. Returns what ``build_poolmap`` returns (read back from poolmap.json), or
    ({}, 0) when ``cancelled()`` turned true (the child is terminated, nothing is left running). Raises RuntimeError with the
    child's last lines when it fails. ``DETACHED = False`` (tests) runs in-process instead."""
    if not DETACHED: return build_map(source, outdir, log, max_bucket)
    cmd, env = _mapper_command(source.id, outdir, max_bucket)
    # Its own session: the child's scan workers are in the same process group, so Stop (or Quit) ends all of them at once.
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, env=env, start_new_session=True)
    RUNNING.add(p)
    tail = collections.deque(maxlen=8)
    def reader():
        for line in p.stdout:
            line = line.rstrip("\n"); tail.append(line)
            try: log(line)
            except Exception: pass
    t = threading.Thread(target=reader, daemon=True, name="mapper-log"); t.start()
    try:
        while p.poll() is None:
            if cancelled is not None and cancelled():
                _kill_group(p); t.join(2); return {}, 0
            time.sleep(0.3)
    finally:
        RUNNING.discard(p)
    t.join(5)
    if p.returncode != 0:
        raise RuntimeError("mapper failed: " + (" | ".join(l for l in tail if l.strip())[-400:] or f"exit {p.returncode}"))
    return read_map(outdir)
