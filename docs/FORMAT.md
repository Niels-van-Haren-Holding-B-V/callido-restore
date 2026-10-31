# Synology Hyper Backup `.hbk` format (as reverse engineered, Oct 2026)

Observed on a DSM 7 Hyper Backup task (HB 4.1.2) with a Google Drive target, **no client-side encryption**,
`data_compress_type=1`, multi-version. Every file name carries a revision suffix `.<N>` (`version_info.db.2334`);
always take the highest N. Numbers are big-endian unless stated.

## Top level
| Path | Content |
|---|---|
| `_Syno_TaskConfig` | INI text: task settings, `backup_folders`, `enable_data_encrypt`, schedule |
| `synobkpinfo.db` | SQLite, task id |
| `storage_statistics.db.<N>` | SQLite `source_table`: per version new/modified/removed counts (last ~360 runs) |
| `Config/version_info.db.<N>` | SQLite `version_info`: id, timestamp, status (Complete), share list json |
| `Config/@Share/<share>/<ver>.db.<N>` | SQLite snapshot of one share at one version (full listing, see below) |
| `Config/@Share/<share>/complete_list.db.<N>` | list of completed version ids |
| `Config/virtual_file.index/<k>.idx.<N>` | virtual file index, 8 MiB slices of one stream |
| `Config/file_chunk{1..4}.index/<k>.idx.<N>` | four chunk-record streams (one per writer thread), 8 MiB slices |
| `Pool/0/<d>/<n>.bucket.<N>` + `<n>.index.<N>` | data buckets (~52 MB) and their indexes; bucket id = d*2048+n |
| `Pool/file_pool/<n>.file.<N>` | small single files stored whole (chunked, LZ4) |
| `Pool/bucketID.counter.<N>` | u64 number of buckets |
| `Guard/cloud/*_bucket.db`, `*_file.db` | SQLite: md5 + size of every uploaded object (integrity) |
| `Control/` | lock/marker files only |

## Snapshot database `<ver>.db`
Table `version_list`: `name_id_v2` (20-byte id), `pname_id_v2` (parent id; share root = `5058F1AF5058F1AF8388633F609CADB75A75DC9D`),
`file_name`, `mtime_sec/nsec`, `ctime_sec/nsec`, `size`, `mode` (POSIX), `inode`, `nlink`, `status` (`N` new, `M` modified, `U` unchanged,
`D` deleted), `version_id`, `off_virtual_file`. Paths are rebuilt by following parent ids. Each snapshot is a full listing.

## Virtual file index
Stream = 64-byte header + 56-byte entries; `off_virtual_file` is the byte offset of the entry (entries may straddle slices).
Entry as 14 u32: `[0..1]` ptr A (u16 set, u48 offset) to the chunk list, `[2]` version, `[3]` flags (0x402 file), `[4]` 0x64,
`[6..7]` ctime, `[9..10]` mtime, `[11]` crc, `[12..13]` ptr B = A − 12 (start of the record). Directories have A = 0.

## Chunk record streams (`file_chunk<set>.index`)
TLV stream after a 64-byte header: `[u32 magic 0xa93bXXXX][u32 len][payload]`. File record:
`[u32 flags][0xa93b0200][u32 len]` then `len/8` u64 values: references, last one is a trailer `0x74000000 << 32 | crc32(ref bytes)`.
A reference `r` encodes a **global chunk id** `g = (r − 64) / 29`. Ids are handed out in **blocks of 2260** from one shared counter;
each writer (set 4→writer 0, set 3→1, set 1→2, set 2→3) takes a block when it needs ids. Other TLVs (`a93b0156/01b5/015b`
4-byte, `a93b0143` 28-byte) are markers.

### Attribute record before a chunk list
Some file records (photos on a real backup: 4,421 of 349,914 files in three shares) have a 28-byte attribute TLV in front of
the chunk-list header: `[0xa93b0186][u32 28][000000c9 01000000 12000000 01000000 08000000 ffffffff 00000000]`, the last
word doubling as the header's flags word, then `[0xa93b0200][u32 len]` and the list. Virtual-file pointer B then points at
the TLV's body, 24 bytes before the header; pointer A (words 0-1) points at the list itself, `A = B + 12` for a plain record
and `B + 36` for these. `ChunkRecords.record()` reads through pointer A (same set as B) and recognises the TLV from B.

## Small files
`off_virtual_file < 0` → `Pool/file_pool/<-off>.file.<N>`: 0x140-byte header, then chunks `[u16 0xe5bb][u32 clen][u32 ulen][u16 0][LZ4 block]`.
`file_pool_map.db` holds md5 of the whole file.

## Buckets
`<n>.index`: 64-byte header, 32-byte entries `[u32 clen][u32 off][u32 ulen][md5 16][crc32 of the first 28 bytes]`.
`<n>.bucket`: concatenation of raw **LZ4 block** compressed chunks at `off`. Chunk ≤ 32 KiB, content-defined (~8 KiB avg).
md5 is of the decompressed chunk. Chunks of a file are consecutive in the writer's own bucket sequence; a bucket is closed
at ~52 MB and gets its id in **fill-completion order** across all writers (so bucket ids interleave between writers).

## Chunk id → bucket: `Pool/chunk_index`
The backup's own table (format first documented by
[woutervanranst/synology-hyperbackup-reverse-engineering](https://github.com/woutervanranst/synology-hyperbackup-reverse-engineering),
README §2; confirmed and extended on a real backup). Sliced like the other indexes: `Pool/chunk_index/<n>.idx.<rev>`, 8 MiB
slices of one stream, the newest revision per slice, a 64-byte header once (magic `7053a86e`, record stride 29 at `0x10`).
A chunk reference `r` in a file record is the byte offset of a **29-byte record** here: `g = (r − 64) / 29`.

| byte | kind 0: a stored chunk | kind 1: a copy that stored nothing |
|---|---|---|
| 0 | `0x00` | `0x01` |
| 1–8 | u32 BE bucket id, u32 BE byte offset of the chunk's 32-byte entry in that bucket's `.index` (`64 + 32 · entry`) | u64 BE byte offset of the record it shares (follow it) |
| 9–23 | zero | zero |
| 24 | refcount | refcount |
| 25–28 | fingerprint | fingerprint |

Measured on the real backup (867 slices, 6.8 GB, 250 M records): 1.15 M kind-0 records agree with placements found by size
checks (0 disagreements); a 111-chunk copy's kind-1 records lead to its source's entries (111/111 same md5). Unused or purged
ids: not checked yet. `callido_restore/core/pool.py` `ChunkIndex` reads it (slices fetched on demand into the app cache, an
LRU of 32 slices in memory); `PoolReader.plan()` uses it first.

### When a backup has no `Pool/chunk_index`: the inferred map
`callido_restore/pool/` reconstructs the table from what else the backup holds (TASKS.md T1, docs/handovers): tagged
records are anchored in the bucket indexes (`version_list.tag` = md5 of the first K ≤ 9 chunk md5s + their length), bucket
ownership and stream offsets follow from the anchors, large files are laid out along the writers' bucket-id turn (quads in
the initial backup), copies alias their stored record, leftover chunks of deleted files are skipped by exact bytes, and every
placement with a known size is checked against it (`pool/place.py`). Result: `poolmap2.json`. On the real backup it located
86 % of the bytes; everything it placed agrees with the table.
