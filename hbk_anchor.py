"""Anchor-first pool mapping: locate every tagged record's first chunk in the bucket indexes (tag = md5 of the first K
chunk md5s + their total length), derive bucket owners from anchors, then place the untagged remainder by chunk counts."""
import os, json, hashlib, collections, time, bisect
from array import array
from hbk_pool import SET_OF_WRITER, WRITER_OF_SET, bucket_index_entries
from hbk_poolmap2 import build_writers

def scan_anchors(source, outdir, listing, writers, log=print, max_bucket=None, batch_log=500):
    # index: (K, tail) -> [(w, rec idx, tag)]
    idx = collections.defaultdict(list); ntag = 0
    for w, recs in writers.items():
        for r in recs:
            if r.tag and r.n > 0:
                K = min(r.n + len(r.dedup), 9)
                if r.head_new < K: continue            # leading chunks include dedup refs: tag would match elsewhere
                idx[(K, r.tail)].append((w, r.idx, r.tag)); ntag += 1
    log(f"anchor scan: {ntag:,} tagged records, {len(idx):,} (K,tail) keys")
    names = {}; counts = {}
    for e in listing:
        if e["t"] == "index" and (e["b"] not in names or e["rev"] > names[e["b"]][1]): names[e["b"]] = (e["name"], e["rev"]); counts[e["b"]] = (e["size"] - 64) // 32
    buckets = sorted(b for b in counts if max_bucket is None or b <= max_bucket)
    anchors = collections.defaultdict(list)     # (w, idx) -> [(bucket, entry, K)]
    t0 = time.time(); checks = 0
    for n_done, b in enumerate(buckets):
        ents = bucket_index_entries(source.ensure(f"Pool/0/{b // 2048}/{names[b][0]}"))
        ul = [e[2] for e in ents]; ps = [0]
        for x in ul: ps.append(ps[-1] + x)
        n = len(ents)
        for i in range(n):
            base = ps[i]
            for K in range(1, 10):
                if i + K > n: break
                key = (K, ps[i + K] - base)
                cands = idx.get(key)
                if not cands: continue
                h = hashlib.md5(b"".join(e[3][:16] for e in ents[i:i + K])).hexdigest(); checks += 1
                for w, ri, tag in cands:
                    if tag == h: anchors[(w, ri)].append((b, i, K))
        if (n_done + 1) % batch_log == 0: log(f"  scanned {n_done + 1}/{len(buckets)} buckets, anchors={len(anchors):,}, md5 checks={checks:,}, {time.time() - t0:.0f}s")
    log(f"anchor scan done: {len(anchors):,} records anchored of {ntag:,} tagged; {sum(1 for v in anchors.values() if len(v) > 1):,} with multiple hits; {time.time() - t0:.0f}s")
    out = {f"{w}:{ri}": v for (w, ri), v in anchors.items()}
    json.dump(out, open(os.path.join(outdir, "anchors.json"), "w"))
    return anchors, counts

def rhythm(b): return ((b - 20) // 4) % 4

def place_from_anchors(writers, anchors, counts, log=print, max_bucket=None):
    """anchors: {(w, idx): [(bucket, entry, K), ...]}. Returns (owner dict bucket->w, placements {w: {idx: [(b, e, n)]}}, report)."""
    buckets = sorted(b for b in counts if max_bucket is None or b <= max_bucket); bset = set(buckets)
    # 1. owners from unique anchors
    votes = collections.defaultdict(collections.Counter)
    for (w, ri), hits in anchors.items():
        hits = [h for h in hits if h[0] in bset]
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
        seq = sorted(((h[0][0], h[0][1], ri) for (ww, ri), h in anchors.items() if ww == w and len([x for x in h if x[0] in bset]) == 1 for h in [[x for x in h if x[0] in bset]]))
        log(f"writer{w}: {len(seq):,} unique anchors")
        for k in range(len(seq)):
            bA, eA, ri = seq[k]; r = recs[ri]; need = r.n
            if k + 1 < len(seq): bB, eB, _ = seq[k + 1]
            else: bB, eB = None, None
            # walk: A's bucket tail, then mid buckets, then B's head
            segs = []; cur_b = bA; cur_e = eA; remaining = need; gap = 0
            mids = [b for b in buckets if bA < b < (bB if bB is not None else 10 ** 9)]
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
                take = min(remaining, counts[b] - e)
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
    hits = {k: [h for h in v if h[0] in bset] for k, v in anchors.items()}
    fixed = {k: v[0] for k, v in hits.items() if len(v) == 1}
    report = collections.Counter(); report["unique"] = len(fixed); report["multi"] = sum(1 for v in hits.values() if len(v) > 1)
    def owners_from(fixed):
        votes = collections.defaultdict(collections.Counter)
        for (w, ri), (b, e, K) in fixed.items(): votes[b][w] += 1
        return {b: c.most_common(1)[0][0] for b, c in votes.items()}, sum(1 for c in votes.values() if len(c) > 1)
    owner, conf = owners_from(fixed); report["owner-conflicts"] = conf
    # --- resolve multi-hit records by neighbours (iterate)
    for it in range(3):
        per_w = {w: sorted((ri, h[0]) for (ww, ri), h in fixed.items() if ww == w) for w in range(4)}
        idxs = {w: [x[0] for x in per_w[w]] for w in range(4)}; bks = {w: [x[1] for x in per_w[w]] for w in range(4)}
        newly = 0
        for (w, ri), hl in hits.items():
            if (w, ri) in fixed or len(hl) < 2: continue
            k = bisect.bisect_left(idxs[w], ri)
            lo = bks[w][k - 1] if k > 0 else buckets[0]; hi = bks[w][k] if k < len(bks[w]) else buckets[-1]
            ok = [h for h in hl if lo <= h[0] <= hi and owner.get(h[0], w) == w]
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
                    b = path[cursor[0]]; room = counts[b] - cursor[1]
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

def export_map(writers, owner, placements, counts, outdir, mapped_upto):
    out = {"buckets": {str(b): w for b, w in owner.items()}, "counts": {str(b): counts[b] for b in owner}, "placements": {}, "ranges": {}, "mapped_upto": mapped_upto}
    for w in range(4):
        s = SET_OF_WRITER[w]; recs = writers[w]
        out["placements"][str(s)] = {str(recs[i].off): segs for i, segs in placements[w].items()}
        out["ranges"][str(s)] = [[a, L, r.off] for r in recs for a, L in r.ranges]
    json.dump(out, open(os.path.join(outdir, "poolmap2.json"), "w"))
    return out
