"""Build a small synthetic Hyper Backup (.hbk) folder with neutral names for README screenshots.

Layout follows docs/FORMAT.md as far as the browsing UI needs it: Config/version_info.db.<rev>, storage_statistics.db.<rev>,
Config/@Share/<share>/<version>.db.<rev> (table version_list), an empty Pool/ and Control/. No data pool, so restores do not
run; the timeline, browser, search, compare and the restore basket all work.
"""
import hashlib, os, random, sqlite3, sys, time

ROOT_PARENT = bytes.fromhex("5058F1AF5058F1AF8388633F609CADB75A75DC9D")
DAY = 86400
random.seed(7)

# weekly runs, the last one after a ransomware incident
T0 = int(time.mktime((2026, 5, 3, 2, 0, 0, 0, 0, -1)))            # Sun 3 May 2026 02:00
VERSIONS = [1201 + i for i in range(12)]
TS = {v: T0 + i * 7 * DAY + random.randint(0, 900) for i, v in enumerate(VERSIONS)}
LAST, INCIDENT = VERSIONS[-1], VERSIONS[-1]
REV = LAST

SHARES = {
    "Documents": {
        "Finance/2025": [("invoices-q1.xlsx", 184_320), ("invoices-q2.xlsx", 201_114), ("tax-return-2025.pdf", 1_240_050)],
        "Finance/2026": [("invoices-q1.xlsx", 190_221), ("budget-2026.xlsx", 96_400)],
        "Contracts": [("lease-office.pdf", 2_310_450), ("supplier-agreement.pdf", 880_112), ("nda-template.docx", 48_120)],
        "Projects/Website": [("brief.docx", 61_200), ("sitemap.png", 410_880), ("notes.txt", 2_310)],
        "Projects/Garden": [("plan.pdf", 1_920_000), ("plants.xlsx", 22_400)],
        "Manuals": [("router.pdf", 4_120_000), ("dishwasher.pdf", 2_800_333), ("camera.pdf", 6_100_200)],
    },
    "Photos": {
        "2024/Summer": [(f"IMG_{n}.jpg", random.randint(2_000_000, 6_500_000)) for n in range(4101, 4113)],
        "2025/Winter": [(f"IMG_{n}.jpg", random.randint(2_000_000, 6_500_000)) for n in range(5210, 5226)],
        "2026/Spring": [(f"IMG_{n}.jpg", random.randint(2_000_000, 6_500_000)) for n in range(6002, 6020)],
        "2026/Birthday": [(f"IMG_{n}.jpg", random.randint(2_000_000, 6_500_000)) for n in range(6301, 6309)] + [("party.mp4", 388_000_000)],
    },
    "homes": {
        "alice/Desktop": [("todo.txt", 1_020), ("meeting-notes.docx", 33_200)],
        "alice/Documents": [("thesis-draft.docx", 1_204_000), ("references.bib", 51_000), ("cv.pdf", 210_400)],
        "alice/Downloads": [("setup.dmg", 92_000_000), ("report.pdf", 1_100_000)],
        "bob/Documents": [("recipes.docx", 120_000), ("insurance.pdf", 640_200)],
        "bob/Music/Demos": [("track01.mp3", 7_200_000), ("track02.mp3", 6_900_000), ("track03.mp3", 8_100_000)],
    },
}

# per-share growth: which folders exist from which version on
FIRST_SEEN = {
    "Documents": {"Finance/2026": 1203, "Projects/Garden": 1206},
    "Photos": {"2026/Spring": 1204, "2026/Birthday": 1209},
    "homes": {"alice/Downloads": 1205, "bob/Music/Demos": 1207},
}
# files modified in a given version (status M)
MODIFIED = {1202: ["notes.txt", "todo.txt"], 1204: ["budget-2026.xlsx", "thesis-draft.docx"], 1205: ["invoices-q1.xlsx"],
            1207: ["thesis-draft.docx", "plants.xlsx", "todo.txt"], 1208: ["brief.docx"], 1210: ["thesis-draft.docx", "meeting-notes.docx"],
            1211: ["invoices-q2.xlsx", "cv.pdf"]}
# removed in a version (absent from that version on)
REMOVED = {1208: ["sitemap.png"], 1210: ["setup.dmg"]}


def nid(share, path):
    return hashlib.sha1(f"{share}/{path}".encode()).digest()


def rows_for(share, ver):
    """version_list rows of one share at one version."""
    out = []
    seen_dirs = set()
    ts_run = TS[ver]
    def add_dir(path):
        if path in seen_dirs: return
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        if parent: add_dir(parent)
        seen_dirs.add(path)
        name = path.rsplit("/", 1)[-1]
        first = TS[VERSIONS[0]] - 200 * DAY
        out.append((nid(share, path), nid(share, parent) if parent else ROOT_PARENT, name, first, first, 0, 0o040755, "U", 0, None, 0, ver, first, 2))
    removed = {n for v, names in REMOVED.items() if v <= ver for n in names}
    for folder, files in SHARES[share].items():
        if FIRST_SEEN[share].get(folder, 0) > ver: continue
        add_dir(folder)
        for i, (name, size) in enumerate(files):
            if name in removed: continue
            path = f"{folder}/{name}"
            born = FIRST_SEEN[share].get(folder, VERSIONS[0])
            created = TS[born] - random.randint(1, 20) * DAY
            mtime = created
            status = "N" if born == ver else "U"
            for v in VERSIONS:
                if v > ver: break
                if name in MODIFIED.get(v, []):
                    mtime = TS[v] - random.randint(1, 5) * DAY
                    if v == ver: status = "M"
            locked = ver == INCIDENT and share != "Photos" and folder.startswith(("Finance", "Contracts", "alice", "bob/Documents"))
            fname, fsize = name, size
            if locked:
                fname, fsize, mtime, status = name + ".L0CK3D", size + 512, TS[ver] - 3600 * 5, "N"
            out.append((nid(share, path + ("#L" if locked else "")), nid(share, folder), fname, mtime, mtime if not locked else mtime,
                        fsize, 0o100644, status, 100_000 + i, (-(100 + i) if fsize < 50_000 else random.randint(1, 1 << 30)), 0, ver, created, 1))
        if ver == INCIDENT and share != "Photos" and folder.startswith(("Finance", "Contracts", "alice", "bob/Documents")):
            note = "HOW_TO_RECOVER_FILES.txt"
            out.append((nid(share, f"{folder}/{note}"), nid(share, folder), note, TS[ver] - 3600 * 5, TS[ver] - 3600 * 5, 1_480, 0o100644, "N",
                        199_000, -999, 0, ver, TS[ver] - 3600 * 5, 1))
    return out


def main(root):
    root = os.path.abspath(root)
    cfg = os.path.join(root, "Config")
    os.makedirs(os.path.join(root, "Pool", "0"), exist_ok=True)
    os.makedirs(os.path.join(root, "Control"), exist_ok=True)
    os.makedirs(os.path.join(cfg, "@Share"), exist_ok=True)
    with open(os.path.join(root, "_Syno_TaskConfig"), "w") as f:
        f.write('[task]\nname="Weekly backup"\nhost_name="nas"\nbackup_folders=["/Documents","/Photos","/homes"]\n'
                'data_compress_type=1\nenable_version_rotation=1\nenable_data_encrypt=0\n')
    # version_info
    p = os.path.join(cfg, f"version_info.db.{REV}")
    if os.path.exists(p): os.unlink(p)
    con = sqlite3.connect(p)
    con.execute("create table version_info(id integer primary key, timestamp integer, status text, share text, depose_time integer)")
    for v in VERSIONS:
        con.execute("insert into version_info values (?,?,?,?,?)", (v, TS[v], "Done", ",".join(SHARES), 0))
    con.commit(); con.close()
    # storage statistics
    p = os.path.join(root, f"storage_statistics.db.{REV}")
    if os.path.exists(p): os.unlink(p)
    con = sqlite3.connect(p)
    con.execute("create table source_table(version_id integer primary key, total_count integer, new_count integer, modify_count integer, remove_count integer, source_size integer, start_time integer, end_time integer)")
    prev = {}
    for v in VERSIONS:
        cur = {}
        for sh in SHARES:
            for r in rows_for(sh, v):
                if r[6] & 0o170000 != 0o040000: cur[(sh, r[0])] = (r[5], r[7])
        new = sum(1 for k, (_, s) in cur.items() if s == "N"); mod = sum(1 for k, (_, s) in cur.items() if s == "M")
        rem = sum(1 for k in prev if k not in cur)
        con.execute("insert into source_table values (?,?,?,?,?,?,?,?)", (v, len(cur), new, mod, rem, sum(s for s, _ in cur.values()), TS[v], TS[v] + random.randint(600, 2400)))
        prev = cur
    con.commit(); con.close()
    # per-share snapshot dbs
    for sh in SHARES:
        d = os.path.join(cfg, "@Share", sh); os.makedirs(d, exist_ok=True)
        for v in VERSIONS:
            p = os.path.join(d, f"{v}.db.{REV}")
            if os.path.exists(p): os.unlink(p)
            con = sqlite3.connect(p)
            con.execute("""create table version_list(name_id_v2 blob, pname_id_v2 blob, file_name text, mtime_sec integer, ctime_sec integer,
                           size integer, mode integer, status text, inode integer, off_virtual_file integer, dedup_id integer, version_id integer,
                           cr_time integer, nlink integer)""")
            con.executemany("insert into version_list values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows_for(sh, v))
            con.commit(); con.close()
    print(root, "versions", VERSIONS[0], "..", LAST)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "backup_1.hbk")
