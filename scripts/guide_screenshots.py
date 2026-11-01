#!/usr/bin/env python3
"""Regenerate the user-guide pictures (docs/screenshots/guide-*.png) with headless Chrome, from synthetic backups.

    python3 scripts/guide_screenshots.py --chrome "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --python .venv/bin/python

Two synthetic backups from tests/e2e/hbkgen.py (made-up names only: Documents, Photos, homes, alice, bob):
  * one with the backup's own chunk table (Pool/chunk_index), as real Hyper Backup writes it: steps 1-4, "Ready to restore";
  * one without it (an older layout, a rotated-away run), restored through the inferred map: the result line with checked files,
    files that could not be verified and errors.
The app runs with its own HOME (your ~/.hbk-explorer is never touched); the restore writes into the temporary folder.
Each picture is the bounding box of one part of the page, so it does not depend on fixed crop heights.
Run it with a Python that has Pillow to downsize and quantize the captures (otherwise the raw 2x PNGs are kept).
"""
import argparse, base64, json, os, shutil, subprocess, sys, tempfile, time, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, ROOT)
from screenshots import Browser, api, free_port, wait_job   # noqa: E402  (same CDP client as the README pictures)

PAD = 8


def save(b, out, name, selectors, width=1280):
    """Capture the union of the selectors' boxes (page coordinates) and write docs/screenshots/<name>.png."""
    box = b.js("""(() => {const els=%s.map(s=>document.querySelector(s)).filter(Boolean); if(!els.length) return null;
        const rs=els.map(e=>e.getBoundingClientRect());
        const x=Math.min(...rs.map(r=>r.left)), y=Math.min(...rs.map(r=>r.top)), r=Math.max(...rs.map(r=>r.right)), btm=Math.max(...rs.map(r=>r.bottom));
        return {x:x+scrollX, y:y+scrollY, w:r-x, h:btm-y}})()""" % json.dumps(selectors))
    if not box: raise RuntimeError(f"{name}: nothing matches {selectors}")
    clip = {"x": max(0, box["x"] - PAD), "y": max(0, box["y"] - PAD), "width": min(width, box["w"] + 2 * PAD), "height": box["h"] + 2 * PAD, "scale": 1}
    data = base64.b64decode(b.call("Page.captureScreenshot", format="png", clip=clip, captureBeyondViewport=True)["data"])
    p = os.path.join(out, name + ".png")
    with open(p, "wb") as f: f.write(data)
    try:
        from PIL import Image
        im = Image.open(p)
        if im.width > 1600: im = im.resize((1600, round(im.height * 1600 / im.width)), Image.LANCZOS)
        im.quantize(256, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE).save(p, optimize=True)
    except ImportError: pass
    print(p)


def capture(b, base, out, dest):
    sleep = lambda ms=700: time.sleep(ms / 1000)
    clear_toasts = lambda: b.js("document.querySelectorAll('#toasts > *').forEach(t=>t.remove())")
    b.navigate(base + "/")
    b.wait("typeof S!=='undefined' && S.info && S.info.readable && S.ver"); sleep()
    b.js("localStorage.setItem('basket','[]'); S.basket=[]; renderFlow(true)")
    b.js("document.querySelector('header').style.position='static'")   # a sticky header would cover the clipped regions

    # 1. Add a backup: where the .hbk lives
    b.js("openAddBackup()"); sleep(1000)
    save(b, out, "guide-1-add-backup", ["#dlgSrc"])
    b.js("$('#dlgSrc').close()"); sleep()

    # 2. A run's file lists: one chip per shared folder, loaded on click
    b.js("selectVersion(S.info.versions[S.info.versions.length-2].id)"); sleep(1500)   # the last clean run
    b.js("window.scrollTo(0, document.querySelector('#snap').offsetTop - 84)"); sleep()
    save(b, out, "guide-2-load-lists", ["#snapHead", "#snapShares"] if b.js("!!document.querySelector('#snapHead')") else ["#snapShares"])
    b.js("runIndex('*')")
    b.wait("Object.values(S.snaps||{}).every(i=>i.indexed||!i.available)", 120); sleep(1200)

    # 3. What to restore: the tree in the Restore tab (a whole share and one subfolder ticked)
    b.js("showTab('restore')"); sleep(1500)
    b.js("document.querySelector('#rsTree .tc[data-share=\"Documents\"][data-path=\"\"]').click()"); sleep(1500)
    b.js("document.querySelector('#rsTree .tc[data-share=\"Photos\"][data-path=\"\"]').closest('.tn').querySelector('.tog').click()"); sleep(1500)
    first = b.js("(document.querySelector('#rsTree .tc[data-share=\"Photos\"]:not([data-path=\"\"])')||{}).dataset?.path")
    if first: b.js(f"document.querySelector('#rsTree .tc[data-share=\"Photos\"][data-path={json.dumps(first)}]').click()"); sleep(1500)
    clear_toasts()
    b.js("document.querySelector('#rsTree').closest('.step').scrollIntoView()"); sleep()
    save(b, out, "guide-3-select", ["#rsTree"])

    # 4. Targets and step 4 "Check and start": this backup has its own chunk table, so it is ready at once
    b.js("{const r=$('#wzRem'); r.value=''; r.dispatchEvent(new Event('change'))}"); sleep(1000)
    b.js("""document.querySelectorAll('.mapPath').forEach((p,i)=>{const sh=document.querySelectorAll('#wzMap tbody td.mono')[i].firstChild.textContent.trim();
              p.value=%s+'/'+sh; p.dispatchEvent(new Event('input'))})""" % json.dumps(dest + "/Restore")); sleep(500)
    b.js("$('#rsPlan').click()"); b.wait("/files/.test($('#rsPlanOut').textContent)", 60); sleep(800); clear_toasts()
    b.js("{const s=$('#rsGo').closest('.step'); s.id='shotStep4'; s.scrollIntoView()}"); sleep()
    save(b, out, "guide-4-ready", ["#shotStep4"])

    # 5. The result of a restore from a backup without the chunk table: checked, could not be verified, errors
    b.js("$('#srcSel').value='older'; openSource()")
    b.wait("S.src==='older' && S.info && S.info.readable && S.ver", 120); sleep(1000)
    b.js("localStorage.setItem('basket','[]'); S.basket=[]; renderFlow(true)")
    b.js("selectVersion(S.info.versions[S.info.versions.length-2].id)"); sleep(1500)
    b.js("runIndex('*')"); b.wait("Object.values(S.snaps||{}).every(i=>i.indexed||!i.available)", 120); sleep(800)
    b.js("showTab('restore')"); sleep(1500)
    b.js("$('#rsAddAll').click()"); sleep(1500)
    b.wait("(async()=>{const s=await prepState();return s.complete&&!s.running})()", 600); b.js("tabRestore()"); sleep(1500)
    b.js("{const r=$('#wzRem'); r.value=''; r.dispatchEvent(new Event('change'))}"); sleep(1000)
    b.js("""document.querySelectorAll('.mapPath').forEach((p,i)=>{const sh=document.querySelectorAll('#wzMap tbody td.mono')[i].firstChild.textContent.trim();
              p.value=%s+'/'+sh; p.dispatchEvent(new Event('input'))})""" % json.dumps(dest + "/Restore-older")); sleep(500)
    b.js("$('#rsGo').click()")
    b.wait("/Restore (finished|paused|failed)/.test(($('#rsProg h4')||{}).textContent||'')", 600); sleep(1500); clear_toasts()
    b.js("$('#rsProg').scrollIntoView()"); sleep()
    save(b, out, "guide-5-result", ["#rsProg", "#rsUnverified", "#rsErrors"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chrome", required=True, help="path to a Chrome/Chromium binary")
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "screenshots"))
    ap.add_argument("--python", default=sys.executable, help="interpreter with the app's requirements installed")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    # a fixed, short work folder: the restore targets in the pictures read /tmp/callido-guide/Restore/<share>
    tmp = "/tmp/callido-guide"; shutil.rmtree(tmp, ignore_errors=True); os.makedirs(tmp)
    try:
        gen = [a.python, "-m", "tests.e2e.hbkgen"]
        ci, old = os.path.join(tmp, "ci", "backup_1.hbk"), os.path.join(tmp, "older", "backup_1.hbk")
        subprocess.check_call(gen + [ci, "--chunk-index", "--scale", "2"], cwd=ROOT, stdout=subprocess.DEVNULL)
        subprocess.check_call(gen + [old, "--rotate", "--scale", "2"], cwd=ROOT, stdout=subprocess.DEVNULL)
        dest = tmp
        for d in ("Restore", "Restore-older"): os.makedirs(os.path.join(dest, d))
        home = os.path.join(tmp, "home"); os.makedirs(os.path.join(home, ".hbk-explorer"))
        with open(os.path.join(home, ".hbk-explorer", "sources.json"), "w") as f:
            json.dump([{"id": "weekly", "name": "Weekly backup (Google Drive)", "local": ci, "remotes": None, "extra": []},
                       {"id": "older", "name": "Older backup (NAS)", "local": old, "remotes": None, "extra": []}], f)
        port = free_port(); base = f"http://127.0.0.1:{port}"
        srv = subprocess.Popen([a.python, "-m", "callido_restore", "--no-window", "--port", str(port)], cwd=ROOT,
                               env={**os.environ, "HOME": home}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(50):
                try: api(base, "/api/sources"); break
                except Exception: time.sleep(0.2)
            for src in ("weekly", "older"): api(base, "/api/open", src=src)
            with Browser(a.chrome, os.path.join(tmp, "chrome")) as b: capture(b, base, a.out, dest)
        finally:
            try: urllib.request.urlopen(f"{base}/api/quit", timeout=5)
            except Exception: pass
            srv.terminate()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
