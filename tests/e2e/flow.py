"""Drive the app's own engine through the whole flow on a synthetic backup and compare what lands on disk with the manifest.

Used by tests/e2e/test_e2e.py (in-process, CI) and runnable by hand:  python -m tests.e2e.flow [--keep]
Everything lives in one temporary folder: the backup, the app's settings folder (paths.*) and the restore targets."""
import hashlib, json, os, sys, tempfile, time
from pathlib import Path


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""): h.update(b)
    return h.hexdigest()


def compare(manifest, ver, share_roots, only=None):
    """{missing, wrong_bytes, wrong_mtime, extra, ok} for the restored tree(s); share_roots: share -> folder it was restored into.
    `only(share, path)` limits which manifest files are expected."""
    out = {"ok": [], "missing": [], "wrong_bytes": [], "wrong_mtime": [], "extra": []}
    want = manifest["versions"][str(ver)]
    for share, root in share_roots.items():
        exp = {p: e for p, e in want.get(share, {}).items() if only is None or only(share, p)}
        for p, e in exp.items():
            f = Path(root, *p.split("/"))
            if not f.is_file(): out["missing"].append(f"{share}/{p} ({e['kind']})"); continue
            if sha256(f) != e["sha256"]: out["wrong_bytes"].append(f"{share}/{p} ({e['kind']}, {f.stat().st_size} vs {e['size']} bytes)"); continue
            if int(f.stat().st_mtime) != e["mtime"]: out["wrong_mtime"].append(f"{share}/{p}: {int(f.stat().st_mtime)} vs {e['mtime']}")
            else: out["ok"].append(f"{share}/{p}")
        if Path(root).is_dir():
            for f in Path(root).rglob("*"):
                if f.is_file():
                    rel = f.relative_to(root).as_posix()
                    if rel not in exp: out["extra"].append(f"{share}/{rel}")
    return out


def run_inprocess(work, scale=1.0, bucket_kb=256, log=print, rotate=False, rotate_copy=True):
    """The whole flow against the engine (Backup + the real mapper in-process). Returns a report dict."""
    from tests.e2e import hbkgen
    from callido_restore import paths
    from callido_restore.pool import poolmap
    from callido_restore.restore.backup import Backup
    from callido_restore.sources.source import Source
    work = Path(work); hbk = work / "e2e_1.hbk"
    t0 = time.time(); manifest = hbkgen.build(str(hbk), scale, bucket_kb * 1024, rotate=rotate, rotate_copy=rotate_copy); rep = {"gen_s": round(time.time() - t0, 2), "stats": manifest["stats"]}
    old = (paths.CONF_DIR, paths.INDEX_DIR, paths.CACHE_DIR, paths.JOBS_DIR, poolmap.DETACHED)
    paths.CONF_DIR = work / "conf"; paths.INDEX_DIR = work / "conf" / "index"; paths.CACHE_DIR = work / "conf" / "cache"; paths.JOBS_DIR = work / "conf" / "jobs"
    poolmap.DETACHED = False
    try:
        src = Source("e2e", "e2e", str(hbk)); b = Backup(src)
        rep["open"] = {"readable": b.info["readable"], "problems": b.info["problems"], "shares": b.shares, "versions": [v["id"] for v in b.versions],
                       "ransomware": b.info["ransomware"]}
        last_clean = manifest["last_clean"]
        for ver in (manifest["incident"], last_clean, min(int(v) for v in manifest["versions"])):
            for sh in b.shares: b.build_index(sh, ver, {"id": "idx"})
        job = {"id": "prep", "label": "build pool map"}; t0 = time.time()
        rep["prepare"] = b.build_pool(job); rep["prepare_s"] = round(time.time() - t0, 2); rep["prepare_msg"] = job.get("message")
        rep["pool_status"] = {k: v for k, v in b.pool_status().items() if k in ("map", "mapped_buckets", "total_buckets", "complete")}
        mp = Path(paths.INDEX_DIR, "e2e", "pool", "poolmap2.json")
        if mp.is_file():
            d = json.loads(mp.read_text()); rep["map"] = {"mapped_upto": d.get("mapped_upto"), "stats": d.get("stats"),
                                                          "placed": sum(len(v) for v in d["placements"].values()),
                                                          "guessed": sum(len(v) for v in d.get("guessed", {}).values()),
                                                          "aliased": sum(len(v) for v in d.get("aliased", {}).values())}
        rep["restores"] = {}
        for ver in sorted(int(v) for v in manifest["versions"]):          # every run, the incident run included
            dest = work / f"restore-v{ver}"; dest.mkdir()
            items = [{"share": sh, "ver": ver, "path": "", "is_dir": True} for sh in b.shares]
            job = {"id": f"r{ver}", "label": "restore", "started": time.time()}; t0 = time.time()
            res = b.restore(items, str(dest), job)
            cmp_ = compare(manifest, ver, {sh: dest / sh for sh in b.shares})
            rep["restores"][ver] = {"result": res, "errors": job["errors"], "unverified": job["unverified"], "secs": round(time.time() - t0, 2),
                                    **{k: (len(v) if k == "ok" else v) for k, v in cmp_.items()}}
        # T8: a second restore into the same folder writes nothing and counts every file as skipped
        dest = work / f"restore-v{last_clean}"; before = {p: p.stat().st_mtime_ns for p in dest.rglob("*") if p.is_file()}
        victim = next(p for p in sorted(before) if p.stat().st_size > 0); victim.write_bytes(b"user changed this"); before[victim] = victim.stat().st_mtime_ns
        job = {"id": "again", "label": "restore", "started": time.time()}
        res = b.restore([{"share": sh, "ver": last_clean, "path": "", "is_dir": True} for sh in b.shares], str(dest), job)
        after = {p: p.stat().st_mtime_ns for p in dest.rglob("*") if p.is_file()}
        rep["rerun"] = {"result": res, "changed": [str(p) for p in after if before.get(p) != after[p]], "victim_kept": victim.read_bytes() == b"user changed this",
                        "new_files": [str(p) for p in after if p not in before]}
    finally:
        paths.CONF_DIR, paths.INDEX_DIR, paths.CACHE_DIR, paths.JOBS_DIR, poolmap.DETACHED = old
    rep["manifest"] = manifest
    return rep


def summary(rep):
    lines = [f"generated in {rep['gen_s']}s: {rep['stats']}", f"open: {rep['open']}", f"prepare ({rep.get('prepare_s')}s): {rep.get('prepare')} · {rep.get('prepare_msg')}",
             f"pool: {rep.get('pool_status')}", f"map: {rep.get('map')}"]
    for ver, r in rep.get("restores", {}).items():
        lines.append(f"restore v{ver} ({r['secs']}s): {r['result']}; ok={r['ok']} missing={r['missing']} wrong_bytes={r['wrong_bytes']} wrong_mtime={r['wrong_mtime']} extra={r['extra']}")
        for e in r["errors"]: lines.append(f"   error {e}")
        for u in r["unverified"]: lines.append(f"   unverified {u}")
    if "rerun" in rep: lines.append(f"re-run into the same folder: {rep['rerun']}")
    return "\n".join(lines)


if __name__ == "__main__":
    keep = "--keep" in sys.argv
    work = tempfile.mkdtemp(prefix="hbk-e2e-")
    rep = run_inprocess(work)
    print(summary(rep)); print("work folder:", work if keep else "(removed)")
    if not keep:
        import shutil; shutil.rmtree(work, ignore_errors=True)
