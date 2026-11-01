#!/usr/bin/env python3
"""Regenerate the README screenshots (docs/screenshots/*.png) from the demo backup, with headless Chrome.

    python3 scripts/screenshots.py --chrome "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --python .venv/bin/python

Run it with a Python that has Pillow (for the cropping); --python names the interpreter that runs the app server. It builds the demo backup (scripts/make_demo_backup.py) in a temporary folder, starts the app server there with its own
settings folder (HOME is overridden, so your real ~/.hbk-explorer is never touched), loads the file lists of the last two runs,
drives the UI through the DevTools protocol over --remote-debugging-pipe (no extra Python packages) and writes the pictures.
Pillow, if installed, crops and downsizes them; otherwise the raw 2x captures are kept.
"""
import argparse, base64, json, os, shutil, subprocess, sys, tempfile, time, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
W, H = 1280, 860
# name -> (viewport height while capturing, crop height in device pixels at 2x)
SHOTS = {"overview": (960, 1480), "changes": (900, 1730), "browse": (1000, 1792), "restore": (1080, 1939), "basket": (800, 1536)}


class Browser:
    """Minimal CDP client: one headless Chrome, one page."""
    def __init__(self, chrome, user_dir):
        self.chrome, self.user_dir, self.n, self.sid, self.buf = chrome, user_dir, 0, None, b""

    def __enter__(self):
        r_in, w_in = os.pipe(); r_out, w_out = os.pipe()
        def pre(): os.dup2(r_in, 3); os.dup2(w_out, 4)
        self.p = subprocess.Popen([self.chrome, "--headless=new", "--remote-debugging-pipe", "--no-first-run", "--no-default-browser-check",
                                   "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=2", f"--window-size={W},{H}",
                                   f"--user-data-dir={self.user_dir}", "about:blank"],
                                  preexec_fn=pre, pass_fds=(3, 4, r_in, w_out), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(r_in); os.close(w_out); self.w, self.r = w_in, r_out
        tid = self.call("Target.createTarget", url="about:blank")["targetId"]
        self.sid = self.call("Target.attachToTarget", targetId=tid, flatten=True)["sessionId"]
        self.call("Page.enable"); self.call("Runtime.enable"); self.viewport(H)
        return self

    def __exit__(self, *a):
        try: self.call("Browser.close", session=False)
        except Exception: pass
        try: self.p.wait(timeout=5)
        except Exception: self.p.kill()

    def _read(self):
        while b"\0" not in self.buf:
            chunk = os.read(self.r, 1 << 16)
            if not chunk: raise RuntimeError("chrome closed the pipe")
            self.buf += chunk
        msg, self.buf = self.buf.split(b"\0", 1)
        return json.loads(msg)

    def call(self, method, session=True, **params):
        self.n += 1
        m = {"id": self.n, "method": method, "params": params}
        if session and self.sid: m["sessionId"] = self.sid
        os.write(self.w, json.dumps(m).encode() + b"\0")
        while True:
            r = self._read()
            if r.get("id") == self.n:
                if "error" in r: raise RuntimeError(f"{method}: {r['error']}")
                return r.get("result", {})

    def viewport(self, height):
        self.call("Emulation.setDeviceMetricsOverride", width=W, height=height, deviceScaleFactor=2, mobile=False)

    def js(self, expr):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r: raise RuntimeError(r["exceptionDetails"].get("text"))
        return r.get("result", {}).get("value")

    def wait(self, expr, timeout=60):
        t0 = time.time()
        while not self.js(expr):
            if time.time() - t0 > timeout: raise TimeoutError(expr)
            time.sleep(0.2)

    def navigate(self, url):
        self.call("Page.navigate", url=url); self.wait("document.readyState === 'complete'")

    def png(self):
        return base64.b64decode(self.call("Page.captureScreenshot", format="png", captureBeyondViewport=False)["data"])


def free_port():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]


def api(base, path, **q):
    from urllib.parse import urlencode
    with urllib.request.urlopen(f"{base}{path}?{urlencode(q)}", timeout=60) as r: return json.load(r)


def wait_job(base, jid):
    while True:
        j = api(base, "/api/job", id=jid)
        if j.get("state") in ("done", "error"): return j
        time.sleep(0.5)


def capture(b, base, out):
    sleep = lambda ms=700: time.sleep(ms / 1000)
    to_run = lambda: (b.js("window.scrollTo(0, document.querySelector('#snap').offsetTop - 84)"), sleep())
    def shot(name):
        vh, crop = SHOTS[name]; b.viewport(vh); sleep(300)
        data = b.png(); b.viewport(H)
        p = os.path.join(out, name + ".png")
        with open(p, "wb") as f: f.write(data)
        try:
            from PIL import Image
            im = Image.open(p); im = im.crop((0, 0, im.width, min(crop, im.height)))
            im = im.resize((1600, round(im.height * 1600 / im.width)), Image.LANCZOS)
            im.quantize(256, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE).save(p, optimize=True)   # flat UI: 256 colours stay sharp, a third of the size
        except ImportError: pass
        print(p)
    b.navigate(base + "/")
    b.wait("typeof S!=='undefined' && S.info && S.info.readable && S.ver && Object.keys(S.snaps||{}).length>0"); sleep()
    b.js("localStorage.setItem('basket','[]'); S.basket=[]; renderFlow(true)")
    b.js("window.scrollTo(0,0)"); sleep(); shot("overview")                                   # latest run = the incident
    b.js("showTab('changes')"); sleep(1200); to_run(); shot("changes")                           # ransomware flags
    b.js("selectVersion(S.info.versions[S.info.versions.length-2].id)")                         # the last clean run
    b.wait("Object.keys(S.snaps||{}).length>0 && S.snaps[Object.keys(S.snaps)[0]].indexed"); sleep(800)
    b.js("BR.share='Photos'; BR.dir='2026/Birthday'; showTab('browse')"); sleep(1500); to_run(); shot("browse")
    b.js("""addBasket({share:'Documents',path:'Finance',is_dir:1,name:'Finance'});
            addBasket({share:'Photos',path:'2026/Birthday',is_dir:1,name:'Birthday'});
            addBasket({share:'homes',path:'alice/Documents',is_dir:1,name:'Documents'});"""); sleep(500)
    b.js("document.querySelectorAll('#toasts > *').forEach(t=>t.remove())")
    b.js("showTab('restore')"); sleep(1500); to_run(); shot("restore")
    b.js("showTab('browse')"); sleep(800); b.js("openBasket(true)"); sleep(1200); b.js("window.scrollTo(0,0)"); sleep(); shot("basket")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chrome", required=True, help="path to a Chrome/Chromium binary")
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "screenshots"))
    ap.add_argument("--python", default=sys.executable, help="interpreter with the app's requirements installed")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="callido-demo-")
    try:
        hbk = os.path.join(tmp, "backup_1.hbk")
        subprocess.check_call([a.python, os.path.join(ROOT, "scripts", "make_demo_backup.py"), hbk])
        home = os.path.join(tmp, "home"); os.makedirs(os.path.join(home, ".hbk-explorer"))
        with open(os.path.join(home, ".hbk-explorer", "sources.json"), "w") as f:
            json.dump([{"id": "demo", "name": "Weekly backup (Google Drive)", "local": hbk, "remotes": None, "extra": []}], f)
        port = free_port(); base = f"http://127.0.0.1:{port}"
        srv = subprocess.Popen([a.python, "-m", "callido_restore", "--no-window", "--port", str(port)], cwd=ROOT,
                               env={**os.environ, "HOME": home, "HBK_NO_AUTOPREPARE": "1"}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(50):
                try: api(base, "/api/sources"); break
                except Exception: time.sleep(0.2)
            info = api(base, "/api/open", src="demo")
            for v in info["versions"][-2:]:
                wait_job(base, api(base, "/api/index", src="demo", share="*", ver=v["id"])["id"])
            with Browser(a.chrome, os.path.join(tmp, "chrome")) as b: capture(b, base, a.out)
        finally:
            try: urllib.request.urlopen(f"{base}/api/quit", timeout=5)
            except Exception: pass
            srv.terminate()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
