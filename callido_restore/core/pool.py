"""
Hyper Backup data-pool decoding (reverse engineered, no client-side encryption, compression type 1 = LZ4).

Layout facts
------------
* version_list.off_virtual_file  < 0  -> Pool/file_pool/<-off>.file.<rev>: 0x140-byte header, then chunks
                                         [u16 0xe5bb][u32 clen][u32 ulen][u16 0][LZ4 block]
* off_virtual_file >= 0 -> byte offset into the virtual file index (Config/virtual_file.index/<n>.idx.*,
  8 MiB slices of one stream, 64-byte header once).  Entry = 56 bytes, 14 big-endian u32:
     [0..1] ptr A (u16 set | u48 offset) -> start of chunk list   [12..13] ptr B = A-12 -> record start
* record (file_chunk<set>.index stream, 8 MiB slices): [u32 flags][u32 0xa93b0200][u32 len][len bytes]
  body = refs (u64 each) + trailer [0x74000000][crc32 of refs].  ref = 64 + 29*g, g = global chunk id.
* chunk ids are handed out in blocks of 2260 from one shared counter; each of the 4 writers (sets) takes a
  block when it needs ids and appends its chunks to its own current bucket. bucket id = fill-completion order.
* bucket: Pool/0/<id//2048>/<id%2048>.bucket.<rev> raw concatenated LZ4 blocks; .index = 64-byte header +
  32-byte entries [u32 clen][u32 off][u32 ulen][md5 16][crc32 of first 28 bytes].
"""
import struct, os, re, json, glob, hashlib, collections, bisect
def lz4_decompress_py(src, usize):
    """LZ4 block decode used when the lz4 package is not installed. `usize` is the caller's expected size; this fallback does not check it."""
    dst = bytearray(); i = 0; n = len(src)
    while i < n:
        tok = src[i]; i += 1; ll = tok >> 4
        if ll == 15:
            while True:
                b = src[i]; i += 1; ll += b
                if b != 255: break
        dst += src[i:i + ll]; i += ll
        if i >= n: break
        off = src[i] | (src[i + 1] << 8); i += 2; ml = tok & 15
        if ml == 15:
            while True:
                b = src[i]; i += 1; ml += b
                if b != 255: break
        ml += 4; start = len(dst) - off
        if off >= ml: dst += dst[start:start + ml]
        else:
            for k in range(ml): dst.append(dst[start + k])
    return bytes(dst)
try:
    import lz4.block as _lz4
    def lz4_decompress(src, usize): return _lz4.decompress(src, uncompressed_size=usize)
except Exception:  # pure python fallback (slow)
    lz4_decompress = lz4_decompress_py

SLICE = 8388608
BLOCK = 2260
REC_MAGIC = 0xa93b0200
SET_OF_WRITER = {0: 4, 1: 3, 2: 1, 3: 2}      # writer w's records live in file_chunk<set>.index
WRITER_OF_SET = {v: k for k, v in SET_OF_WRITER.items()}

def dump_json(path, obj):
    """Write JSON atomically (tmp file + os.replace): a crash never leaves a half-written map that load_map would prefer."""
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f: json.dump(obj, f)
    os.replace(tmp, path)

def load_json(path):
    with open(path) as f: return json.load(f)


def rev_sort_key(name):
    m = re.search(r"\.(\d+)$", name); return int(m.group(1)) if m else -1

class SlicedStream:
    """Random access over <n>.idx.<rev> slices of one logical stream (local files, fetched on demand)."""
    def __init__(self, source, reldir):
        self.src, self.reldir = source, reldir
        self._names = None; self._fh = {}
    def names(self):
        if self._names is None:
            self._names = {}
            for n, e in self.src.listdir(self.reldir).items():
                m = re.match(r"^(\d+)\.idx\.(\d+)$", n)
                if m: 
                    k = int(m.group(1))
                    if k not in self._names or int(m.group(2)) > rev_sort_key(self._names[k]): self._names[k] = n
        return self._names
    def slice_path(self, k, job=None):
        n = self.names().get(k)
        if n is None: raise FileNotFoundError(f"{self.reldir}: slice {k} missing")
        return self.src.ensure(f"{self.reldir}/{n}", job)
    def read(self, off, n, job=None):
        out = b""
        while n > 0:
            k, lo = divmod(off, SLICE)
            p = self.slice_path(k, job)
            fh = self._fh.get(k)
            if fh is None: fh = self._fh[k] = open(p, "rb")
            fh.seek(lo); chunk = fh.read(min(n, SLICE - lo))
            if not chunk: raise EOFError(f"{self.reldir} offset {off}")
            out += chunk; off += len(chunk); n -= len(chunk)
        return out

class VirtualFileIndex:
    def __init__(self, source): self.s = SlicedStream(source, "Config/virtual_file.index")
    def entry(self, off):
        f = struct.unpack(">14I", self.s.read(off, 56))
        ptr_a = ((f[0] & 0xffff) << 32 | f[1], f[0] >> 16)
        ptr_b = ((f[12] & 0xffff) << 32 | f[13], f[12] >> 16)
        return {"set": f[12] >> 16, "rec_off": ptr_b[0], "list_off": ptr_a[0], "version": f[2], "flags": f[3], "mtime": f[9], "crc": f[11], "raw": f}

class ChunkRecords:
    def __init__(self, source):
        self.src = source; self.streams = {s: SlicedStream(source, f"Config/file_chunk{s}.index") for s in (1, 2, 3, 4)}
    def record(self, set_no, off):
        hdr = self.streams[set_no].read(off, 12)
        flags, magic, ln = struct.unpack(">III", hdr)
        if magic != REC_MAGIC: raise ValueError(f"no record magic at set{set_no}+{off}: {hdr.hex()}")
        body = self.streams[set_no].read(off + 12, ln)
        refs = struct.unpack(f">{ln // 8}Q", body)
        if (refs[-1] >> 32) != 0x74000000: raise ValueError("record trailer missing")
        ids = []
        for r in refs[:-1]:
            if (r - 64) % 29: raise ValueError(f"bad ref {r:#x}")
            ids.append((r - 64) // 29)
        return {"flags": flags, "ids": ids}

def parse_record_stream(path_iter, on_record):
    """Sequentially parse a set's slices (list of local paths in order); calls on_record(stream_off, ids)."""
    data = b"".join(open(p, "rb").read() for p in path_iter)
    n = len(data); p = 64
    while p + 8 <= n:
        magic, ln = struct.unpack_from(">II", data, p)
        if (magic >> 16) != 0xa93b:
            q = data.find(b"\xa9\x3b", p + 1)
            if q < 0: break
            p = q; continue
        if magic == REC_MAGIC and ln % 8 == 0 and ln >= 8 and p + 8 + ln <= n:
            refs = struct.unpack_from(f">{ln // 8}Q", data, p + 8)
            if (refs[-1] >> 32) == 0x74000000:
                on_record(p - 4, [(r - 64) // 29 for r in refs[:-1] if (r - 64) % 29 == 0])
        p += 8 + ln

class PoolMap:
    """bucket -> (writer, first writer-relative chunk position); writer -> sorted block list."""
    def __init__(self, path):
        self.path = path; self.blocks = {}; self.block_pos = {}; self.buckets = {}; self.counts = {}; self.cum = {}
        if os.path.isfile(path): self.load()
    def load(self):
        d = load_json(self.path)
        self.blocks = {int(w): v for w, v in d["blocks"].items()}
        self.block_pos = {w: {b: i for i, b in enumerate(v)} for w, v in self.blocks.items()}
        self.counts = {int(k): v for k, v in d["counts"].items()}
        self.buckets = {int(k): tuple(v) for k, v in d["buckets"].items()}   # bucket -> (writer, jstart)
        self.cum = {}
        for b, (w, j) in self.buckets.items(): self.cum.setdefault(w, []).append((j, b))
        for w in self.cum: self.cum[w].sort()
    def save(self):
        dump_json(self.path, {"blocks": self.blocks, "counts": self.counts, "buckets": {k: list(v) for k, v in self.buckets.items()}})
    def writer_of_id(self, g):
        blk = g // BLOCK
        for w, pos in self.block_pos.items():
            if blk in pos: return w, pos[blk] * BLOCK + g % BLOCK
        return None, None
    def locate(self, g):
        """global chunk id -> (bucket, entry) or raises."""
        w, j = self.writer_of_id(g)
        if w is None: raise KeyError(f"chunk id {g}: block {g // BLOCK} has no known writer")
        lst = self.cum.get(w, [])
        i = bisect.bisect_right(lst, (j, 1 << 60)) - 1
        if i < 0: raise KeyError(f"chunk id {g}: writer {w} position {j} before first mapped bucket")
        jstart, b = lst[i]
        if j - jstart >= self.counts[b]: raise KeyError(f"chunk id {g}: writer {w} position {j} not covered (bucket map incomplete after bucket {b})")
        return b, j - jstart

def bucket_index_entries(path):
    d = open(path, "rb").read()
    return [struct.unpack_from(">III", d, p) + (d[p + 12:p + 28],) for p in range(64, len(d) - 31, 32)]

def tag_parts(tag):
    """Split a snapshot `version_list.tag` (20 raw bytes, or 40 hex chars) into (md5 hex of the first K chunk hashes, tail).
    tag = md5(concat of the 16-byte md5 fields of the file's first K chunk index entries) + u32 big-endian total uncompressed
    length of those K chunks (`tail`). K is the number of leading chunks whose ulen sums to exactly `tail` (empirically ≤ 9)."""
    if isinstance(tag, str): tag = bytes.fromhex(tag)
    if len(tag) != 20: raise ValueError(f"tag must be 20 bytes, got {len(tag)}")
    return tag[:16].hex(), struct.unpack(">I", tag[16:])[0]

def iter_file_starts(entries_by_bucket, tag16_hex, tail):
    """Yield every (bucket, entry index, K) where a run of K consecutive bucket-index entries matches the file tag:
    their ulen sum == `tail` and md5(concat of their md5 fields) == `tag16_hex`. Needs only bucket index files, no pool map.
    `entries_by_bucket` is an ordered mapping {bucket: entries} (entries as from `bucket_index_entries`), in the order the
    buckets were filled by one writer; a run that reaches the end of a bucket continues into the next listed bucket."""
    want = tag16_hex.lower(); tail = int(tail)
    if tail <= 0: return
    buckets = list(entries_by_bucket); n = len(buckets)
    for bi, b in enumerate(buckets):
        ents = entries_by_bucket[b]; m = len(ents)
        pre = [0] * (m + 1)
        for i, e in enumerate(ents): pre[i + 1] = pre[i] + e[2]
        for i in range(m):
            j = bisect.bisect_left(pre, pre[i] + tail, i + 1)      # first j with pre[j] - pre[i] >= tail
            if j <= m:
                if pre[j] - pre[i] != tail: continue
                hashes = [e[3] for e in ents[i:j]]
            else:                                                 # run crosses into the next bucket (same writer)
                cum = pre[m] - pre[i]; hashes = [e[3] for e in ents[i:]]; k = bi + 1
                while cum < tail and k < n:
                    for e in entries_by_bucket[buckets[k]]:
                        cum += e[2]; hashes.append(e[3])
                        if cum >= tail: break
                    k += 1
                if cum != tail: continue
            if hashlib.md5(b"".join(hashes)).hexdigest() == want: yield b, i, len(hashes)

def find_file_start(entries_by_bucket, tag16_hex, tail):
    """First (bucket, entry index, K) matching the file tag, or None. See `iter_file_starts`; `tag_parts` splits the tag."""
    return next(iter_file_starts(entries_by_bucket, tag16_hex, tail), None)

def file_pool_chunks(path):
    d = open(path, "rb").read(); p = 0x140; out = []
    while p + 12 <= len(d):
        mg, clen, ulen, z = struct.unpack_from(">HIIH", d, p)
        if mg != 0xe5bb: raise ValueError(f"file_pool chunk magic {mg:#x} at {p}")
        out.append(lz4_decompress(d[p + 12:p + 12 + clen], ulen)); p += 12 + clen
    return out

# ----------------------------------------------------------------------------- offline analysis: record runs
def build_runs(source, outdir, log=print):
    """Parse all 4 record streams. Writes <outdir>/runs_set<s>.bin (rec_off,u64 g_start,u32 len triples) and
    blocks.json (block -> owning writer, by longest consecutive runs)."""
    from array import array
    os.makedirs(outdir, exist_ok=True)
    owner_votes = collections.defaultdict(lambda: [0, 0, 0, 0])   # block -> votes per writer (sum of run lengths >= 2)
    stats = {}
    for s in (1, 2, 3, 4):
        w = WRITER_OF_SET[s]
        st = SlicedStream(source, f"Config/file_chunk{s}.index")
        names = st.names(); paths = [st.slice_path(k) for k in sorted(names)]
        rec_off = array("q"); g0 = array("q"); ln = array("l"); nrec = 0; nrefs = 0
        def on_record(off, ids):
            nonlocal nrec, nrefs
            nrec += 1; nrefs += len(ids)
            i = 0; n = len(ids)
            while i < n:
                j = i + 1
                while j < n and ids[j] == ids[j - 1] + 1: j += 1
                rec_off.append(off); g0.append(ids[i]); ln.append(j - i)
                if j - i >= 2:
                    b0, b1 = ids[i] // BLOCK, ids[j - 1] // BLOCK
                    for b in range(b0, b1 + 1): owner_votes[b][w] += j - i
                i = j
        parse_record_stream(paths, on_record)
        with open(os.path.join(outdir, f"runs_set{s}.bin"), "wb") as f:
            f.write(struct.pack("<Q", len(g0))); rec_off.tofile(f); g0.tofile(f); ln.tofile(f)
        stats[s] = {"records": nrec, "refs": nrefs, "runs": len(g0)}
        log(f"set{s}: records={nrec:,} refs={nrefs:,} runs={len(g0):,}")
    blocks = {}
    amb = 0
    for b, v in owner_votes.items():
        best = max(range(4), key=lambda w: v[w]); tot = sum(v)
        if v[best] < 0.6 * tot: amb += 1
        blocks[b] = best
    dump_json(os.path.join(outdir, "blocks.json"), {"blocks": blocks, "stats": stats, "ambiguous": amb})
    log(f"blocks={len(blocks):,} ambiguous={amb} max_block={max(blocks) if blocks else -1}")
    return stats

def load_runs(outdir, s):
    from array import array
    with open(os.path.join(outdir, f"runs_set{s}.bin"), "rb") as f:
        n = struct.unpack("<Q", f.read(8))[0]
        a = array("q"); a.fromfile(f, n); b = array("q"); b.fromfile(f, n); c = array("l"); c.fromfile(f, n)
    return a, b, c

# ----------------------------------------------------------------------------- offline analysis: record -> file size
def build_recsizes(source, snapshot_dbs, outdir, log=print):
    """snapshot_dbs: iterable of (sqlite path, kind) where kind='index' (our derived files table) or 'raw'
    (Hyper Backup <ver>.db with version_list). Writes recsize_set<s>.json: {rec_off: size}."""
    import sqlite3, mmap
    vf = SlicedStream(source, "Config/virtual_file.index")
    names = vf.names(); maps = {}
    for k in sorted(names):
        p = vf.slice_path(k); fh = open(p, "rb"); maps[k] = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
    def entry(off):
        k, lo = divmod(off, SLICE); m = maps.get(k)
        if m is None: return None
        b = m[lo:lo + 56]
        if len(b) < 56:
            m2 = maps.get(k + 1)
            if m2 is None: return None
            b += m2[:56 - len(b)]
        f = struct.unpack(">14I", b); return f[12] >> 16, ((f[12] & 0xffff) << 32) | f[13]
    sizes = {1: {}, 2: {}, 3: {}, 4: {}}; n = 0; conflicts = 0
    for path, kind in snapshot_dbs:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        q = ("select off, size from files where is_dir=0 and off>=0 and (mode & 61440)=32768" if kind == "index"
             else "select off_virtual_file, size from version_list where off_virtual_file>=0 and (mode & 61440)=32768")
        for off, size in con.execute(q):
            e = entry(off)
            if not e: continue
            s, ro = e
            if s not in sizes: continue
            old = sizes[s].get(ro)
            if old is not None and old != size: conflicts += 1
            sizes[s][ro] = size; n += 1
        con.close()
    for s in sizes: dump_json(os.path.join(outdir, f"recsize_set{s}.json"), sizes[s])
    log(f"record sizes: {sum(len(v) for v in sizes.values()):,} records from {n:,} rows, conflicts={conflicts}")

# ----------------------------------------------------------------------------- reading chunks back
class PoolReader:
    """Reconstructs file contents from the pool using poolmap.json (built by pool/poolmap.py)."""
    def __init__(self, source, pool_dir, log=None):
        self.src, self.pool_dir = source, pool_dir
        self.vf = VirtualFileIndex(source); self.recs = ChunkRecords(source)
        self.map = None; self.idx_cache = collections.OrderedDict(); self.names = None; self.v1 = self.v2 = False; self.counts = {}
        self.load_map()
    # --- map
    def load_map(self):
        """poolmap2.json (v2, per-record placements) is used first; poolmap.json (v1) stays loaded as the fallback for chunks a
        partial v2 map (mapped_upto < total buckets) cannot serve. Returns True when any map is loaded."""
        self.v1 = self.v2 = False; self.map = None
        p = os.path.join(self.pool_dir, "poolmap.json"); p2 = os.path.join(self.pool_dir, "poolmap2.json")
        if os.path.isfile(p): self.load_map1(p)
        if os.path.isfile(p2): self.load_map2(p2)
        return self.map is not None
    def load_map1(self, p):
        d = load_json(p)
        self.block_pos = {}
        for w, blocks in d["blocks"].items():
            for i, b in enumerate(blocks): self.block_pos[b] = (int(w), i)
        self.pieces = {int(w): v for w, v in d["pieces"].items()}
        self.counts.update({int(k): v for k, v in d["counts"].items()})
        self.assigned = {w: [] for w in range(4)}
        for b, (w, s0) in d["buckets"].items(): self.assigned[w].append((s0, int(b)))
        for w in self.assigned: self.assigned[w].sort()
        self.map = d; self.mapped_upto = d.get("mapped_upto", 0); self.v1 = True
        return True
    def status(self):
        listing = os.path.join(self.pool_dir, "listing.json")
        st = {"listing": os.path.isfile(listing), "runs": all(os.path.isfile(os.path.join(self.pool_dir, f"runs_set{s}.bin")) for s in (1, 2, 3, 4)),
              "recsizes": os.path.isfile(os.path.join(self.pool_dir, "recsize_set1.json")), "map": self.map is not None,
              "mapped_buckets": len(self.map["buckets"]) if self.map else 0, "total_buckets": 0, "indexes_local": 0, "index_files": 0}
        if st["listing"]:
            l = load_json(listing); idx = [e for e in l if e["t"] == "index"]
            st["total_buckets"] = len({e["b"] for e in idx})
            st["index_files"] = len(idx)
            n = 0
            for e in idx:
                if self.src.has_sized_file(f"Pool/0/{e['b'] // 2048}/{e['name']}", e.get("size")): n += 1
            st["indexes_local"] = n
        return st
    def bucket_name(self, b, kind):
        if self.names is None:
            listing = os.path.join(self.pool_dir, "listing.json")
            self.names = {}
            for e in load_json(listing):
                k = (e["b"], e["t"])
                if k not in self.names or e["rev"] > self.names[k][1]: self.names[k] = (e["name"], e["rev"])
        return f"Pool/0/{b // 2048}/{self.names[(b, kind)][0]}"
    def load_map2(self, p):
        """v2 map: per record placement segments + new-id ranges (storage order != id order)."""
        d = load_json(p); self.v2 = True
        self.counts.update({int(k): v for k, v in d["counts"].items()})
        self.place2 = {int(s): {int(k): v for k, v in m.items()} for s, m in d["placements"].items()}
        self.rng2 = {}; self.recrng = {}        # recrng[set][rec_off] = the record's new-id ranges [(start, len)] in id order
        for s, lst in d["ranges"].items():
            lst = sorted(lst); self.rng2[int(s)] = ([x[0] for x in lst], [x[1] for x in lst], [x[2] for x in lst])
            rr = self.recrng[int(s)] = {}
            for a, L, ro in lst: rr.setdefault(ro, []).append((a, L))
        self.guessed = {int(s): set(v) for s, v in d.get("guessed", {}).items()}     # records placed without tag evidence
        self.map = d; self.mapped_upto = d.get("mapped_upto", 0)
        return True
    def _rec_of_id(self, g):
        for s, (st, ln, ro) in self.rng2.items():
            i = bisect.bisect_right(st, g) - 1
            if i >= 0 and g < st[i] + ln[i]: return s, ro[i]
        return None
    def _segs_lookup(self, s, rec_off, k):
        segs = self.place2.get(s, {}).get(rec_off)
        if segs is None: raise KeyError(f"record {rec_off:#x} (set {s}) not placed yet (map covers up to bucket {self.mapped_upto})")
        for b, e0, c in segs:
            if k < c: return b, e0 + k
            k -= c
        raise KeyError(f"record {rec_off:#x}: chunk index {k} beyond its placement")
    def locate2(self, set_no, rec_off, ids):
        """All chunk locations of a record (set, rec_off) with its id list, in file order."""
        out = []; own = self.place2.get(set_no, {}).get(rec_off)
        own_ranges = self.recrng.get(set_no, {}).get(rec_off, [])     # this record's new-id ranges, id order
        for g in ids:
            k = 0; found = False
            for a, L in own_ranges:
                if a <= g < a + L: k += g - a; found = True; break
                k += L
            if found and own is not None: out.append(self._segs_lookup(set_no, rec_off, k)); continue
            owner = self._rec_of_id(g)
            if owner is None: raise KeyError(f"chunk id {g}: no record owns it")
            s2, r2 = owner; k = 0
            for a, L in self.recrng[s2][r2]:
                if a <= g < a + L: k += g - a; break
                k += L
            out.append(self._segs_lookup(s2, r2, k))
        return out
    def locate(self, g):
        if self.v2:
            try:
                owner = self._rec_of_id(g)
                if owner is None: raise KeyError(f"chunk id {g}: no record owns it")
                return self.locate2(owner[0], owner[1], [g])[0]
            except KeyError:
                if not self.v1: raise
        return self.locate1(g)
    def locate1(self, g):
        if not self.v1: raise KeyError(f"chunk {g}: no v1 map loaded")
        bp = self.block_pos.get(g // BLOCK)
        if bp is None: raise KeyError(f"chunk {g}: unknown id block {g // BLOCK}")
        w, bi = bp; p = bi * BLOCK + g % BLOCK; pc = self.pieces[w]
        i = bisect.bisect_right(pc["raw_start"], p) - 1
        if i < 0 or p >= pc["raw_start"][i] + pc["length"][i]: raise KeyError(f"chunk {g}: not in writer {w} piece table")
        sp = pc["stored_start"][i] + (p - pc["raw_start"][i])
        lst = self.assigned[w]; k = bisect.bisect_right(lst, (sp, 1 << 62)) - 1
        if k < 0: raise KeyError(f"chunk {g}: writer {w} position {sp} precedes mapped buckets")
        s0, b = lst[k]
        if sp - s0 >= self.counts[b]: raise KeyError(f"chunk {g}: writer {w} position {sp} beyond mapped buckets (map covers up to bucket {self.mapped_upto})")
        return b, sp - s0
    def entries(self, b, job=None):
        e = self.idx_cache.get(b)
        if e is None:
            e = bucket_index_entries(self.src.ensure(self.bucket_name(b, "index"), job)); self.idx_cache[b] = e
            if len(self.idx_cache) > 2000: self.idx_cache.popitem(last=False)
        return e
    def bucket_data_path(self, b, job=None): return self.src.ensure(self.bucket_name(b, "bucket"), job)
    # --- files
    def plan(self, off):
        """off = off_virtual_file (>=0). Returns list of (bucket, entry) in file order."""
        e = self.vf.entry(off); rec = self.recs.record(e["set"], e["rec_off"])
        if self.v2:
            try: return self.locate2(e["set"], e["rec_off"], rec["ids"]), rec
            except KeyError:
                if not self.v1: raise            # partial v2 map: fall back to the v1 map for this file
        return [self.locate1(g) for g in rec["ids"]], rec
    def read_chunk(self, b, i, fh_cache, job=None):
        clen, off, ulen, h = self.entries(b, job)[i]
        fh = fh_cache.get(b)
        if fh is None:
            fh = fh_cache[b] = open(self.bucket_data_path(b, job), "rb")
        fh.seek(off); raw = fh.read(clen)
        data = lz4_decompress(raw, ulen)
        if hashlib.md5(data).digest() != h[:16]: raise ValueError(f"md5 mismatch bucket {b} entry {i}")
        return data
    def extract(self, off, out_path, size=None, mtime=None, job=None, fh_cache=None):
        """Write one pooled file to out_path. Returns bytes written."""
        own = fh_cache is None
        if own: fh_cache = {}
        try:
            if off < 0:
                name = self.file_pool_name(-off)
                chunks = file_pool_chunks(self.src.ensure(name, job))
                tmp = out_path + ".part"
                with open(tmp, "wb") as f:
                    for c in chunks: f.write(c)
                n = sum(len(c) for c in chunks)
            else:
                locs, rec = self.plan(off); tmp = out_path + ".part"; n = 0
                with open(tmp, "wb") as f:
                    for b, i in locs:
                        d = self.read_chunk(b, i, fh_cache, job); f.write(d); n += len(d)
            if size is not None and n != size: raise ValueError(f"size mismatch: wrote {n}, expected {size}")
            os.replace(tmp, out_path)
            if mtime: os.utime(out_path, (mtime, mtime))
            return n
        finally:
            if own:
                for fh in fh_cache.values(): fh.close()
    def file_pool_name(self, n):
        ents = self.src.find("Pool/file_pool", f"{n}.file.*")
        if not ents: raise FileNotFoundError(f"Pool/file_pool/{n}.file.* missing")
        return "Pool/file_pool/" + sorted(ents, key=lambda x: int(x.rsplit(".", 1)[1]))[-1]
    def buckets_for(self, offs):
        """Set of bucket ids needed for a list of virtual offsets (for pre-fetch / size estimate)."""
        bs = set()
        for off in offs:
            if off >= 0:
                try: bs.update(b for b, _ in self.plan(off)[0])
                except Exception: pass
        return bs

# ----------------------------------------------------------------------------- record -> (size, tag) from raw snapshots
def build_recmeta(source, snapshot_dbs, outdir, log=print):
    """Raw Hyper Backup <ver>.db files -> recmeta_set<s>.json: {rec_off: [size, tag16hex, tag_tail]}.
    tag = md5(concat of the 16-byte md5 of the file's first K chunks), tail = total ulen of those K chunks (K = min(n, 9))."""
    import sqlite3, mmap
    vf = SlicedStream(source, "Config/virtual_file.index"); names = vf.names(); maps = {}; fhs = []
    for k in sorted(names):
        fh = open(vf.slice_path(k), "rb"); fhs.append(fh); maps[k] = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
    def entry(off):
        k, lo = divmod(off, SLICE); m = maps.get(k)
        if m is None: return None
        b = m[lo:lo + 56]
        if len(b) < 56:
            m2 = maps.get(k + 1)
            if m2 is None: return None
            b += m2[:56 - len(b)]
        f = struct.unpack(">14I", b); return f[12] >> 16, ((f[12] & 0xffff) << 32) | f[13]
    meta = {1: {}, 2: {}, 3: {}, 4: {}}; n = 0
    for path in snapshot_dbs:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        for off, size, tag in con.execute("select off_virtual_file, size, tag from version_list where off_virtual_file>=0 and (mode & 61440)=32768"):
            e = entry(off)
            if not e or e[0] not in meta: continue
            s, ro = e; cur = meta[s].get(ro)
            t16 = tag[:16].hex() if tag and len(tag) == 20 else None
            tail = struct.unpack(">I", tag[16:20])[0] if tag and len(tag) == 20 else None
            if cur is None: meta[s][ro] = [size, t16, tail]
            else:
                if cur[1] is None and t16: cur[1] = t16; cur[2] = tail
            n += 1
        con.close()
    for m in maps.values(): m.close()
    for fh in fhs: fh.close()
    for s in meta: dump_json(os.path.join(outdir, f"recmeta_set{s}.json"), meta[s])
    tagged = sum(1 for v in meta.values() for x in v.values() if x[1])
    log(f"record meta: {sum(len(v) for v in meta.values()):,} records ({tagged:,} with tag) from {n:,} rows")
