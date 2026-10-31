"""Phase 2 (v3) of the anchor-first pool map: from the anchors (tagged records located in the bucket indexes) to a
verified placement of every record it can account for.

Three facts about the pool drive the design (measured on the real backup, docs/handovers/2026-10-31-1305-claude-hbk-1.md):
* Content dedup leaves phantom records: a file of two or more chunks that is backed up again gets fresh chunk ids but
  nothing is appended to any bucket. Later copies of a tag (more records than positions) store nothing and alias the
  first copy; a record whose ids did not exist yet at its bucket's revision is a copy of an untagged original and only
  its content position is known.
* Bucket ids are reused; the index revision is the clock. Time order of buckets is (revision, bucket id).
* With phantoms removed, a writer's stream offset pins the first entry of every anchored bucket, and a record's own
  (non-dedup) bytes equal the ulen sum of its entries. Chunks are variable-sized, so that sum verifies a placement.

The placer: anchored records that fit their bucket are placed and verified; a record that spills over claims the
anchorless buckets between its head and the next anchored record of its writer by an exact match of both chunk count
and byte sum (only a unique solution is accepted); records between two placed anchors are laid out when the entries
match exactly and verified where their size is known; phantoms are aliased; small records with several candidate
positions take one whose bytes match. Everything placed without a byte check is flagged (`guessed`), copies as well
(`aliased`).
"""
import os, bisect, collections, struct, time
from array import array
from callido_restore.pool.anchor import _rss_mb, PARENT_RSS_MB
from callido_restore.pool.poolmap2 import TAGK

SEARCH_NODES = 20000         # DFS budget per claim search; over budget counts as ambiguous
MAX_CANDIDATES = 6000        # widest search window (a whole revision band)
MULTI_MAX = 64               # a tag with more positions than this is too common to resolve
CLOCK_PERCENTILE = 0.95      # a revision's "max id" is this percentile of the ids anchored in its buckets (outliers are copies)
CLOCK_SLACK = 1.02           # ... and a record counts as too new for a bucket beyond that times this, plus CLOCK_MARGIN ids
CLOCK_MARGIN = 1000
PHANTOM_TRIES = 6            # untagged records of a gap tried as "stored nothing" when the gap has no exact solution
PERIOD_WINDOW, PERIOD_MAX, PERIOD_MIN_PAIRS, PERIOD_AGREE, PERIOD_STEPS = 400, 40, 30, 0.95, 6   # owner-sequence period detection
QUAD_CYCLES = (4, 3, 2)      # writers taking quads in turn: four, three or two of them active
ROUNDS = 6                   # placement rounds: dedup references resolve as the records they point at get placed


class Index:
    """Bucket index access for the placer: entries per bucket (``counts``), index revision (``revs``), the ulen total per
    bucket (``ubytes``, from the scan when it has them, else read on demand) and ulen prefix sums per bucket (read on
    demand through ``path_of(b)``, kept in a small LRU)."""
    def __init__(self, counts, revs, path_of, ubytes=None, lru=4000):
        self.counts, self.revs, self.path_of = counts, revs, path_of
        self.ubytes = dict(ubytes or {}); self._pref = collections.OrderedDict(); self.lru = lru
    def prefix(self, b):
        p = self._pref.get(b)
        if p is None:
            with open(self.path_of(b), "rb") as f: d = f.read()
            n = self.counts[b]; p = array("q", [0]); c = 0
            for (ulen,) in struct.iter_unpack(">8xI20x", d[64:64 + n * 32]): c += ulen; p.append(c)
            self._pref[b] = p; self.ubytes.setdefault(b, p[-1])
            if len(self._pref) > self.lru: self._pref.popitem(last=False)
        else: self._pref.move_to_end(b)
        return p
    def total(self, b):
        v = self.ubytes.get(b)
        return v if v is not None else self.prefix(b)[-1]
    def seg_bytes(self, segs):
        t = 0
        for b, e, n in segs: p = self.prefix(b); t += p[e + n] - p[e]
        return t


def _group_by_tag(anchors):
    """{(w, ri): positions} with one shared list per tag -> [(positions, [(w, ri), ...])]"""
    groups = {}
    for k, v in anchors.items():
        g = groups.get(id(v))
        if g is None: g = groups[id(v)] = (v, [])
        g[1].append(k)
    return list(groups.values())


def _claim_search(cands, need_c, need_b, index=None, tail=False, budget=SEARCH_NODES):
    """A subset of cands (in time order, each (key, count, bytes)) whose counts sum to need_c and, when need_b is not
    None, whose bytes sum to need_b. tail=True allows the last chosen bucket to be partial (a prefix with the exact
    remaining count and bytes; keys are then buckets). Include-first DFS with suffix-sum pruning.
    Returns (keys, partial tail count or None, solutions found (stops at 2), over budget)."""
    n = len(cands); sc = [0] * (n + 1); sb = [0] * (n + 1)
    for i in range(n - 1, -1, -1): sc[i] = sc[i + 1] + cands[i][1]; sb[i] = sb[i + 1] + cands[i][2]
    sol = []; nodes = 0; found = 0; first = None
    stack = [(0, need_c, need_b, False)]          # (i, remaining count, remaining bytes, True = leaving: pop the choice made at i)
    while stack:
        i, nc, nb, leaving = stack.pop()
        if leaving: sol.pop(); continue
        if nc == 0 and (nb is None or nb == 0):
            found += 1
            if first is None: first = (list(sol), None)
            if found >= 2: break
            continue
        if i == n or nc < 0 or (nb is not None and nb < 0): continue
        if not tail and (nc > sc[i] or (nb is not None and nb > sb[i])): continue
        nodes += 1
        if nodes > budget: break
        key, c, by = cands[i]
        if tail and 0 < nc < c and (nb is None or index.prefix(key)[nc] == nb):
            found += 1
            if first is None: first = (list(sol) + [key], nc)
            if found >= 2: break
        # explore "take cands[i]" first (a contiguous run is the common case), then "skip it"
        stack.append((i + 1, nc, nb, False))
        stack.append((i + 1, nc, nb, True)); sol.append(key); stack.append((i + 1, nc - c, None if nb is None else nb - by, False))
    if first is None: return None, None, 0, nodes > budget
    return first[0], first[1], found, nodes > budget


def resolve_and_place_v3(writers, anchors, index, log=print, max_bucket=None):
    """writers: {w: [Rec]} (poolmap2.build_writers); anchors: {(w, ri): [(bucket, entry, K), ...]} with one shared list
    per tag (anchor.scan_anchors / load_anchors); index: Index. Returns (owner {bucket: w}, placements {w: {ri: [(b, e, n)]}},
    report Counter, flags {"guessed": set((w, ri)), "aliased": set((w, ri))})."""
    t0 = time.time(); R = collections.Counter(); W = writers; counts = index.counts; rev = index.revs
    def guard(phase):                             # H9: the placer runs in the mapper child next to the writer tables; never past the scan's limit
        R["peak-rss-mb"] = max(R["peak-rss-mb"], int(_rss_mb()))
        if _rss_mb() > PARENT_RSS_MB: raise MemoryError(f"placer RSS {_rss_mb():.0f} MB > {PARENT_RSS_MB} MB after {phase}; aborted")
    buckets = sorted(b for b in counts if max_bucket is None or b <= max_bucket); bset = set(buckets)
    torder = sorted(buckets, key=lambda b: (rev.get(b, 0), b)); tpos = {b: i for i, b in enumerate(torder)}
    flags = {"guessed": set(), "aliased": {}}        # aliased: (w, ri) -> the stored record it copies, None for a copy of an untagged original
    def first_id(w, ri): r = W[w][ri]; return r.ranges[0][0] if r.ranges else -1
    # ---- A. phantoms. Copies of a tag are ordered by first chunk id; the first len(positions) copies are the stored ones.
    groups = []
    for pl, rl in _group_by_tag(anchors):
        pl = sorted(p for p in pl if p[0] in bset)
        if pl: groups.append((pl, sorted((first_id(w, ri), w, ri) for w, ri in rl)))
    # A tag the scan found nowhere (its first chunks straddle a bucket boundary, which the scan does not follow) still says
    # which records are copies: a file of two or more chunks backed up again stores nothing, so only the earliest record can
    # be the stored one; the later ones alias it (it is laid out by the gap pass and verified by size). Such records are not
    # in ``anchors`` at all, so they are grouped from the writer tables.
    notag = collections.defaultdict(list)
    for w in W:
        for r in W[w]:
            # only records the scan looked for: their first chunks are their own (a later version whose head is dedup
            # references shares the tag of the version before it but stores its changed chunks; it is not a copy)
            if r.tag and r.n > 1 and r.head_new >= min(r.n + r.nd, TAGK) and (w, r.idx) not in anchors: notag[r.tag].append((first_id(w, r.idx), w, r.idx))
    for tag, rl in notag.items():
        if len(rl) > 1: groups.append(([], sorted(rl)))
    byrev = collections.defaultdict(list)
    for pl, rl in groups:
        if len(pl) == 1: byrev[rev.get(pl[0][0], 0)].append(rl[0][0])
    env = {}; run = 0
    for r_ in sorted(byrev):
        gs = sorted(byrev[r_]); run = max(run, gs[int(len(gs) * CLOCK_PERCENTILE)]); env[r_] = run
    revs_sorted = sorted(env)
    def maxid_at(r_):
        k = bisect.bisect_right(revs_sorted, r_) - 1; return env[revs_sorted[k]] if k >= 0 else 0
    def too_new(g, b): return g > maxid_at(rev.get(b, 0)) * CLOCK_SLACK + CLOCK_MARGIN      # ids beyond what existed at the bucket's revision
    phys = {w: [r.n for r in W[w]] for w in W}; alias = {}; found = {}; cand = {}
    for pl, rl in groups:
        stored = rl[:max(1, len(pl))] if len(pl) < len(rl) else rl
        for g, w, ri in rl[len(stored):]: phys[w][ri] = 0; alias[(w, ri)] = (stored[0][1], stored[0][2])
        if not pl: R["tag-without-position"] += 1; continue
        for g, w, ri in stored:
            if len(pl) == 1 and too_new(g, pl[0][0]): phys[w][ri] = 0; found[(w, ri)] = (pl[0][0], pl[0][1])
            else: cand[(w, ri)] = pl
    R["phantom-of-tagged"] = len(alias); R["phantom-of-untagged"] = len(found)
    P = {}
    for w in W:
        c = 0; P[w] = []
        for r in W[w]: P[w].append(c); c += phys[w][r.idx]
    # ---- id -> (writer, record, chunk rank) and the bytes of a record's dedup references
    idx_st = {}; idx_len = {}; idx_ri = {}; idx_rank = {}
    for w in W:
        st = array("q"); ln = array("q"); rr = array("l"); rk = array("q")
        for r in W[w]:
            k = 0
            for s_, L in r.ranges: st.append(s_); ln.append(L); rr.append(r.idx); rk.append(k); k += L
        idx_st[w], idx_len[w], idx_ri[w], idx_rank[w] = st, ln, rr, rk
    placements = {w: {} for w in W}
    def run_of_id(g):
        """(w, ri, rank, ids left in the record's range from g)"""
        for w in W:
            i = bisect.bisect_right(idx_st[w], g) - 1
            if i >= 0 and g < idx_st[w][i] + idx_len[w][i]: return w, idx_ri[w][i], idx_rank[w][i] + (g - idx_st[w][i]), idx_st[w][i] + idx_len[w][i] - g
        return None
    def chunk_bytes(w, ri, k, m):
        """bytes of chunks k..k+m-1 of a placed record, None when not placed"""
        segs = placements[w].get(ri)
        if segs is None: return None
        tot = 0
        for b, e, n in segs:
            if k < n:
                t = min(m, n - k); p = index.prefix(b); tot += p[e + k + t] - p[e + k]; m -= t; k = 0
                if m == 0: return tot
            else: k -= n
        return None
    dcache = {}
    def own_bytes(w, ri):
        """what the record's own entries must sum to: size minus the bytes of its dedup references; None if unknown"""
        r = W[w][ri]
        if r.size is None: return None
        if not r.dedup: return r.size
        v = dcache.get((w, ri))
        if v is None:
            v = 0
            for s_, L in r.dedup:
                g = s_
                while L > 0:
                    x = run_of_id(g)
                    if x is None: return None
                    w2, ri2, k, left = x; m = min(L, left); by = chunk_bytes(w2, ri2, k, m)
                    if by is None: return None
                    v += by; g += m; L -= m
            dcache[(w, ri)] = v
        return r.size - v
    # ---- B. anchors with one candidate; ownership by majority; chain per writer in time order (for multi-position records)
    anchor = {k: (pl[0][0], pl[0][1]) for k, pl in cand.items() if len(pl) == 1}
    def owners_from(anchor):
        votes = collections.defaultdict(collections.Counter); own = {}; conf = 0
        for (w, ri), (b, e) in anchor.items(): votes[b][w] += 1
        for b, c in votes.items():
            (w1, n1), = c.most_common(1)
            if len(c) > 1 and n1 < 2 * (sum(c.values()) - n1): conf += 1; continue
            own[b] = w1
        return own, conf
    def chains(anchor, owner):
        pins = {w: collections.defaultdict(collections.Counter) for w in W}; out = {}
        for (w, ri), (b, e) in anchor.items():
            if owner.get(b) == w: pins[w][b][P[w][ri] - e] += 1
        for w in W:
            seq = sorted((tpos[b], b, c.most_common(1)[0][0]) for b, c in pins[w].items())
            tails = []; tidx = []; prev = [-1] * len(seq)
            for i, (t, b, s_) in enumerate(seq):
                k = bisect.bisect_left(tails, s_)
                if k == len(tails): tails.append(s_); tidx.append(i)
                else: tails[k] = s_; tidx[k] = i
                prev[i] = tidx[k - 1] if k > 0 else -1
            ch = []; i = tidx[-1] if tidx else -1
            while i >= 0: ch.append(seq[i]); i = prev[i]
            ch = ch[::-1]; ok = []
            for t, b, s_ in ch:
                if ok and s_ - ok[-1][2] < counts[ok[-1][1]]: continue
                ok.append((t, b, s_))
            out[w] = ok
        return out
    owner, conf = owners_from(anchor); chain = chains(anchor, owner)
    for it in range(2):
        CT = {w: [t for t, b, s_ in chain[w]] for w in W}; CB = {w: [b for t, b, s_ in chain[w]] for w in W}; CS = {w: [s_ for t, b, s_ in chain[w]] for w in W}
        for (w, ri), pl in cand.items():
            if (w, ri) in anchor or len(pl) > MULTI_MAX: continue
            good = []
            for b, e, K in pl:
                if owner.get(b, w) != w or too_new(first_id(w, ri), b): continue
                s_ = P[w][ri] - e; k = bisect.bisect_left(CT[w], tpos[b])
                if k < len(CT[w]) and CT[w][k] == tpos[b]: ok = CS[w][k] == s_
                else: ok = (k == 0 or CS[w][k - 1] + counts[CB[w][k - 1]] <= s_) and (k == len(CT[w]) or s_ + counts[b] <= CS[w][k])
                if ok: good.append((b, e))
            if len(good) == 1: anchor[(w, ri)] = good[0]
        owner, conf = owners_from(anchor); chain = chains(anchor, owner)
    # An anchor in a bucket of an older revision than the writer's previous pin (by stream offset) cannot be the stored copy: the
    # record is a copy of an untagged original the revision clock could not tell (a single record anchored in a rotated-away
    # revision sets its own clock). It becomes a copy laid out from its found position; its chunks leave the stream offsets.
    for rnd in range(3):
        demoted = 0
        for w in W:
            ts = [t for t, b, s_ in chain[w]]; ss = [s_ for t, b, s_ in chain[w]]; cb = {b for t, b, s_ in chain[w]}
            for (ww, ri), (b, e) in list(anchor.items()):
                if ww != w or b in cb: continue
                k = bisect.bisect_left(ss, P[w][ri] - e)
                if k > 0 and rev.get(b, 0) < rev.get(torder[ts[k - 1]], 0):
                    del anchor[(w, ri)]; found[(w, ri)] = (b, e); phys[w][ri] = 0; demoted += 1
        if not demoted: break
        R["anchor-behind-demoted"] += demoted
        for w in W:
            c = 0; P[w] = []
            for r in W[w]: P[w].append(c); c += phys[w][r.idx]
        owner, conf = owners_from(anchor); chain = chains(anchor, owner)
    R["owner-conflicts"] = conf; R["owned-by-anchors"] = len(owner)
    guard("pins")
    log(f"placer: {len(anchor):,} anchored records, {len(owner):,} buckets owned by anchors, {len(alias):,} copies, {len(found):,} copies of untagged originals, {time.time() - t0:.0f}s")
    # ---- C. claims. Anchored records in time order of their anchor; a spill claims the anchorless buckets between its
    # head and the next anchored record of its writer by exact count and bytes (unique solutions only).
    claimed = dict(owner)
    free_t = [tpos[b] for b in torder if b not in owner]
    rev_first = {}; rev_last = {}
    for b in torder: rev_first.setdefault(rev.get(b, 0), tpos[b]); rev_last[rev.get(b, 0)] = tpos[b]
    def window(b, bB):
        lo = bisect.bisect_right(free_t, tpos[b]); hi = bisect.bisect_left(free_t, tpos[bB]) if bB is not None else len(free_t)
        return [torder[x] for x in free_t[lo:hi] if torder[x] not in claimed]
    def band(b, bB):
        lo = bisect.bisect_left(free_t, rev_first[rev.get(b, 0)]); hi = bisect.bisect_right(free_t, rev_last[rev.get(bB, 0)])
        return [torder[x] for x in free_t[lo:hi] if torder[x] not in claimed]
    qa = collections.defaultdict(lambda: [0, 0])
    for b, w in owner.items():
        sib = [owner[x] for x in range(b // 4 * 4, b // 4 * 4 + 4) if x in owner and x != b]
        if sib: qa[rev.get(b, 0)][all(x == w for x in sib)] += 1
    quad_rev = {r_: v[True] >= 20 and v[True] >= 9 * v[False] for r_, v in qa.items()}      # the initial backup hands out bucket ids four at a time
    def quad_units(b, bB):
        must = [x for x in range(b + 1, b // 4 * 4 + 4) if x in counts and x not in claimed] + [x for x in range(bB // 4 * 4, bB) if x in counts and x not in claimed]
        units = []
        for q in range(b // 4 + 1, bB // 4):
            xs = [x for x in range(q * 4, q * 4 + 4) if x in counts]
            if xs and all(x not in claimed for x in xs): units.append((xs, sum(counts[x] for x in xs), sum(index.total(x) for x in xs)))
        return must, units
    nxt_anchor = {}
    for w in W:
        mine = sorted(ri for (ww, ri) in anchor if ww == w)
        for a1, a2 in zip(mine, mine[1:]): nxt_anchor[(w, a1)] = a2
    period_cache = {}
    def local_period(b):
        """The repeat period of the owner sequence along bucket ids near b (writers take id batches in a fixed turn while
        they are all busy, e.g. 0000111122223333 or 0 11 2222 11 0000): the smallest P whose known owner pairs (x, x+P)
        within PERIOD_WINDOW ids agree in PERIOD_AGREE of at least PERIOD_MIN_PAIRS cases; None when there is no such P."""
        key = b // PERIOD_WINDOW
        if key in period_cache: return period_cache[key]
        lo, hi = max(0, b - PERIOD_WINDOW), b + PERIOD_WINDOW; best = None
        for P_ in range(2, PERIOD_MAX + 1):
            agree = n = 0
            for x in range(lo, hi):
                a1 = claimed.get(x); a2 = claimed.get(x + P_)
                if isinstance(a1, int) and isinstance(a2, int) and rev.get(x) == rev.get(x + P_): n += 1; agree += a1 == a2
            if n >= PERIOD_MIN_PAIRS and agree >= PERIOD_AGREE * n: best = P_; break
        period_cache[key] = best; return best
    def predicted_owner(x, P_):
        """owner of x by the period: the nearest known owner at x +- k*P_ (same revision)"""
        for k in range(1, PERIOD_STEPS + 1):
            for y in (x - k * P_, x + k * P_):
                o = claimed.get(y)
                if isinstance(o, int) and rev.get(y) == rev.get(x): return o
        return None
    def search(b, bB, need_c, need_b):
        cands = [(x, counts[x], index.total(x)) for x in window(b, bB)]
        P_ = local_period(b) if bB is not None else None
        w_ = claimed.get(b)
        if P_ and isinstance(w_, int):
            # Unique within the turn-predicted subset, not over every candidate: "verified" for such a spill means its size
            # matches exactly under the turn model; a wrong pick would still fail the per-chunk md5 check when it is read.
            sub = [c for c in cands if predicted_owner(c[0], P_) == w_]
            if sub and (need_c or need_b):
                us, _, nsol, over = _claim_search(sub, need_c, need_b)
                if us is not None and nsol == 1 and not over: R["spill-by-period"] += 1; return us, 1, False
        if quad_rev.get(rev.get(b, 0)) and rev.get(bB) == rev.get(b) and bB > b:
            must, units = quad_units(b, bB); nc = need_c - sum(counts[x] for x in must); nb = need_b - sum(index.total(x) for x in must)
            if nc == 0 and nb == 0: return must, 1, False
            if nc > 0 and nb > 0:
                # The writers take quads in turn, so a writer's quads repeat every `cyc` quads (cyc = the writers active then).
                # Try the head's place in that cycle first: a small exhaustive search; only a unique exact count-and-bytes
                # match is accepted. Then every free quad between (the general search, budget-bound).
                for cyc in QUAD_CYCLES:
                    sub = [(k, c, by) for k, (xs, c, by) in enumerate(units) if (xs[0] // 4 - b // 4) % cyc == 0]
                    us, _, nsol, over = _claim_search(sub, nc, nb)
                    if us is not None and nsol == 1 and not over: R[f"spill-quad-cycle-{cyc}"] += 1; return must + [x for k in us for x in units[k][0]], 1, False
                us, _, nsol, over = _claim_search([(k, c, by) for k, (xs, c, by) in enumerate(units)], nc, nb)
                if us is not None: return must + [x for k in us for x in units[k][0]], nsol, over
        if need_c == 0 and need_b == 0: return [], 1, False
        sol, _, nsol, over = _claim_search(cands, need_c, need_b)
        if sol is None:
            c2 = [(x, counts[x], index.total(x)) for x in band(b, bB)]
            if len(cands) < len(c2) <= MAX_CANDIDATES: sol, _, nsol, over = _claim_search(c2, need_c, need_b)
        return sol, nsol, over
    deferred = []
    def place_anchored(w, ri, b, e, final=False):
        n = phys[w][ri]; size = W[w][ri].size
        if claimed.get(b) != w: R["anchor-bucket-not-ours"] += 1; return
        head = min(n, counts[b] - e); segs = [(b, e, head)]; rem = n - head
        ob = own_bytes(w, ri)
        if size is not None and ob is None:
            if not final: deferred.append((w, ri, b, e)); return
            R["dedup-unresolved"] += 1; return
        if rem == 0:
            if ob is not None and index.seg_bytes(segs) != ob: R["size-mismatch"] += 1; return
            placements[w][ri] = segs; R["placed-in-bucket"] += 1
            if ob is None: flags["guessed"].add((w, ri))
            return
        riB = nxt_anchor.get((w, ri)); bB, eB = anchor[(w, riB)] if riB else (None, None)
        if ob is None: R["spill-unsized"] += 1; return
        if riB is None or tpos[bB] <= tpos[b]:
            # the writer's last spilling record (or its next anchor lies behind): no tail pin, so the interior and the partial
            # tail bucket are searched together, like a copy of an untagged original (exact count and bytes, unique only)
            between = [x for x in range(ri + 1, len(W[w])) if phys[w][x] > 0] if riB is None else []
            if between: R["spill-tail-with-between"] += 1; return
            cands = [(x, counts[x], index.total(x)) for x in window(b, None) if rev.get(x, 0) <= rev.get(b, 0) + 2][:MAX_CANDIDATES // 2]
            sol, tail_e, nsol, over = _claim_search(cands, rem, ob - index.seg_bytes(segs), index=index, tail=True)
            if sol is None: R["spill-tail-no-solution"] += 1; return
            if nsol > 1 or over: R["spill-ambiguous"] += 1; return
            for x in sol: claimed[x] = w
            for k, x in enumerate(sorted(sol, key=tpos.get)): segs.append((x, 0, tail_e if (k == len(sol) - 1 and tail_e is not None) else counts[x]))
            placements[w][ri] = segs; R["placed-spill-tail"] += 1; R["claimed"] += len(sol); return
        between = [x for x in range(ri + 1, riB) if phys[w][x] > 0 and (w, x) not in anchor]
        obs = [own_bytes(w, x) for x in between]
        if any(o is None for o in obs) and any(W[w][x].size is not None for x in between):
            if not final: deferred.append((w, ri, b, e)); return
            R["spill-between-unresolved"] += 1; return
        if any(o is None for o in obs): R["spill-deferred-to-gap"] += 1; return        # untagged records between: the gap pass (count-only) handles them
        # the records between (tagged but never anchored: their first chunks straddle a bucket boundary, or are dedup
        # references) are laid out with the spill; their sizes join the exact count-and-bytes match and verify each of them
        need_c = rem + sum(phys[w][x] for x in between) - eB; need_b = ob - index.seg_bytes(segs) + sum(obs) - index.prefix(bB)[eB]
        if need_c < 0 or need_b < 0: R["spill-negative"] += 1; return
        sol, nsol, over = search(b, bB, need_c, need_b)
        if sol is None: R["spill-no-solution"] += 1; return
        if nsol > 1 or over: R["spill-ambiguous"] += 1; return
        path = [(x, counts[x]) for x in sorted(sol, key=tpos.get)] + ([(bB, eB)] if eB else [])
        layout = {}; cur = list(segs); n_left = 0; i = 0; ce = 0; ok = True
        for x in [ri] + between:                                   # consecutive along the claimed buckets
            m = phys[w][x] if x != ri else rem; sg = cur if x == ri else []
            while m > 0 and i < len(path):
                cb, room_end = path[i]; t = min(m, room_end - ce)
                if t > 0: sg.append((cb, ce, t)); m -= t; ce += t
                if ce >= room_end: i += 1; ce = 0
            if m: ok = False; break
            layout[x] = sg
        if not ok: R["spill-layout-mismatch"] += 1; return
        for x in between:
            if index.seg_bytes(layout[x]) != own_bytes(w, x): ok = False; break
        if not ok: R["spill-between-size-mismatch"] += 1; return
        for x in sol: claimed[x] = w
        for x, sg in layout.items(): placements[w][x] = sg
        R["placed-spill"] += 1; R["placed-with-spill"] += len(between); R["claimed"] += len(sol)
    order = sorted(((tpos[b], e), w, ri) for (w, ri), (b, e) in anchor.items())
    for (t, e), w, ri in order: place_anchored(w, ri, torder[t], e)
    # ---- D. gaps: records between two placed anchored records of a writer (untagged, tagged with dedup heads, or
    # anchored ones that could not spill) laid out along the writer's buckets when the entries match; verified by bytes.
    def slide(w, between, seq, sp, b0, e0, bB, eB, avail, need):
        """Lay out the records between two placed anchors when the writer's buckets there hold `avail - need` leftover entries
        too: in stream order, each record takes the start in its window [lower bound, avail - what the later records need] where
        the ulen sum of its entries equals its own bytes; it is placed only when that start is unique. Returns records placed."""
        path = []; cur, ce = b0, e0
        while len(path) < avail:
            room = (eB if cur == bB else counts[cur]); p = index.prefix(cur)
            for e in range(ce, room): path.append((cur, e, p[e + 1] - p[e]))
            if cur == bB: break
            nxt = sp[cur] + 1
            if nxt >= len(seq): break
            cur = seq[nxt]; ce = 0
        if len(path) != avail: return 0
        cum = [0]
        for _, _, u in path: cum.append(cum[-1] + u)
        lo = 0; left = need; n_ok = 0
        for x in between:
            n = phys[w][x]; left -= n; ob = own_bytes(w, x)
            hi = avail - left - n                                           # the latest start that leaves room for the rest
            if ob is None: lo += n; continue
            starts = [s0 for s0 in range(lo, hi + 1) if cum[s0 + n] - cum[s0] == ob]
            if len(starts) != 1: lo = (starts[0] if starts else lo) + n; continue
            s0 = starts[0]; sg = []
            for b, e, _ in path[s0:s0 + n]:
                if sg and sg[-1][0] == b and sg[-1][1] + sg[-1][2] == e: sg[-1] = (b, sg[-1][1], sg[-1][2] + 1)
                else: sg.append((b, e, 1))
            placements[w][x] = sg; n_ok += 1; lo = s0 + n
        return n_ok
    def gap_pass():
        placed_n = 0
        seqs = {w: [b for b in torder if claimed.get(b) == w] for w in W}; spos = {w: {b: i for i, b in enumerate(seqs[w])} for w in W}
        for w in W:
            mine = sorted(ri for (ww, ri) in anchor if ww == w and ri in placements[w])
            for ri, riB in zip(mine, mine[1:]):
                between = [x for x in range(ri + 1, riB) if phys[w][x] > 0 and x not in placements[w]]
                if not between or any(x in placements[w] for x in range(ri + 1, riB)): continue
                segs = placements[w][ri]; b0, e0, n0 = segs[-1]; bB, eB = anchor[(w, riB)]
                if b0 not in spos[w] or bB not in spos[w]: continue
                i0, i1 = spos[w][b0], spos[w][bB]
                if (i1, eB) < (i0, e0 + n0): continue
                avail = (eB - (e0 + n0)) if i0 == i1 else (counts[b0] - (e0 + n0)) + sum(counts[seqs[w][k]] for k in range(i0 + 1, i1)) + eB
                need = sum(phys[w][x] for x in between)
                if avail != need:
                    if avail > need:
                        # leftover chunks of deleted files sit between records (buckets are never rewritten): slide each record
                        # to the position where its own bytes match exactly, the only one in its window
                        n_ok = slide(w, between, seqs[w], spos[w], b0, e0 + n0, bB, eB, avail, need)
                        placed_n += n_ok; R["gap-slid"] += n_ok
                        if not n_ok: R["gap-count-mismatch"] += 1
                        continue
                    obs = [own_bytes(w, x) for x in between]; known = not any(o is None for o in obs)
                    have_b = (index.prefix(b0)[counts[b0]] - index.prefix(b0)[e0 + n0]) + sum(index.total(seqs[w][k]) for k in range(i0 + 1, i1)) + index.prefix(bB)[eB] if i0 != i1 else index.prefix(bB)[eB] - index.prefix(b0)[e0 + n0]
                    win = window(b0, bB); nb = (sum(obs) - have_b) if known else None
                    sol, _, nsol, over = _claim_search([(x, counts[x], index.total(x)) for x in win], need - avail, nb)
                    if sol is None:
                        bd = band(b0, bB)
                        if len(win) < len(bd) <= MAX_CANDIDATES: win = bd; sol, _, nsol, over = _claim_search([(x, counts[x], index.total(x)) for x in win], need - avail, nb)
                    if sol is None:
                        for x in [y for y in between if W[w][y].tag is None][:PHANTOM_TRIES]:      # one untagged record may be a copy that stored nothing
                            s2, _, n2, o2 = _claim_search([(y, counts[y], index.total(y)) for y in win], need - avail - phys[w][x], None)
                            if s2 is not None and n2 == 1 and not o2:
                                phys[w][x] = 0; between.remove(x); need -= W[w][x].n; sol, nsol, over = s2, n2, o2; R["gap-phantom-untagged"] += 1; break
                    if sol is None: R["gap-no-solution"] += 1; continue
                    if nsol > 1 or over: R["gap-ambiguous"] += 1; continue
                    for x in sol: claimed[x] = w
                    seqs[w] = [b for b in torder if claimed.get(b) == w]; spos[w] = {b: i for i, b in enumerate(seqs[w])}
                    R["gap-claimed"] += len(sol)
                cur, ce = b0, e0 + n0; out = {}; ok = True
                for x in between:
                    m = phys[w][x]; sg = []
                    while m > 0:
                        room = (eB if cur == bB else counts[cur]) - ce; tk = min(m, room)
                        if tk > 0: sg.append((cur, ce, tk)); m -= tk; ce += tk
                        if m > 0: cur = seqs[w][spos[w][cur] + 1]; ce = 0
                    out[x] = sg
                for x, sg in out.items():
                    ob = own_bytes(w, x)
                    if ob is not None and index.seg_bytes(sg) != ob: ok = False; break
                if not ok: R["gap-size-mismatch"] += 1; continue
                for x, sg in out.items():
                    placements[w][x] = sg; placed_n += 1; R["placed-gap"] += 1
                    if own_bytes(w, x) is None: flags["guessed"].add((w, x))
        return placed_n
    for rnd in range(ROUNDS):
        todo, deferred = deferred, []; period_cache.clear()             # owners change between rounds
        for w, ri, b, e in todo: place_anchored(w, ri, b, e, final=(rnd == ROUNDS - 1))
        guard("claims")
        if not gap_pass() and not deferred: break
    # ---- E. copies of untagged originals: from the found position through the owner's buckets, else a tail-less search
    seqs = {w: [b for b in torder if claimed.get(b) == w] for w in W}; spos = {w: {b: i for i, b in enumerate(seqs[w])} for w in W}
    for (w, ri), (b, e) in sorted(found.items(), key=lambda kv: tpos[kv[1][0]]):
        n = W[w][ri].n; ob = own_bytes(w, ri)
        if ob is None: R["found-unsized"] += 1; continue
        head = min(n, counts[b] - e); segs = [(b, e, head)]; rem = n - head
        if rem == 0:
            if index.seg_bytes(segs) == ob: placements[w][ri] = segs; flags["aliased"][(w, ri)] = None; R["placed-older-copy"] += 1
            else: R["found-size-mismatch"] += 1
            continue
        ow = claimed.get(b)
        if isinstance(ow, int):
            i = spos[ow].get(b); m = rem
            while m > 0 and i is not None and i + 1 < len(seqs[ow]):
                i += 1; cb = seqs[ow][i]; tk = min(m, counts[cb]); segs.append((cb, 0, tk)); m -= tk
            if m == 0 and index.seg_bytes(segs) == ob: placements[w][ri] = segs; flags["aliased"][(w, ri)] = None; R["placed-older-copy"] += 1
            else: R["found-size-mismatch"] += 1
            continue
        cands = [(x, counts[x], index.total(x)) for x in window(b, None) if rev.get(x, 0) <= rev.get(b, 0) + 2][:MAX_CANDIDATES // 2]
        sol, tail_e, nsol, over = _claim_search(cands, rem, ob - index.seg_bytes(segs), index=index, tail=True)
        if sol is None: R["found-no-solution"] += 1; continue
        if nsol > 1 or over: R["found-ambiguous"] += 1; continue
        for x in sol: claimed[x] = ("?", b)          # someone's buckets, not reusable
        for k, x in enumerate(sorted(sol, key=tpos.get)):
            segs.append((x, 0, tail_e if (k == len(sol) - 1 and tail_e is not None) else counts[x]))
        placements[w][ri] = segs; flags["aliased"][(w, ri)] = None; R["placed-older-copy"] += 1
    # ---- F. small records with several candidate positions: any position whose bytes match (identical content when n <= TAGK)
    for (w, ri), pl in cand.items():
        if ri in placements[w] or len(pl) < 2 or len(pl) > MULTI_MAX: continue
        n = W[w][ri].n; ob = own_bytes(w, ri)
        ok = [(b, e) for b, e, K in pl if ob is not None and e + n <= counts[b] and index.prefix(b)[e + n] - index.prefix(b)[e] == ob]
        if ok:
            placements[w][ri] = [(ok[0][0], ok[0][1], n)]; R["placed-multi-by-size"] += 1
            if n > 9 and len(ok) > 1: flags["guessed"].add((w, ri))
        else: R["multi-unplaced"] += 1
    # ---- G0. records without chunks of their own (a later version whose every chunk is shared with earlier ones): nothing to
    # place; the reader takes each chunk from the record that stores it. Counted as placed (an empty segment list) when every
    # referenced chunk is placed and their bytes add up to the file size exactly.
    for w in W:
        for r in W[w]:
            if r.n or r.idx in placements[w] or r.size is None or not r.dedup: continue
            if own_bytes(w, r.idx) == 0: placements[w][r.idx] = []; R["placed-all-shared"] += 1
            else: R["all-shared-unresolved"] += 1
    # ---- G. copies alias their stored record
    for (w, ri), (w2, ri2) in alias.items():
        if ri2 in placements[w2] and W[w][ri].size == W[w2][ri2].size: placements[w][ri] = placements[w2][ri2]; flags["aliased"][(w, ri)] = (w2, ri2); R["aliased"] += 1
        else: R["alias-unplaced"] += 1
    guard("placement")
    owner_out = {b: w for b, w in claimed.items() if isinstance(w, int)}
    R["owned"] = len(owner_out); R["records"] = sum(len(W[w]) for w in W); R["placed"] = sum(len(p) for p in placements.values())
    sz_all = sum(r.size for w in W for r in W[w] if r.size); sz_pl = sum(W[w][ri].size for w in W for ri in placements[w] if W[w][ri].size)
    R["bytes-placed-pct"] = round(100 * sz_pl / sz_all, 1) if sz_all else 0          # a percentage 0-100 (the UI shows it as such)
    R["sized"] = sum(1 for w in W for r in W[w] if r.size is not None)               # file records the loaded snapshots describe
    R["placed-sized"] = sum(1 for w in W for ri in placements[w] if W[w][ri].size is not None)
    R["verified"] = sum(1 for w in W for ri in placements[w] if (w, ri) not in flags["guessed"] and (w, ri) not in flags["aliased"] and W[w][ri].size is not None)
    log(f"placer done: {R['placed']:,} of {R['records']:,} records placed ({R['bytes-placed-pct']} % of bytes, {R['verified']:,} verified by size), {R['owned']:,} of {len(buckets):,} buckets owned, {time.time() - t0:.0f}s")
    return owner_out, placements, R, flags
