"""Tag-anchored bucket mapper (v2). See docs/FORMAT.md.

Storage order inside a writer's bucket stream is NOT chunk-id order: big files get their whole id range up front but are
stored after the smaller files queued behind them. So the mapper identifies the file starting at each position by its
snapshot *tag* (md5 of the first K chunk hashes + their total length), keeps unmatched big files pending, verifies file
ends by size, and assigns buckets to writers with the round-robin rhythm as a prior plus limited-discrepancy search."""
import os, bisect, collections, time, hashlib
from array import array
from callido_restore.core.pool import BLOCK, WRITER_OF_SET, SET_OF_WRITER, load_runs, bucket_index_entries, load_json, dump_json
from callido_restore.pool.poolmap import new_range

LOOK = 96            # how many upcoming records (stream order) are considered as "the file starting here"
TAGK = 9

class Rec:
    __slots__ = ("off", "ranges", "n", "dedup", "size", "tag", "tail", "idx", "head_new")
    def __init__(self, off): self.off = off; self.ranges = []; self.n = 0; self.dedup = []; self.size = None; self.tag = None; self.tail = None; self.idx = -1; self.head_new = 0

def build_writers(outdir, log=print):
    """Per writer: records in stream order with new-id ranges, dedup ids, size, tag."""
    owner = {}; claims = collections.defaultdict(set); votes = collections.defaultdict(lambda: [0, 0, 0, 0]); runs = {}
    for s in (1, 2, 3, 4):
        w = WRITER_OF_SET[s]; rec, g0, ln = load_runs(outdir, s); runs[w] = (rec, g0, ln); frontier = -1
        for i in range(len(g0)):
            g = g0[i]; L = ln[i]
            for b in range(g // BLOCK, (g + L - 1) // BLOCK + 1):
                lo, hi = max(g, b * BLOCK), min(g + L, (b + 1) * BLOCK); votes[b][w] += hi - lo
            nr = new_range(g, L, frontier)
            if nr:
                for b in range(nr[0] // BLOCK, (nr[1] - 1) // BLOCK + 1): claims[b].add(w)
                frontier = nr[1] - 1
    for b, ws in claims.items(): owner[b] = next(iter(ws)) if len(ws) == 1 else max(ws, key=lambda w: votes[b][w])
    meta = {s: {int(k): v for k, v in load_json(os.path.join(outdir, f"recmeta_set{s}.json")).items()} for s in (1, 2, 3, 4)}
    writers = {}
    for w in range(4):
        rec, g0, ln = runs[w]; s = SET_OF_WRITER[w]; mt = meta[s]; recs = []; frontier = -1; i = 0; n = len(rec)
        while i < n:
            r = Rec(rec[i]); j = i
            while j < n and rec[j] == r.off: j += 1
            head_open = True
            for k in range(i, j):
                g = g0[k]; L = ln[k]; nr = new_range(g, L, frontier)
                if nr and owner.get(nr[0] // BLOCK) == w:
                    ns, ne = nr; r.dedup.extend(range(g, ns))
                    if head_open and ns == g: r.head_new += ne - ns
                    else: head_open = False
                    while ns < ne:
                        blk = ns // BLOCK; seg_end = min(ne, (blk + 1) * BLOCK)
                        if owner.get(blk) != w: r.dedup.extend(range(ns, seg_end))
                        else:
                            if r.ranges and r.ranges[-1][0] + r.ranges[-1][1] == ns: r.ranges[-1][1] += seg_end - ns
                            else: r.ranges.append([ns, seg_end - ns])
                            r.n += seg_end - ns
                        ns = seg_end
                    frontier = ne - 1
                else: r.dedup.extend(range(g, g + L)); head_open = False
            m = mt.get(r.off)
            if m: r.size = m[0]; r.tag = m[1]; r.tail = m[2]
            r.idx = len(recs); recs.append(r); i = j
        writers[w] = recs
        log(f"writer{w}: records={len(recs):,} stored chunks={sum(r.n for r in recs):,} tagged={sum(1 for r in recs if r.tag):,} sized={sum(1 for r in recs if r.size is not None):,}")
    return writers

class Mapper2:
    def __init__(self, source, outdir, listing, writers, log=print):
        self.src, self.outdir, self.W, self.log = source, outdir, writers, log
        self.counts = {}; self.names = {}
        for e in listing:
            if e["t"] == "index" and (e["b"] not in self.names or e["rev"] > self.names[e["b"]][1]):
                self.names[e["b"]] = (e["name"], e["rev"]); self.counts[e["b"]] = (e["size"] - 64) // 32
        self.cache = collections.OrderedDict(); self.stats = collections.Counter()
        # id -> record (for dedup lookups): per writer sorted ranges
        self.rng = {}
        for w, recs in writers.items():
            starts = array("q"); lens = array("q"); ridx = array("l")
            for r in recs:
                for a, L in r.ranges: starts.append(a); lens.append(L); ridx.append(r.idx)
            order = sorted(range(len(starts)), key=lambda i: starts[i])
            self.rng[w] = (array("q", [starts[i] for i in order]), array("q", [lens[i] for i in order]), array("l", [ridx[i] for i in order]))
        self.place = {w: {} for w in range(4)}      # rec idx -> list of (bucket, entry, count) in storage order
        self.placelog = []                           # (w, idx) appended when a record gets placed (for backtracking)
        self.how = {w: {} for w in range(4)}         # rec idx -> "tag" | "end-anchored" | "assumed": the evidence it was identified by
    def entries(self, b):
        e = self.cache.get(b)
        if e is None:
            e = bucket_index_entries(self.src.ensure(f"Pool/0/{b // 2048}/{self.names[b][0]}")); self.cache[b] = e
            if len(self.cache) > 3000: self.cache.popitem(last=False)
        return e
    def ulen_of_id(self, g):
        for w in range(4):
            st, ln, ri = self.rng[w]; i = bisect.bisect_right(st, g) - 1
            if i >= 0 and g < st[i] + ln[i]:
                r = self.W[w][ri[i]]; segs = self.place[w].get(r.idx)
                if segs is None: return None
                k = 0
                for a, L in r.ranges:
                    if a <= g < a + L: k += g - a; break
                    k += L
                for b, e0, c in segs:
                    if k < c: return self.entries(b)[e0 + k][2]
                    k -= c
                return None
        return None
    def tag_matches(self, ents, pos, r):
        cum = 0; j = pos; n = len(ents)
        while j < n and cum < r.tail: cum += ents[j][2]; j += 1
        if cum != r.tail: return False
        return hashlib.md5(b"".join(e[3][:16] for e in ents[pos:j])).hexdigest() == r.tag
    def try_assign(self, w, b, state):
        """state = (qi, deferred tuple, cur) with cur = None or (rec idx, consumed, running sum).
        Returns (new state, evidence weight, placements list) or (None, reason)."""
        recs = self.W[w]; qi, deferred, cur = state; deferred = list(deferred)
        ents = self.entries(b); c = len(ents); pos = 0; ev = 0; placements = []
        while pos < c:
            if cur is None:
                # which record starts here?
                chosen = None; tagged_checked = 0
                cands = [recs[i] for i in deferred] + [r for r in recs[qi:qi + LOOK] if r.n > 0 and r.idx not in self.place[w]]
                untagged = []
                for r in cands:
                    if r.tag:
                        tagged_checked += 1
                        if self.tag_matches(ents, pos, r): chosen = r; break
                    else: untagged.append(r)
                if chosen is None:
                    if not untagged: return None, f"no record matches at bucket {b} entry {pos} (checked {tagged_checked} tags)"
                    # end-anchored identification: prefer the untagged record after which a tagged record starts
                    for r in untagged[:12]:
                        e = pos + r.n
                        if e < c and any(x.tag and self.tag_matches(ents, e, x) for x in cands[:60]):
                            chosen = r; self.stats["untagged-end-anchored"] += 1; self.how[w][r.idx] = "end-anchored"; break
                    if chosen is None: chosen = untagged[0]; self.stats["untagged-assumed"] += 1; self.how[w][chosen.idx] = "assumed"   # a guess, exported for flagging
                else: ev += max(10, min(chosen.n, 5000)); self.stats["tag-match"] += 1; self.how[w][chosen.idx] = "tag"
                # records before it in the window (stream order) with tags that did not match are deferred
                for r in recs[qi:chosen.idx]:
                    if r.n > 0 and r.idx not in self.place[w] and r.idx not in deferred: deferred.append(r.idx)
                if chosen.idx in deferred: deferred.remove(chosen.idx)
                cur = (chosen.idx, 0, 0)
            r = recs[cur[0]]; consumed = cur[1]; run = cur[2]
            take = min(r.n - consumed, c - pos)
            run += sum(e[2] for e in ents[pos:pos + take]); placements.append((r.idx, b, pos, take)); consumed += take; pos += take
            if consumed == r.n:
                if r.size is not None:
                    extra = 0; ok = True
                    for g in r.dedup:
                        ul = self.ulen_of_id(g)
                        if ul is None:
                            ok = False; break
                        extra += ul
                    if ok and run + extra != r.size: self.stats["reject"] += 1; return None, f"size mismatch rec {r.off:#x}: got {run + extra} want {r.size}"
                    if ok: ev += r.n if r.n >= 3 else 0; self.stats["verified"] += 1
                    else: self.stats["unverifiable"] += 1
                cur = None
                # advance queue head past placed/deferred... placement is applied by caller; mark temporarily
                self._mark(w, r.idx, placements)
                while qi < len(recs) and (recs[qi].n == 0 or recs[qi].idx in self.place[w]): qi += 1
            else:
                cur = (r.idx, consumed, run)
        return (qi, tuple(deferred), cur), ev, placements
    def _mark(self, w, idx, placements):
        segs = [(b, e, cnt) for (i, b, e, cnt) in placements if i == idx]
        prev = self.place[w].get(idx)
        self.place[w][idx] = (prev or []) + segs; self.placelog.append((w, idx, len(prev or [])))
    def _apply(self, w, b, ns, placements, state, result):
        # placements of records not yet complete must also be recorded (partial)
        for (i, bb, e, cnt) in placements:
            if i not in self.place[w] or not any(x == (bb, e, cnt) for x in self.place[w][i]):
                self.place[w].setdefault(i, []).append((bb, e, cnt)); self.placelog.append((w, i, len(self.place[w][i]) - 1))
        state[w] = ns; result[b] = w
    def _undo(self, loglen):
        while len(self.placelog) > loglen:
            w, i, prevlen = self.placelog.pop()
            segs = self.place[w].get(i)
            if segs is None: continue
            del segs[prevlen:]
            if not segs: del self.place[w][i]
    def run(self, max_bucket=None, window=40, budget=4000):
        """Forward assignment with the rhythm prior; on a contradiction, a targeted repair tries adjacent bucket swaps
        in the recent window (the real perturbations are local), replaying the affected stretch."""
        buckets = sorted(b for b in self.counts if max_bucket is None or b <= max_bucket)
        self.place = {w: {} for w in range(4)}; self.placelog = []; self.how = {w: {} for w in range(4)}
        state = {w: (0, (), None) for w in range(4)}; result = {}; snaps = {}
        i = 0; t0 = time.time(); last_log = 0; last_t = time.time(); repairs = 0; perturb = 0
        while i < len(buckets):
            b = buckets[i]; snaps[i] = (dict(state), len(self.placelog)); snaps.pop(i - window - 1, None)
            cands, reasons = self._cands(b, state)
            pred = result.get(b - 1) if b % 4 != 0 else result.get(b - 16)
            chosen = self._pick(cands, pred)
            if pred is not None and (chosen is None or chosen[0] != pred) and pred in reasons:
                # contradiction for the expected writer: try a local repair, then re-evaluate this bucket
                if self._repair(buckets, i, pred, window, state, result, snaps):
                    repairs += 1; continue
                perturb += 1
            if chosen is None:
                # last resort: repair for any writer rejected by a size mismatch
                fixed = False
                for w in reasons:
                    if self._repair(buckets, i, w, window, state, result, snaps): fixed = True; repairs += 1; break
                if fixed: continue
                self.log(f"  stuck at bucket {b} (index {i}): {reasons}"); return result, i
            w, ns, ev, pl = chosen
            self._apply(w, b, ns, pl, state, result); i += 1
            if i - last_log >= 1000 or time.time() - last_t >= 2: last_log = i; last_t = time.time(); self.log(f"  mapped {i}/{len(buckets)} buckets, repairs={repairs}, perturbations={perturb}, {time.time()-t0:.0f}s, {dict(self.stats)}")
        self.log(f"done: {len(result)} buckets, repairs={repairs}, perturbations accepted={perturb}")
        return result, i
    def probe(self, w, b, st):
        """try_assign without side effects: the placements it marks while probing are undone again."""
        lg = len(self.placelog)
        try: return self.try_assign(w, b, st)
        finally: self._undo(lg)
    def _cands(self, b, state):
        cands = []; reasons = {}
        for w in range(4):
            r = self.probe(w, b, state[w])
            if r[0] is not None: cands.append((w, r[0], r[1], r[2]))
            else: reasons[w] = r[1]
        return cands, reasons
    def _pick(self, cands, pred):
        if not cands: return None
        strong = [c for c in cands if c[2] > 0]
        if strong: return max(strong, key=lambda c: c[2])
        for c in cands:
            if c[0] == pred: return c
        return cands[0]
    def _replay(self, buckets, k, i, assign, state, result):
        """Re-run buckets k..i-1 with forced writers assign[j]; returns True if every step passes."""
        for j in range(k, i):
            w = assign[j]; r = self.try_assign(w, buckets[j], state[w])
            if r[0] is None: return False
            self._apply(w, buckets[j], r[0], r[2], state, result)
        return True
    def _repair(self, buckets, i, w, window, state, result, snaps):
        lo = max(0, i - window); old = {j: result[buckets[j]] for j in range(lo, i)}
        mine = [j for j in range(lo, i) if old[j] == w][::-1]
        for x in mine:
            for y in range(x - 6, x + 7):
                if y < lo or y >= i or y == x or old[y] == w: continue
                k = min(x, y)
                if k not in snaps: continue
                saved_state, saved_log = snaps[k]
                st = dict(saved_state); self._undo(saved_log); res = dict(result)
                for j in range(k, i): res.pop(buckets[j], None)
                assign = dict(old); assign[x], assign[y] = old[y], w
                ok = self._replay(buckets, k, i, assign, st, res)
                if ok: ok = self.probe(w, buckets[i], st[w])[0] is not None     # the probe's placements must not leak into self.place
                if ok:
                    state.clear(); state.update(st); result.clear(); result.update(res)
                    for j in range(k, i): snaps.pop(j, None)
                    self.stats["repair-swap"] += 1
                    return True
                # restore
                self._undo(saved_log)
                st2 = dict(saved_state); res2 = dict(result)
                for j in range(k, i): res2.pop(buckets[j], None)
                if not self._replay(buckets, k, i, old, st2, res2):
                    raise RuntimeError("replay of the original assignment failed")
                state.clear(); state.update(st2); result.clear(); result.update(res2)
        return False
    def export(self, result):
        out = {"buckets": {str(b): w for b, w in result.items()}, "counts": {str(b): self.counts[b] for b in result},
               "placements": {}, "ranges": {}, "guessed": {}, "stats": dict(self.stats)}
        for w in range(4):
            s = SET_OF_WRITER[w]; recs = self.W[w]
            out["placements"][str(s)] = {str(recs[i].off): segs for i, segs in self.place[w].items()}
            out["ranges"][str(s)] = [[a, L, r.off] for r in recs for a, L in r.ranges]
            out["guessed"][str(s)] = sorted(recs[i].off for i in self.place[w] if self.how[w].get(i) == "assumed")   # placed with no tag evidence
        return out

def build_poolmap2(source, outdir, log=print, max_bucket=None):
    listing = load_json(os.path.join(outdir, "listing.json"))
    writers = build_writers(outdir, log)
    mp = Mapper2(source, outdir, listing, writers, log)
    result, upto = mp.run(max_bucket=max_bucket)
    out = mp.export(result); out["mapped_upto"] = upto
    dump_json(os.path.join(outdir, "poolmap2.json"), out)
    return result, upto, mp
