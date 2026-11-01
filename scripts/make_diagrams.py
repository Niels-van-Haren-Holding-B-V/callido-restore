#!/usr/bin/env python3
"""Generate the architecture diagrams in docs/diagrams/*.svg (light and dark, from one source).

    python3 scripts/make_diagrams.py

Plain SVG with an embedded stylesheet: the colours switch with prefers-color-scheme, so the pictures read well on GitHub in both
themes. No personal data; names are the module and file names of this repository and the .hbk format.
"""
import html, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "diagrams")

STYLE = """
<style>
  .bg{fill:#ffffff} .t{fill:#1d1d1f;font:13px -apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif}
  .s{fill:#6e6e73;font:11.5px -apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif}
  .m{fill:#1d1d1f;font:12px ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
  .h{fill:#1d1d1f;font:600 14px -apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif}
  .band{fill:#f5f5f7;stroke:none}
  .box{fill:#ffffff;stroke:#c7c7cc;stroke-width:1.2}
  .acc{fill:#f3edff;stroke:#7c3aed;stroke-width:1.3}
  .ext{fill:#eef6ff;stroke:#2f6fd6;stroke-width:1.2}
  .ok{fill:#ecf8f0;stroke:#1f9d55;stroke-width:1.2}
  .warn{fill:#fff6e8;stroke:#c27c0e;stroke-width:1.2;stroke-dasharray:5 3}
  .a{stroke:#6e6e73;stroke-width:1.4;fill:none;marker-end:url(#arr)}
  .ad{stroke:#6e6e73;stroke-width:1.3;fill:none;stroke-dasharray:4 3;marker-end:url(#arr)}
  .ah{fill:#6e6e73}
  .n{fill:#7c3aed} .nt{fill:#ffffff;font:600 11px -apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif}
  @media (prefers-color-scheme: dark){
    .bg{fill:#1c1c1e} .t,.m,.h{fill:#f5f5f7} .s{fill:#a1a1a6}
    .band{fill:#2c2c2e} .box{fill:#1c1c1e;stroke:#48484a}
    .acc{fill:#2a2140;stroke:#a78bfa} .ext{fill:#15263d;stroke:#5b9bff} .ok{fill:#132a1d;stroke:#3fbf77}
    .warn{fill:#33270f;stroke:#e0a33a}
    .a,.ad{stroke:#a1a1a6} .ah{fill:#a1a1a6} .n{fill:#a78bfa} .nt{fill:#1c1c1e}
  }
</style>
<defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
  <path class="ah" d="M0,0 L10,5 L0,10 z"/></marker></defs>
"""


def esc(s): return html.escape(s, quote=True)


class SVG:
    def __init__(self, w, h, title):
        self.w, self.h, self.parts = w, h, []
        self.title = title

    def add(self, s): self.parts.append(s)

    def rect(self, x, y, w, h, cls="box", r=8): self.add(f'<rect class="{cls}" x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}"/>')

    def text(self, x, y, s, cls="t", anchor="start"): self.add(f'<text class="{cls}" x="{x}" y="{y}" text-anchor="{anchor}">{esc(s)}</text>')

    def box(self, x, y, w, h, title, lines=(), cls="box", mono_title=False):
        self.rect(x, y, w, h, cls)
        self.text(x + 12, y + 21, title, "m" if mono_title else "h")
        for i, l in enumerate(lines): self.text(x + 12, y + 40 + 16 * i, l, "s")

    def arrow(self, x1, y1, x2, y2, dashed=False, label=None, lx=None, ly=None, anchor="middle"):
        self.add(f'<path class="{"ad" if dashed else "a"}" d="M{x1},{y1} L{x2},{y2}"/>')
        if label: self.text(lx if lx is not None else (x1 + x2) / 2, ly if ly is not None else (y1 + y2) / 2 - 6, label, "s", anchor)

    def path(self, d, dashed=False): self.add(f'<path class="{"ad" if dashed else "a"}" d="{d}"/>')

    def num(self, x, y, n):
        self.add(f'<circle class="n" cx="{x}" cy="{y}" r="10"/>'); self.text(x, y + 4, str(n), "nt", "middle")

    def save(self, name):
        os.makedirs(OUT, exist_ok=True)
        body = "\n".join(self.parts)
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" viewBox="0 0 {self.w} {self.h}" '
               f'role="img" aria-label="{esc(self.title)}">\n<title>{esc(self.title)}</title>{STYLE}'
               f'<rect class="bg" x="0" y="0" width="{self.w}" height="{self.h}"/>\n{body}\n</svg>\n')
        with open(os.path.join(OUT, name), "w") as f: f.write(svg)
        print(os.path.join(OUT, name))


def layers():
    d = SVG(960, 624, "Callido Restore: package layers and who calls whom")
    d.text(24, 32, "Package layers: a layer imports only the layers below it", "h")
    d.text(24, 52, "callido_restore/ · arrows: calls / imports · dashed: separate processes", "s")
    bands = [("UI", 70, 70), ("app", 150, 70), ("server", 230, 70), ("restore", 310, 80), ("domain", 400, 120), ("base", 530, 82)]
    for name, y, h in bands:
        d.rect(16, y, 928, h, "band", 10); d.text(30, y + 22, name, "s")
    d.box(110, 80, 330, 50, "static/index.html", ["single page, no build step, polls /api/job"], "box", True)
    d.box(520, 80, 400, 50, "pywebview window (or the browser)", ["WebKit on macOS, Edge WebView2 on Windows"], "ext")
    d.box(110, 160, 330, 50, "app/main.py", ["CLI, app.log, window; child dispatch"], "box", True)
    d.box(520, 160, 400, 50, "child processes (same app binary)", ["--mapper and --scan-worker ×4: run pool/ and core/"], "warn")
    d.box(110, 240, 810, 50, "server/api.py", ["JSON API + static UI on 127.0.0.1 · auto-start of the preparation (POOL_LOCK) · jobs"], "acc", True)
    d.box(110, 320, 400, 60, "restore/backup.py", ["Backup: shares, runs, snapshot index (SQLite),", "queries, build_pool, restore (prefetch, resume)"], "box", True)
    d.box(530, 320, 220, 60, "restore/destination.py", ["never overwrite or delete", "place_file, list_destination"], "box", True)
    d.box(770, 320, 150, 60, "restore/jobs.py", ["start_job, JOBS"], "box", True)
    d.box(110, 410, 250, 100, "core/", ["pool.py: PoolReader,", "ChunkIndex (Pool/chunk_index),", "chunk records, buckets, LZ4", "hbkmeta.py: revisions, flags"], "box", True)
    d.box(380, 410, 250, 100, "pool/", ["the inferred map (no chunk", "table): anchor.py scan,", "place.py placer v3,", "indexfetch.py, poolmap*.py"], "box", True)
    d.box(650, 410, 270, 100, "sources/", ["source.py: Source, ensure (cache)", "rclone.py: daemon, ls, copyfile", "transfer.py: every transfer number", "google.py: key checks"], "box", True)
    d.box(110, 540, 360, 62, "paths.py · about.py · oscompat.py", ["settings folder, bundled rclone, version,", "Windows/macOS process differences"], "box", True)
    d.box(520, 540, 400, 62, "rclone rcd (one daemon per app)", ["127.0.0.1, random port and credentials;", "Google Drive · SMB · any rclone remote"], "ext")
    for x in (275,): d.arrow(x, 130, x, 160)
    d.arrow(440, 105, 520, 105, label="shows", ly=99)
    d.arrow(275, 210, 275, 240)
    d.arrow(440, 185, 520, 185, dashed=True, label="starts", ly=179)
    d.arrow(275, 290, 275, 320)
    d.arrow(640, 290, 640, 320); d.arrow(845, 290, 845, 320)
    d.arrow(200, 380, 200, 410); d.arrow(430, 380, 470, 410); d.arrow(480, 380, 700, 410)
    d.arrow(640, 380, 760, 410)
    d.arrow(360, 460, 380, 460)
    d.path("M630,500 L650,500", dashed=False)
    d.arrow(240, 510, 240, 540); d.arrow(785, 510, 785, 540, label="HTTP rc API", lx=795, ly=528, anchor="start")
    d.save("layers.svg")


def restore_flow():
    d = SVG(980, 592, "Callido Restore: how a restore moves data")
    d.text(24, 32, "How a restore moves data", "h")
    d.text(24, 52, "read once, verify every chunk, never overwrite: from the backup on Drive (or a NAS, or a disk) to the target", "s")
    # row 1: the backup and the transfer
    d.box(24, 80, 200, 74, "the backup (.hbk)", ["Google Drive · NAS over SMB ·", "a local or mounted disk"], "ext")
    d.box(304, 80, 240, 74, "rclone daemon", ["one backend per connection,", "throttled, retried (transfer.py)"], "ext")
    d.box(624, 80, 332, 74, "app cache  ~/.hbk-explorer/cache", ["slices and bucket files on demand;", "LRU ≤ 20 GB, several buckets at once"], "box")
    d.arrow(224, 117, 304, 117, label="read only", ly=108)
    d.arrow(544, 117, 624, 117, label="copyfile", ly=108)
    # row 2: locating a file's chunks
    d.text(24, 196, "PoolReader: where are the pieces of this file?", "h")
    steps = [
        ("file row", ["snapshot index:", "path, size, mtime,", "off_virtual_file"]),
        ("virtual_file", ["56-byte entry:", "pointer A → the", "file's chunk list"]),
        ("file_chunk set", ["record: chunk ids", "g = (ref − 64) / 29"]),
        ("Pool/chunk_index", ["29-byte record per g:", "bucket id + entry", "(or: a copy → follow)"]),
        ("bucket .index", ["32-byte entry:", "clen, offset, ulen,", "md5 of the chunk"]),
    ]
    x = 24; w = 176; gap = 12
    for i, (t, ls) in enumerate(steps):
        d.box(x, 210, w, 92, t, ls, "acc" if t == "Pool/chunk_index" else "box", True)
        if i: d.arrow(x - gap, 256, x, 256)
        x += w + gap
    d.box(588, 318, 176, 66, "inferred map", ["no chunk table: pool/,", "built once"], "warn")
    d.path("M676,318 L676,304", dashed=True)
    # row 3: decode, verify, write
    d.text(24, 400, "decode, verify and write", "h")
    d.box(24, 440, 196, 92, ".bucket data", ["LZ4 block at offset,", "decompressed to ulen"], "box", True)
    d.box(250, 440, 196, 92, "md5 check", ["every chunk against", "its index entry; file", "size against the row"], "ok")
    d.box(476, 440, 196, 92, "staging folder", ["per target root, same", "disk as the target;", "removed afterwards"], "box")
    d.box(702, 440, 254, 92, "the target", ["local: place_file (no overwrite)", "NAS: rclone moveto", "--ignore-existing"], "ok")
    d.path("M864,302 L864,410 L122,410 L122,440")
    d.arrow(220, 486, 250, 486); d.arrow(446, 486, 476, 486); d.arrow(672, 486, 702, 486)
    d.text(24, 560, "Files already at the target are skipped; nothing there is ever overwritten or deleted. A stopped restore resumes at the next file.", "s")
    d.text(24, 580, "Small files (off_virtual_file < 0) come whole from Pool/file_pool instead of buckets.", "s")
    d.save("restore-flow.svg")


def layout():
    d = SVG(980, 560, "The .hbk backup on disk and how a file is found")
    d.text(24, 32, "A Hyper Backup (.hbk) on disk, and how a file is found in it", "h")
    d.text(24, 52, "every file name carries a revision suffix .<N>; the newest N wins · details in docs/FORMAT.md", "s")
    d.rect(24, 70, 430, 466, "box", 10)
    tree = [
        (0, "backup_1.hbk/", ""),
        (1, "_Syno_TaskConfig", "task settings"),
        (1, "Config/", ""),
        (2, "version_info.db", "the backup runs"),
        (2, "@Share/<share>/<ver>.db", "one share at one run"),
        (2, "virtual_file.index/", "8 MiB slices"),
        (2, "file_chunk{1..4}.index/", "4 writers' records"),
        (1, "Pool/", ""),
        (2, "chunk_index/", "chunk id → bucket"),
        (2, "0/<d>/<n>.index", "chunk entries + md5"),
        (2, "0/<d>/<n>.bucket", "LZ4 data, ~52 MB"),
        (2, "file_pool/<n>.file", "small files, whole"),
        (1, "Guard/ · Control/", "integrity, locks"),
    ]
    ys = {}
    y = 100
    for depth, name, note in tree:
        d.text(44 + 22 * depth, y, name, "m");
        if note: d.text(440, y, note, "s", "end")
        ys[name] = y; y += 33
    nums = {"@Share/<share>/<ver>.db": 1, "virtual_file.index/": 2, "file_chunk{1..4}.index/": 3, "chunk_index/": 4, "0/<d>/<n>.index": 5, "0/<d>/<n>.bucket": 6}
    for name, n in nums.items(): d.num(30 + 0, ys[name] - 4, n) if False else d.num(468, ys[name] - 4, n)
    steps = [
        ("the file's row", "<ver>.db of the share: path, size, mtime and off_virtual_file"),
        ("its virtual-file entry", "at off_virtual_file: pointer A to the file's chunk list"),
        ("its chunk list", "a record in one of the 4 file_chunk streams: chunk ids, in order"),
        ("each chunk's place", "chunk_index record g: bucket id and entry (a copy points on)"),
        ("each chunk's entry", "the bucket's .index: compressed size, offset, size, md5"),
        ("the data", "the .bucket bytes at that offset, LZ4-decompressed, md5-checked"),
    ]
    y = 104
    for i, (t, s) in enumerate(steps, 1):
        d.num(518, y - 4, i); d.text(540, y, t, "h"); d.text(540, y + 18, s, "s"); y += 64
    d.rect(506, 492, 450, 44, "warn", 8)
    d.text(518, 510, "A backup without Pool/chunk_index: step 4 comes from", "s")
    d.text(518, 526, "the inferred map (pool/), built once in the background.", "s")
    d.save("hbk-layout.svg")


if __name__ == "__main__":
    layers(); restore_flow(); layout()
