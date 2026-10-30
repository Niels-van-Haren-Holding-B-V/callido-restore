"""Build the bucket -> writer map for a Hyper Backup pool (see hbk_pool.py for the format notes)."""
import os, json, struct, bisect, collections, time
from array import array
from hbk_pool import BLOCK, WRITER_OF_SET, SET_OF_WRITER, load_runs

class WriterModel:
    """One writer's stored chunk sequence = its file pieces (runs of newly stored chunks) in allocation order."""
    def __init__(self, w):
        self.w = w; self.blocks = []; self.pos = {}
        self.raw_start = array("q"); self.length = array("q"); self.stored_start = array("q"); self.rec = array("q")
        self.size = []; self.dedup = []
        self.total = 0

def _is_new(g, frontier):
    return g == frontier + 1 or (g % BLOCK == 0 and g > frontier)

def build_models(outdir, log=print):
    owner = {}; conflicts = 0; runs = {}
    for s in (1, 2, 3, 4):
        w = WRITER_OF_SET[s]; rec, g0, ln = load_runs(outdir, s); runs[w] = (rec, g0, ln)
        frontier = -1
        for i in range(len(g0)):
            g = g0[i]
            if _is_new(g, frontier):
                b0, b1 = g // BLOCK, (g + ln[i] - 1) // BLOCK
                for b in range(b0, b1 + 1):
                    if b in owner and owner[b] != w: conflicts += 1
                    else: owner[b] = w
                frontier = g + ln[i] - 1
    log(f"blocks owned: {len(owner):,} conflicts={conflicts}")
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
                g = g0[k]; L = ln[k]; blk = g // BLOCK
                if owner.get(blk) == w and _is_new(g, frontier):
                    p = m.pos[blk] * BLOCK + g % BLOCK
                    if cur is not None and p == cur[0] + cur[1]: cur[1] += L
                    else:
                        if cur is not None: pieces.append(cur)
                        cur = [p, L, r]
                    frontier = g + L - 1
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
    def try_assign(self, w, b, state):
        """state = (piece idx, offset in piece, running sum). Returns (new state, verified checks) or (None, reason)."""
        m = self.models[w]; pi, off, run = state
        u = self.ulens(b); c = len(u); k = 0; np_ = len(m.length); verified = 0
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
                        if ul is None: ok = False; break
                        extra += ul
                    if ok and run + extra != size: self.stats["reject"] += 1; return None, f"size mismatch piece {pi}: got {run + extra} want {size}"
                    if ok: self.stats["verified"] += 1; verified += 1
                if size is not None: run = 0
                pi += 1; off = 0
        return (pi, off, run), verified
    def run(self, max_bucket=None, budget=100000, verbose=False):
        buckets = sorted(b for b in self.counts if max_bucket is None or b <= max_bucket)
        state = {w: (0, 0, 0) for w in range(4)}; result = {}; stack = []
        i = 0; backtracks = 0; t0 = time.time(); last_log = 0
        while i < len(buckets):
            b = buckets[i]; cands = []; reasons = {}
            for w in range(4):
                ns, why = self.try_assign(w, b, state[w])
                if ns is not None: cands.append((w, ns, why))
                else: reasons[w] = why
            strong = [c for c in cands if c[2] > 0]
            if strong: cands = strong                      # positive evidence beats "not contradicted"
            cands.sort(key=lambda c: -c[2]); cands = [(c[0], c[1]) for c in cands]
            if not cands:
                if verbose: self.log(f"  bucket {b}: no candidate: {reasons}")
                while stack:
                    bi, alts, saved_state, saved_len = stack.pop()
                    state = dict(saved_state)
                    for w in range(4): del self.assigned[w][saved_len[w]:]
                    for bb in buckets[bi:i]: result.pop(bb, None)
                    i = bi; backtracks += 1
                    if alts:
                        w, ns = alts.pop(0)
                        if alts: stack.append((bi, alts, saved_state, saved_len))
                        self._apply(w, buckets[bi], ns, state, result); i += 1
                        break
                else:
                    self.log(f"STUCK at bucket {b} (index {i}) after {backtracks} backtracks: {reasons}"); return result, i
                if backtracks > budget: self.log("backtrack budget exhausted"); return result, i
                continue
            self.stats["cands%d" % len(cands)] += 1
            w, ns = cands[0]; alts = cands[1:]
            if alts: stack.append((i, alts, dict(state), {ww: len(self.assigned[ww]) for ww in range(4)}))
            self._apply(w, b, ns, state, result); i += 1
            if i - last_log >= 500: last_log = i; self.log(f"  mapped {i}/{len(buckets)} buckets, backtracks={backtracks}, open branches={len(stack)}, {time.time()-t0:.0f}s")
        self.log(f"done: {len(result)} buckets, backtracks={backtracks}, open branches={len(stack)}")
        return result, i
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
