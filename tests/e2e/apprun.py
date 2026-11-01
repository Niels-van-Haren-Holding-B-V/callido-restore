"""The real app as a subprocess for end-to-end tests: its own HOME (settings folder, rclone config, mapper child, rclone daemon all
inside a temporary folder), a port from HBK_SMOKE_PORT or a free one (never 8765, the user's running app), stdout captured
(from source the app logs there, what the packaged app writes to app.log)."""
import json, os, socket, subprocess, sys, time, http.client
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]


def free_port():
    with socket.socket() as s: s.bind(("127.0.0.1", 0)); return s.getsockname()[1]


class App:
    def __init__(self, home, log_path, env=None):
        self.home = Path(home); self.log_path = Path(log_path)
        self.port = int(os.environ.get("HBK_SMOKE_PORT") or free_port())
        if self.port == 8765: raise RuntimeError("refusing port 8765: that is the user's running app")
        self.base = f"http://127.0.0.1:{self.port}"
        self.env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home), PYTHONPATH=str(ROOT), HBK_NO_UPDATE_CHECK="1", **(env or {}))
        self.env.pop("HBK_NO_AUTOPREPARE", None)

    def rclone(self, *args):
        """rclone with the app's HOME, so remotes land in the test's own rclone.conf"""
        from callido_restore.paths import RCLONE
        r = subprocess.run([RCLONE, *args], env=self.env, capture_output=True, text=True, timeout=120)
        if r.returncode: raise RuntimeError(f"rclone {' '.join(args)}: {r.stderr.strip()[-300:]}")
        return r.stdout

    def start(self):
        self.log = open(self.log_path, "w")
        self.p = subprocess.Popen([sys.executable, "-m", "callido_restore", "--no-window", "--port", str(self.port)], cwd=str(ROOT), env=self.env,
                                  stdout=self.log, stderr=subprocess.STDOUT)
        for _ in range(200):
            try: self.get("/api/about"); return self
            except OSError: time.sleep(0.1)
        raise RuntimeError("the app did not start: " + self.log_path.read_text()[-2000:])

    def stop(self):
        try: self.get("/api/quit")
        except Exception: pass
        try: self.p.wait(10)
        except subprocess.TimeoutExpired: self.p.kill()
        self.log.close()

    def alive(self): return self.p.poll() is None
    def output(self): self.log.flush(); return self.log_path.read_text(errors="replace")

    def get(self, path, **q):
        with urlopen(f"{self.base}{path}?{urlencode(q)}", timeout=60) as r: return json.loads(r.read() or b"null")

    def raw(self, path, **q):
        with urlopen(f"{self.base}{path}?{urlencode(q)}", timeout=60) as r: return r.read()

    def post(self, path, **body):
        req = Request(f"{self.base}{path}", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=60) as r: return json.loads(r.read())

    def request(self, method, path, headers=None, body=None):
        """(status, json) with exactly the given headers (http.client sends Host itself unless one is given)"""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        hdrs = dict(headers or {}); skip = "Host" in hdrs
        c.putrequest(method, path, skip_host=skip)
        data = json.dumps(body).encode() if body is not None else None
        if data is not None: hdrs.setdefault("Content-Type", "application/json"); hdrs["Content-Length"] = str(len(data))
        for k, v in hdrs.items(): c.putheader(k, v)
        c.endheaders(data)
        r = c.getresponse(); raw = r.read(); c.close()
        try: return r.status, json.loads(raw or b"null")
        except ValueError: return r.status, raw

    def wait_job(self, jid, timeout=300, until=None, every=0.1):
        t0 = time.time(); j = {}
        while time.time() - t0 < timeout:
            j = self.get("/api/job", id=jid)
            if j.get("state") != "running" or (until and until(j)): return j
            time.sleep(every)
        raise TimeoutError(f"job {jid} still running after {timeout}s: {j.get('message')}")
