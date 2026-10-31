"""Drive the real UI (static/index.html) in headless Chrome against the app server on a synthetic backup, the way a user
would: pick the backup, wait for the preparation, choose the last clean run, put a share in the basket, set a target, start
the restore, read what the page says. Screenshots and page/console errors land in --out.

    python -m tests.e2e.ui_drive --chrome "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --out /tmp/ui

HOME is a temporary folder (the user's ~/.hbk-explorer is never touched); the port is HBK_SMOKE_PORT or a free one, never 8765."""
import argparse, importlib.util, json, os, shutil, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("screenshots", ROOT / "scripts" / "screenshots.py"); shots = importlib.util.module_from_spec(spec); spec.loader.exec_module(shots)
from tests.e2e import flow, hbkgen


class Browser(shots.Browser):
    """The screenshot script's CDP client, keeping console messages and uncaught exceptions instead of dropping events."""
    events = []
    def _read(self):
        while True:
            m = super()._read()
            if m.get("method") == "Runtime.consoleAPICalled" and m["params"]["type"] in ("error", "warning", "assert"):
                self.events.append(("console." + m["params"]["type"], " ".join(str(a.get("value", a.get("description", ""))) for a in m["params"]["args"])[:400]))
            elif m.get("method") == "Runtime.exceptionThrown":
                d = m["params"]["exceptionDetails"]; self.events.append(("exception", (d.get("exception", {}).get("description") or d.get("text", ""))[:400]))
            elif m.get("method") == "Page.javascriptDialogOpening":
                self.events.append(("dialog", m["params"]["message"][:400])); self.dialog = m["params"]["message"]
            return m
    def text(self, sel): return self.js(f"(document.querySelector({json.dumps(sel)})||{{}}).innerText||''")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chrome", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--scale", type=float, default=6); ap.add_argument("--bucket-kb", type=int, default=1024)
    a = ap.parse_args(); out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="hbk-ui-")); home = work / "home"; (home / ".hbk-explorer").mkdir(parents=True)
    hbk = work / "backups" / "e2e_1.hbk"; m = hbkgen.build(str(hbk), a.scale, a.bucket_kb * 1024)
    (home / ".hbk-explorer" / "sources.json").write_text(json.dumps([{"id": "e2e", "name": "E2E backup", "local": str(hbk), "remotes": [], "extra": []}]))
    port = int(os.environ.get("HBK_SMOKE_PORT") or shots.free_port()); assert port != 8765
    base = f"http://127.0.0.1:{port}"; log = open(work / "app.log", "w")
    env = dict(os.environ, HOME=str(home), PYTHONPATH=str(ROOT)); env.pop("HBK_NO_AUTOPREPARE", None)
    srv = subprocess.Popen([sys.executable, "-m", "callido_restore", "--no-window", "--port", str(port)], cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
    notes = []; n = [0]
    def shot(b, name):
        n[0] += 1; p = out / f"{n[0]:02d}-{name}.png"; p.write_bytes(b.png()); notes.append(f"[shot] {p.name}")
    try:
        for _ in range(100):
            try: shots.api(base, "/api/sources"); break
            except Exception: time.sleep(0.1)
        with Browser(a.chrome, str(work / "chrome")) as b:
            b.call("Page.enable")
            b.navigate(base + "/"); time.sleep(1.5); shot(b, "start")
            notes.append("header: " + b.text("header")[:300].replace("\n", " | "))
            b.wait("typeof S!=='undefined' && S.info && S.info.readable", 60); time.sleep(1); shot(b, "opened")
            notes.append(f"selected run after open: {b.js('S.ver')} (versions {[v['id'] for v in shots.api(base, '/api/open', src='e2e')['versions']]})")
            # the preparation runs by itself: watch the pill until it is gone
            t0 = time.time(); seen = set()
            while time.time() - t0 < 120:
                t = b.js("(document.querySelector('#prepPill')||{}).style?.display!=='none' ? (document.querySelector('#prepText')||{}).textContent : ''")
                if t: seen.add(t[:120])
                st = shots.api(base, "/api/pool", src="e2e")
                if st.get("complete") and not st.get("running"): break
                time.sleep(0.3)
            notes.append(f"prep pill texts seen: {sorted(seen)[:6]}"); notes.append(f"/api/pool after prep: { {k: st.get(k) for k in ('complete', 'mapped_buckets', 'total_buckets', 'bytes_pct', 'records_placed', 'records_total')} }")
            time.sleep(2.5); shot(b, "prepared")
            ver = m["last_clean"]
            b.js(f"selectVersion({ver})"); time.sleep(2)
            b.wait("Object.keys(S.snaps||{}).length>0", 30); time.sleep(1.5)
            b.js("showTab('restore')"); time.sleep(2); shot(b, "restore-tab")
            notes.append("restore tab text: " + b.text("#tab-restore")[:1500].replace("\n", " | "))
            # whole Documents share into the basket, target = a new folder
            # step 2 as a user does it: tick the three shares in the tree (one at a time: each tick re-renders the tab)
            for sh in ("Documents", "Photos", "homes"):
                b.js(f"(()=>{{const c=document.querySelector('#rsTree .tc[data-share=\"{sh}\"][data-path=\"\"]');if(c&&!c.checked){{c.checked=true;c.dispatchEvent(new Event('change'))}}}})()"); time.sleep(1.2)
            time.sleep(1); notes.append("selection summary: " + b.text("#rsSelSum"))
            dest = work / "restore-ui"; dest.mkdir()           # the "mounted share": its subfolders are new
            # step 3: one target per shared folder (the rows' path fields), each a new folder
            ok = b.js(f"""(()=>{{const ins=[...document.querySelectorAll('#tab-restore input')].filter(i=>i.placeholder==='/Volumes/share/folder');
                         const shares=['Documents','Photos','homes'];ins.forEach((i,k)=>{{i.value={json.dumps(str(dest))}+'/'+shares[k];
                         i.dispatchEvent(new Event('input',{{bubbles:true}}));i.dispatchEvent(new Event('change',{{bubbles:true}}))}});return ins.length}})()""")
            notes.append(f"target rows filled: {ok}"); time.sleep(1.5); shot(b, "target-set")
            notes.append("check/start area: " + b.text("#tab-restore")[-900:].replace("\n", " | "))
            go = b.js("(()=>{const g=document.querySelector('#rsGo');return g?{disabled:g.disabled,text:g.textContent}:null})()"); notes.append(f"start button: {go}")
            if go and not go["disabled"]:
                b.js("document.querySelector('#rsGo').click()"); t0 = time.time(); time.sleep(1.5)
                notes.append("toasts after Start: " + b.text("#toasts").replace("\n", " | "))
                notes.append("target rows after the check: " + b.text("#wzMap").replace("\n", " | ") + " || " + b.text("#wzCheckOut"))
                b.js("document.querySelector('#wzMap').scrollIntoView()"); time.sleep(0.5); shot(b, "targets-checked")
                if not b.text("#rsProg").strip():
                    notes.append("Start was refused; loading the file lists of the run ('Load all shares for this run') and trying again")
                    b.js("[...document.querySelectorAll('button')].find(x=>/Load all shares/.test(x.textContent)).click()")
                    b.wait("Object.values(S.snaps||{}).every(i=>i.indexed)", 60); time.sleep(2)
                    b.js("showTab('restore')"); time.sleep(2)
                    notes.append("selection summary after loading: " + b.text("#rsSelSum"))
                    b.js("document.querySelector('#rsGo').click()"); time.sleep(1.5)
                    notes.append("toasts after second Start: " + b.text("#toasts").replace("\n", " | "))
                while time.time() - t0 < 120:
                    txt = b.text("#rsProg")
                    if "Done:" in txt or "Paused:" in txt or "rror" in txt: break
                    time.sleep(0.5)
                time.sleep(1); shot(b, "restore-done")
                notes.append("restore result: " + b.text("#rsProg")[:800].replace("\n", " | "))
                notes.append("unverified box: " + b.text("#rsUnverified")[:500].replace("\n", " | ") + " || errors box: " + b.text("#rsErrors")[:800].replace("\n", " | "))
                cmp_ = flow.compare(m, ver, {sh: dest / sh for sh in ("Documents", "Photos", "homes")})
                notes.append(f"on disk vs manifest: ok={len(cmp_['ok'])} missing={len(cmp_['missing'])} wrong_bytes={cmp_['wrong_bytes']} wrong_mtime={cmp_['wrong_mtime']}")
            for tab in ("changes", "timeline", "browse", "search", "compare"):
                b.js(f"showTab('{tab}')"); time.sleep(1.5); shot(b, "tab-" + tab)
            notes.extend(f"[{k}] {v}" for k, v in b.events)
    finally:
        try: urllib.request.urlopen(f"{base}/api/quit", timeout=5)
        except Exception: pass
        try: srv.wait(10)
        except subprocess.TimeoutExpired: srv.kill()
        log.close(); shutil.copy(work / "app.log", out / "app.log"); shutil.rmtree(work, ignore_errors=True)
    (out / "notes.txt").write_text("\n".join(notes)); print("\n".join(notes))


if __name__ == "__main__":
    main()
