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

## Small files
`off_virtual_file < 0` → `Pool/file_pool/<-off>.file.<N>`: 0x140-byte header, then chunks `[u16 0xe5bb][u32 clen][u32 ulen][u16 0][LZ4 block]`.
`file_pool_map.db` holds md5 of the whole file.

## Buckets
`<n>.index`: 64-byte header, 32-byte entries `[u32 clen][u32 off][u32 ulen][md5 16][crc32 of the first 28 bytes]`.
`<n>.bucket`: concatenation of raw **LZ4 block** compressed chunks at `off`. Chunk ≤ 32 KiB, content-defined (~8 KiB avg).
md5 is of the decompressed chunk. Chunks of a file are consecutive in the writer's own bucket sequence; a bucket is closed
at ~52 MB and gets its id in **fill-completion order** across all writers (so bucket ids interleave between writers).

## Chunk id → bucket (the hard part)
Nothing in the backup stores it. `hbk_poolmap.py` reconstructs it:
1. parse all records; a writer's *new* chunks are the runs that continue its allocation frontier (`g == frontier+1` or a new block);
   everything else is a dedup reference. This yields block ownership and, per writer, the ordered "pieces" of stored chunks.
2. file sizes come from the snapshot databases (record → size).
3. walk bucket ids in order; for each bucket try each writer: its next positions map to the bucket's entries; whenever a file's last
   chunk is reached, `sum(ulen)` must equal the file size. A writer with an exact match wins; a bucket with several candidates and
   no evidence is branched on (backtracking).
Result: `poolmap.json` = `bucket → (writer, first stored position)` + piece tables.
