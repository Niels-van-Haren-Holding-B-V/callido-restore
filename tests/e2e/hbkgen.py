"""Synthetic Hyper Backup (.hbk) with a real data pool, for end-to-end tests of the whole flow (open, file lists, pool
preparation, restore) against files whose bytes are known.

    python -m tests.e2e.hbkgen <out>/e2e_1.hbk          # writes the backup and <out>/e2e_1.hbk.manifest.json

What it writes follows docs/FORMAT.md and the readers in callido_restore/core/pool.py:
* Config/version_info.db, storage_statistics.db, Config/@Share/<share>/<ver>.db (version_list with a 20-byte `tag`)
* Config/virtual_file.index/0.idx (64-byte header, 56-byte entries), Config/file_chunk{1..4}.index/0.idx (record TLVs)
* Pool/0/<d>/<n>.bucket.<rev> + .index.<rev> (LZ4 blocks; 64-byte header + 32-byte entries), Pool/file_pool/<n>.file.<rev>

The writer model is the one the mapper assumes (pool/place.py docstring): four writers (sets 4, 3, 1, 2), chunk ids handed
out in blocks of 2260 from one shared counter, each writer appends to its own open bucket, a bucket gets its id when it is
closed (fill-completion order across writers) and its index carries the revision of the run that closed it. Chunking is
content defined (so identical content dedups), chunks stay under 32 KiB.

Scenarios, each recorded in the manifest so a test can say which one broke:
* small files in the file pool, multi-chunk files, files that span several buckets, a nested folder, odd names
* run 2 adds files, modifies one by appending (its leading chunks are dedup references: an untagged record), copies a file
  byte for byte (a "phantom": fresh ids, nothing stored, as measured on the real backup), deletes one
* run 3 is a ransomware run: files renamed *.L0CK3D with new content plus a ransom note; run 2 is the last clean run

Layout choices the real format does not pin down are marked ASSUMED below; a failure on one of those is a question for
the format notes, not necessarily an app bug.
"""
import collections, hashlib, json, os, random, re, sqlite3, struct, sys, zlib

try:
    import lz4.block as _lz4
except ImportError:                                                     # pragma: no cover - lz4 is a project requirement
    _lz4 = None

ROOT_PARENT = bytes.fromhex("5058F1AF5058F1AF8388633F609CADB75A75DC9D")
BLOCK = 2260
REC_MAGIC = 0xa93b0200
TAGK = 9
SLICE = 8 * 1024 * 1024                         # every sliced stream (*.idx) is cut at 8 MiB
SET_OF_WRITER = {0: 4, 1: 3, 2: 1, 3: 2}
CUT = re.compile(rb"\x00[\x00-\x07]")         # content-defined cut points: ~1 in 8 KiB of random data
MIN_CHUNK, MAX_CHUNK = 2048, 32 * 1024
FILE_POOL_MAX = 16 * 1024                       # ASSUMED: files below this size are stored whole in Pool/file_pool
T0 = 1_780_000_000                              # 2026-05-28, a fixed clock so the backup is reproducible


def chunks_of(data):
    """Content-defined chunks: cut after a CUT match once MIN_CHUNK is reached, never beyond MAX_CHUNK."""
    out = []; start = 0; n = len(data)
    marks = [m.end() for m in CUT.finditer(data)]; mi = 0
    while start < n:
        lo, hi = start + MIN_CHUNK, min(n, start + MAX_CHUNK)
        while mi < len(marks) and marks[mi] < lo: mi += 1
        end = marks[mi] if mi < len(marks) and marks[mi] <= hi else hi
        out.append(data[start:end]); start = end
    return out


def lz4c(b):
    if _lz4 is None: raise RuntimeError("the lz4 package is needed to write a synthetic backup")
    return _lz4.compress(b, store_size=False)


def content(seed, size):
    """Deterministic bytes: random runs mixed with text, so LZ4 has something to do and cut points are content defined."""
    r = random.Random(seed); out = bytearray()
    words = [b"backup", b"restore", b"invoice", b"photo", b"report", b"the", b"and", b"2026", b"\n"]
    while len(out) < size:
        if r.random() < 0.5: out += r.randbytes(r.randint(256, 4096))
        else: out += b" ".join(r.choice(words) for _ in range(r.randint(20, 300)))
    return bytes(out[:size])


class Pool:
    """The four writers, their open buckets, the shared id counter and the record / virtual file streams."""
    def __init__(self, root, bucket_cap, id_base_blocks=0):
        self.root, self.cap = root, bucket_cap
        self.next_block = id_base_blocks; self.next_bucket = 0; self.next_fp = 1     # ids may start high (history before the kept runs)
        self.blk = {w: None for w in range(4)}; self.used = {w: BLOCK for w in range(4)}
        self.open = {w: None for w in range(4)}                        # [entries, data]
        self.stored = {}                                               # chunk md5 -> global id
        self.where = {}                                                # global id -> (bucket, entry): the ground truth the map should find
        self.refs = collections.Counter()                              # global id -> references (Pool/chunk_index refcount; ASSUMED semantics)
        self.alias = {}                                                # phantom id -> the stored id with the same content
        self.md5 = {}                                                  # global id -> md5 of the chunk
        self.recs = {s: bytearray(64) for s in (1, 2, 3, 4)}           # ASSUMED: header bytes are zeros
        self.vf = bytearray(64)
        self.bucket_writer = {}                                        # bucket -> (writer, revision)
        self.bucket_ids = {}                                           # bucket -> the chunk ids stored in it
        self.free = []; self.reuse_writer = None; self.reused = {}     # bucket ids freed by a purge, and who took them
        self.rev = 0; self.stats = {"buckets": 0, "phantoms": 0, "dedup_refs": 0, "file_pool": 0}

    def new_id(self, w):
        if self.used[w] >= BLOCK: self.blk[w] = self.next_block; self.next_block += 1; self.used[w] = 0
        g = self.blk[w] * BLOCK + self.used[w]; self.used[w] += 1; return g

    def append(self, w, chunk, g):
        if self.open[w] is None: self.open[w] = [[], bytearray(), []]
        ents, data, gs = self.open[w]; c = lz4c(chunk); gs.append(g)
        ents.append((len(c), len(data), len(chunk), hashlib.md5(chunk).digest())); data += c
        if len(data) >= self.cap: self.close(w)

    def close(self, w):
        if not self.open[w]: return
        ents, data, gs = self.open[w]; self.open[w] = None
        if self.free and self.reuse_writer in (None, w):               # after a purge one writer takes the lowest free ids,
            self.reuse_writer = w; b = self.free.pop(0); self.reused[b] = w   # the others keep getting fresh, higher ones
        else: b = self.next_bucket; self.next_bucket += 1
        self.stats["buckets"] += 1; self.bucket_ids[b] = list(gs)
        for i, g in enumerate(gs): self.where[g] = (b, i)
        self.bucket_writer[b] = (w, self.rev)
        d = os.path.join(self.root, "Pool", "0", str(b // 2048)); os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{b % 2048}.bucket.{self.rev}"), "wb") as f: f.write(data)
        idx = bytearray(64)                                            # ASSUMED: index header bytes are zeros
        for clen, off, ulen, md5 in ents:
            e = struct.pack(">III", clen, off, ulen) + md5
            idx += e + struct.pack(">I", zlib.crc32(e))
        with open(os.path.join(d, f"{b % 2048}.index.{self.rev}"), "wb") as f: f.write(idx)

    def purge(self, keep_ids):
        """Version rotation: buckets (of any revision) none of whose chunks a kept record needs are deleted and their ids
        freed. ASSUMED: the chunk-record streams keep the records of the rotated run (their chunks are simply gone)."""
        gone = sorted(b for b, gs in self.bucket_ids.items() if not any(g in keep_ids for g in gs))
        for b in gone:
            d = os.path.join(self.root, "Pool", "0", str(b // 2048)); rev = self.bucket_writer[b][1]
            for kind in ("bucket", "index"): os.unlink(os.path.join(d, f"{b % 2048}.{kind}.{rev}"))
            for g in self.bucket_ids.pop(b): self.where.pop(g, None)
            self.bucket_writer.pop(b); self.stats["buckets"] -= 1
        dead = {g for g in range(self.next_block * BLOCK) if g not in self.where}
        self.stored = {h: g for h, g in self.stored.items() if g not in dead}
        self.free = gone; self.stats["purged"] = len(gone)
        return gone

    def close_all(self):
        for w in range(4): self.close(w)

    def store_file(self, w, data, mtime, phantom_ok=True):
        """Back up one file through writer w. Returns (off_virtual_file, tag, info)."""
        if len(data) < FILE_POOL_MAX:
            n = self.next_fp; self.next_fp += 1; self.stats["file_pool"] += 1
            d = os.path.join(self.root, "Pool", "file_pool"); os.makedirs(d, exist_ok=True)
            body = bytearray(0x140)                                    # ASSUMED: header bytes are zeros
            for ch in (chunks_of(data) if data else []):
                c = lz4c(ch); body += struct.pack(">HIIH", 0xe5bb, len(c), len(ch), 0) + c
            with open(os.path.join(d, f"{n}.file.{self.rev}"), "wb") as f: f.write(body)
            return -n, None, {"kind": "file_pool"}
        chs = chunks_of(data); known = [hashlib.md5(c).digest() in self.stored for c in chs]
        ids = []; kind = "stored"; src_ids = None
        if phantom_ok and len(chs) >= 2 and all(known):
            ids = [self.new_id(w) for _ in chs]; kind = "phantom"; self.stats["phantoms"] += 1   # fresh ids, nothing appended
            src_ids = [self.stored[hashlib.md5(c).digest()] for c in chs]
            for g, g0 in zip(ids, src_ids): self.alias[g] = g0; self.refs[g] += 1; self.refs[g0] += 1
        else:
            for c in chs:
                h = hashlib.md5(c).digest()
                if h in self.stored: ids.append(self.stored[h]); self.refs[self.stored[h]] += 1; self.stats["dedup_refs"] += 1; kind = "partial_dedup" if kind == "stored" else kind
                else:
                    g = self.new_id(w); self.stored[h] = g; ids.append(g); self.refs[g] += 1; self.md5[g] = h; self.append(w, c, g)
        s = SET_OF_WRITER[w]; st = self.recs[s]
        refs = b"".join(struct.pack(">Q", 64 + 29 * g) for g in ids)
        body = refs + struct.pack(">II", 0x74000000, zlib.crc32(refs))
        rec_off = len(st); st += struct.pack(">III", 0, REC_MAGIC, len(body)) + body
        a = rec_off + 12; off = len(self.vf)
        f = [0] * 14
        f[0], f[1] = (s << 16) | (a >> 32), a & 0xffffffff
        f[2], f[3], f[4] = self.rev, 0x402, 0x64
        f[6], f[9] = mtime, mtime
        f[11] = zlib.crc32(refs)
        f[12], f[13] = (s << 16) | (rec_off >> 32), rec_off & 0xffffffff
        self.vf += struct.pack(">14I", *f)
        K = min(len(chs), TAGK)
        tag = hashlib.md5(b"".join(hashlib.md5(c).digest() for c in chs[:K])).digest() + struct.pack(">I", sum(len(c) for c in chs[:K]))
        return off, tag, {"kind": kind, "chunks": len(chs), "set": s, "rec_off": rec_off, "writer": w, "ids": ids, "src_ids": src_ids}

    def chunk_index(self, rev):
        """Pool/chunk_index/<k>.idx.<rev> (hbk-1, after woutervanranst/synology-hyperbackup-reverse-engineering §2): 64-byte header,
        then one 29-byte record per chunk id g at 64 + 29*g: [0x00][u32 bucket][u32 64 + 32*entry in that bucket's .index][15 zero]
        [u8 refcount][u32 fingerprint], sliced at 8 MiB. ASSUMED: header bytes after the magic are zeros; a phantom id points at the
        entry of the stored chunk it copies; ids that store nothing (unused ids of a block, purged chunks) are all zeros;
        refcount = references from records, capped at 255; fingerprint = crc32 of the chunk md5 (the reader checks neither)."""
        n = self.next_block * BLOCK; tab = bytearray(64 + 29 * n); tab[:4] = bytes.fromhex("7053a86e")
        for g in range(n):
            src = self.alias.get(g, g); loc = self.where.get(src)
            if loc is None: continue
            b, e = loc; p = 64 + 29 * g
            tab[p:p + 29] = b"\0" + struct.pack(">II", b, 64 + 32 * e) + bytes(15) + bytes([min(255, self.refs[g] or 1)]) + struct.pack(">I", zlib.crc32(self.md5.get(src, b"")))
        self.straddle = [g for g in range(n) if (64 + 29 * g) // SLICE != (64 + 29 * g + 28) // SLICE and self.alias.get(g, g) in self.where]
        d = os.path.join(self.root, "Pool", "chunk_index"); os.makedirs(d, exist_ok=True)
        for k in range(0, len(tab), SLICE):
            with open(os.path.join(d, f"{k // SLICE}.idx.{rev}"), "wb") as f: f.write(tab[k:k + SLICE])

    def finish(self, rev):
        for s, st in self.recs.items():
            d = os.path.join(self.root, "Config", f"file_chunk{s}.index"); os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, f"0.idx.{rev}"), "wb") as f: f.write(st)
        d = os.path.join(self.root, "Config", "virtual_file.index"); os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"0.idx.{rev}"), "wb") as f: f.write(self.vf)
        with open(os.path.join(self.root, "Pool", f"bucketID.counter.{rev}"), "wb") as f: f.write(struct.pack(">Q", self.next_bucket))


def nid(share, path): return hashlib.sha1(f"{share}/{path}".encode()).digest()


def plan(scale):
    """Files per share for run 1, and the edits of runs 2 and 3. Sizes scale with `scale` (1 = ~6 MB in all)."""
    S = lambda n: max(1, int(n * scale))
    run1 = {
        "Documents": {
            "notes.txt": 900, "Finance/2025/invoices-q1.xlsx": S(180_000), "Finance/2025/tax return 2025.pdf": S(1_240_000),
            "Contracts/lease.pdf": S(310_000), "Contracts/nda.docx": 9_000, "Projects/Website/brief.docx": S(61_000),
            "Projects/Website/notes.txt": 2_300, "Projects/Garden/plan.pdf": S(420_000), "Empty/zero.bin": 0,
            "Ünïcode & spaces/résumé (final).pdf": S(77_000),
        },
        "Photos": {f"2025/Summer/IMG_{n}.jpg": S(random.Random(n).randint(150_000, 600_000)) for n in range(4101, 4109)},
        "homes": {
            "alice/Documents/thesis.docx": S(520_000), "alice/Desktop/todo.txt": 1_020, "bob/Music/track01.mp3": S(2_100_000),
            "bob/.ssh/authorized_keys": 400,
        },
    }
    run2 = {"add": {"Photos": {"2026/Spring/IMG_6001.jpg": S(330_000), "2026/Spring/IMG_6002.jpg": S(290_000)},
                    "Documents": {"Finance/2026/budget.xlsx": S(96_000)}},
            "append": {"Documents": {"Finance/2025/tax return 2025.pdf": S(150_000)}},     # modified in place: leading chunks dedup
            "copy": {"homes": {"alice/Documents/thesis-copy.docx": ("homes", "alice/Documents/thesis.docx")}},
            "delete": {"Documents": ["Projects/Garden/plan.pdf"]}}
    run3_lock = {"Documents": ["Finance/2025/invoices-q1.xlsx", "Contracts/lease.pdf", "notes.txt"], "homes": ["alice/Documents/thesis.docx"]}
    return run1, run2, run3_lock


def build(root, scale=1.0, bucket_cap=256 * 1024, versions=(101, 102, 103), rotate=False, rotate_copy=True, chunk_index=False, id_base_blocks=0):
    """Write the backup at `root`; returns the manifest {"versions": {ver: {share: {path: {...}}}}, ...}.

    rotate=True adds version rotation (hbk-1, 2026-10-31): a run before the first kept one whose files are gone from every
    kept snapshot; at the start of the second kept run its buckets that nothing needs are purged and their ids freed, the
    first writer to close a bucket then takes the lowest free ids (same revision as the fresh ids of the others), so its
    bucket order by (revision, id) is wrong for those. With rotate_copy one rotated file is copied in that run: a copy of an
    untagged original whose content sits in an older bucket (phantom found under the revision clock).

    chunk_index=True writes the backup's own chunk table (Pool/chunk_index, real Hyper Backup always has it; off by default so
    the tests of the inferred map keep exercising the map). id_base_blocks starts the chunk ids that many 2260-id blocks up;
    128 puts ids around 289,000, where a 29-byte record straddles the first 8 MiB slice boundary of the table."""
    root = os.path.abspath(root); os.makedirs(root, exist_ok=True)
    if os.listdir(root): raise FileExistsError(f"{root} is not empty")
    run1, run2, lock = plan(scale)
    pool = Pool(root, bucket_cap, id_base_blocks)
    v1, v2, v3 = versions; ts = {v1: T0, v2: T0 + 86400, v3: T0 + 2 * 86400}
    state = {}                     # (share, path) -> dict(data, mtime, off, tag, born, kind)
    snaps = {}                     # ver -> {(share, path): (entry, status)}
    rr = [0]
    def put(share, path, data, ver, mtime, status):
        w = rr[0] % 4; rr[0] += 1
        off, tag, info = pool.store_file(w, data, mtime)
        state[(share, path)] = {"data": data, "mtime": mtime, "off": off, "tag": tag, "status": status, "info": info, "ctime": mtime}
    rotated = {}
    if rotate:                     # run v1-1: backed up, then rotated away (no snapshot, no version_info row)
        v0 = v1 - 1; ts[v0] = T0 - 86400; pool.rev = v0; S = lambda n: max(1, int(n * scale))
        for p, size in sorted({"Archive/2024/old-report.pdf": 300_000, "Archive/2024/scan.tif": 640_000, "Archive/2024/notes-2024.docx": 140_000,
                               "Archive/2024/contract-orig.pdf": 230_000}.items()):
            put("Documents", p, content(f"rotated/{p}", S(size)), v0, ts[v0] - 86400 * 40, "N")
        pool.close_all()
        rotated = {p: state.pop(("Documents", p)) for p in [k[1] for k in list(state)]}
        if rotate_copy: run2["copy"].setdefault("Documents", {})["Archive/contract-copy.pdf"] = ("rotated", "Archive/2024/contract-orig.pdf")
    # run 1
    pool.rev = v1
    for share in run1:
        for path, size in sorted(run1[share].items()):
            put(share, path, content(f"{share}/{path}", size), v1, ts[v1] - 86400 * (7 + len(path) % 30), "N")
    pool.close_all(); snaps[v1] = {k: dict(e) for k, e in state.items()}
    # run 2
    pool.rev = v2
    if rotate:                     # the rotation's purge: keep what run 1 and the coming copy of the rotated contract need
        keep = {g for e in state.values() for g in e["info"].get("ids", [])} | set(rotated["Archive/2024/contract-orig.pdf"]["info"]["ids"])
        pool.purge(keep)
    for e in state.values(): e["status"] = "U"
    for share, files in run2["delete"].items():
        for p in files: state.pop((share, p))
    for share, files in run2["append"].items():
        for p, extra in files.items():
            old = state[(share, p)]["data"]; put(share, p, old + content(f"append/{p}", extra), v2, ts[v2] - 3600, "M")
    for share, files in run2["copy"].items():
        for p, (s0, p0) in files.items(): put(share, p, (rotated[p0] if s0 == "rotated" else state[(s0, p0)])["data"], v2, ts[v2] - 7200, "N")
    for share, files in run2["add"].items():
        for p, size in sorted(files.items()): put(share, p, content(f"{share}/{p}", size), v2, ts[v2] - 5400, "N")
    pool.close_all(); snaps[v2] = {k: dict(e) for k, e in state.items()}
    # run 3: ransomware
    pool.rev = v3
    for e in state.values(): e["status"] = "U"
    for share, files in lock.items():
        for p in files:
            old = state.pop((share, p))["data"]
            put(share, p + ".L0CK3D", random.Random(p).randbytes(len(old) + 512), v3, ts[v3] - 1800, "N")
        folders = sorted({p.rsplit("/", 1)[0] if "/" in p else "" for p in files})
        for d in folders:
            note = (d + "/" if d else "") + "HOW_TO_RECOVER_FILES.txt"
            put(share, note, b"Your files are encrypted. (synthetic test note)\n" * 30, v3, ts[v3] - 1700, "N")
    pool.close_all(); snaps[v3] = {k: dict(e) for k, e in state.items()}
    last = v3; pool.finish(last)
    if chunk_index: pool.chunk_index(last)
    shares = sorted(run1)
    _write_config(root, shares, versions, ts, snaps, last)
    truth = {"buckets": {str(b): list(v) for b, v in sorted(pool.bucket_writer.items())}, "reused": {str(b): w for b, w in sorted(pool.reused.items())}, "rotated_run": (v1 - 1) if rotate else None,
             "chunk_index": chunk_index, "id_range": [id_base_blocks * BLOCK, pool.next_block * BLOCK],
             "chunk_index_straddle": getattr(pool, "straddle", [])}       # used ids whose 29-byte record crosses an 8 MiB slice boundary
    manifest = {"format": "hbkgen-1", "truth": truth, "root": root, "last_clean": v2, "incident": v3, "bucket_cap": bucket_cap, "stats": pool.stats,
                "versions": {str(v): {sh: {p: {"sha256": hashlib.sha256(e["data"]).hexdigest(), "size": len(e["data"]), "mtime": e["mtime"],
                                               "kind": e["info"]["kind"], "status": e["status"], "writer": e["info"].get("writer"),
                                               "buckets": sorted({pool.where[g][0] for g in (e["info"].get("src_ids") or e["info"].get("ids") or []) if g in pool.where})}
                                           for (s, p), e in sorted(snaps[v].items()) if s == sh} for sh in shares} for v in versions}}
    return manifest


def _write_config(root, shares, versions, ts, snaps, rev):
    cfg = os.path.join(root, "Config"); os.makedirs(os.path.join(cfg, "@Share"), exist_ok=True)
    os.makedirs(os.path.join(root, "Control"), exist_ok=True)
    with open(os.path.join(root, "_Syno_TaskConfig"), "w") as f:
        f.write('[task]\nname="E2E synthetic backup"\nhost_name="e2e-nas"\nbackup_folders=' + json.dumps(["/" + s for s in shares]) +
                '\ndata_compress_type=1\nenable_version_rotation=1\nenable_data_encrypt=0\n')
    con = sqlite3.connect(os.path.join(cfg, f"version_info.db.{rev}"))
    con.execute("create table version_info(id integer primary key, timestamp integer, status text, share text, depose_time integer)")
    for v in versions: con.execute("insert into version_info values (?,?,?,?,?)", (v, ts[v], "Complete", json.dumps(shares), 0))
    con.commit(); con.close()
    con = sqlite3.connect(os.path.join(root, f"storage_statistics.db.{rev}"))
    con.execute("create table source_table(version_id integer primary key, total_count integer, new_count integer, modify_count integer,"
                " remove_count integer, source_size integer, start_time integer, end_time integer)")
    prev = set()
    for v in versions:
        cur = snaps[v]
        con.execute("insert into source_table values (?,?,?,?,?,?,?,?)", (v, len(cur), sum(e["status"] == "N" for e in cur.values()),
                    sum(e["status"] == "M" for e in cur.values()), len(prev - set(cur)), sum(len(e["data"]) for e in cur.values()), ts[v], ts[v] + 900))
        prev = set(cur)
    con.commit(); con.close()
    for sh in shares:
        d = os.path.join(cfg, "@Share", sh); os.makedirs(d, exist_ok=True)
        for v in versions:
            con = sqlite3.connect(os.path.join(d, f"{v}.db.{rev}"))
            con.execute("""create table version_list(name_id_v2 blob, pname_id_v2 blob, file_name text, mtime_sec integer, ctime_sec integer,
                           size integer, mode integer, status text, inode integer, off_virtual_file integer, dedup_id integer, version_id integer,
                           cr_time integer, nlink integer, tag blob)""")
            rows = []; dirs = set(); inode = [1000]
            def add_dir(path):
                if not path or path in dirs: return
                parent = path.rsplit("/", 1)[0] if "/" in path else ""
                add_dir(parent); dirs.add(path)
                rows.append((nid(sh, path), nid(sh, parent) if parent else ROOT_PARENT, path.rsplit("/", 1)[-1], ts[versions[0]] - 90 * 86400,
                             ts[versions[0]] - 90 * 86400, 0, 0o040755, "U", 0, 0, 0, v, ts[versions[0]] - 90 * 86400, 2, None))
            for (s, p), e in sorted(snaps[v].items()):
                if s != sh: continue
                parent = p.rsplit("/", 1)[0] if "/" in p else ""; add_dir(parent); inode[0] += 1
                rows.append((nid(sh, p), nid(sh, parent) if parent else ROOT_PARENT, p.rsplit("/", 1)[-1], e["mtime"], e["ctime"], len(e["data"]),
                             0o100644, e["status"], inode[0], e["off"], 0, v, e["mtime"], 1, e["tag"]))
            con.executemany("insert into version_list values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows); con.commit(); con.close()


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("out", help="folder to create, e.g. /tmp/e2e/e2e_1.hbk (must not exist or be empty)")
    ap.add_argument("--scale", type=float, default=1.0, help="file size factor (1 = ~6 MB)")
    ap.add_argument("--bucket-kb", type=int, default=256, help="bucket size in KiB (real backups: ~52 MB)")
    ap.add_argument("--chunk-index", action="store_true", help="write Pool/chunk_index (the backup's own chunk table)")
    ap.add_argument("--id-base-blocks", type=int, default=0, help="start chunk ids this many 2260-id blocks up (128: a record straddles the 8 MiB slice)")
    ap.add_argument("--rotate", action="store_true", help="add a rotated-away run: purged buckets, reused bucket ids, copy of an untagged original")
    a = ap.parse_args(argv)
    m = build(a.out, a.scale, a.bucket_kb * 1024, rotate=a.rotate, chunk_index=a.chunk_index, id_base_blocks=a.id_base_blocks)
    mp = a.out.rstrip("/\\") + ".manifest.json"
    with open(mp, "w") as f: json.dump(m, f, indent=1)
    print(f"{a.out}: {m['stats']}; manifest {mp}")


if __name__ == "__main__":
    main(sys.argv[1:])
