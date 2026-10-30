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
