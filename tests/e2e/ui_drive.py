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
            elif m.get("method") == "Page.javascriptDialogOpening":     # a confirm() blocks the page: answer it (OK) and log it
                self.events.append(("dialog", m["params"]["message"][:400])); self.dialog = m["params"]["message"]
                self.n += 1; msg = {"id": self.n, "method": "Page.handleJavaScriptDialog", "params": {"accept": True}, "sessionId": self.sid}
                os.write(self.w, json.dumps(msg).encode() + b"\0")
            return m
    def text(self, sel): return self.js(f"(document.querySelector({json.dumps(sel)})||{{}}).innerText||''") or ""    # None while a dialog is answered


def post(base, path, **body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r: return json.load(r)


def queue_flow(b, base, work, m, notes, shot):
    """#140: "Start when the backup is prepared" queues the restore; the preparation watcher starts it on any tab; the button
    cancels a queued restore. The preparation is paused before the page opens (as a user pausing it would), so the queue
    window is deterministic; Resume in the header pill lets it finish."""
    gs = "(()=>{const g=document.querySelector('#rsGo');return g?{disabled:g.disabled,text:g.textContent}:null})()"
    toasts = lambda: b.text("#toasts").replace("\n", " | ")
    clear = lambda: b.js("document.querySelectorAll('#toasts > *').forEach(t=>t.remove())")
    restores = lambda: [j for j in shots.api(base, "/api/jobs") if j["label"].startswith("restore")]
    b.navigate(base + "/"); b.wait("typeof S!=='undefined' && S.info && S.info.readable", 60); time.sleep(1.5)
    notes.append(f"queue: /api/pool at start: { {k: shots.api(base, '/api/pool', src='e2e').get(k) for k in ('paused', 'running', 'complete')} }")
    notes.append("queue: pill: " + b.text("#prepPill").replace("\n", " "))
    ver = m["last_clean"]; b.js(f"selectVersion({ver})"); time.sleep(2); b.js("showTab('restore')"); time.sleep(1.5)
    for sh in ("Documents", "Photos", "homes"):
        b.js(f"(()=>{{const c=document.querySelector('#rsTree .tc[data-share=\"{sh}\"][data-path=\"\"]');if(c&&!c.checked){{c.checked=true;c.dispatchEvent(new Event('change'))}}}})()")
        t0 = time.time()
        while time.time() - t0 < 30 and not b.js(f"!!(S.snaps&&S.snaps['{sh}']&&S.snaps['{sh}'].indexed) && [...document.querySelectorAll('#wzMap td.mono')].some(t=>t.textContent.startsWith('{sh}'))"): time.sleep(0.3)
    dest = work / "restore-queue"; dest.mkdir()
    b.js(f"""(()=>{{const ins=[...document.querySelectorAll('#tab-restore input')].filter(i=>i.placeholder==='/Volumes/share/folder');
             const shares=['Documents','Photos','homes'];ins.forEach((i,k)=>{{i.value={json.dumps(str(dest))}+'/'+shares[k];i.dispatchEvent(new Event('input',{{bubbles:true}}))}})}})()""")
    time.sleep(1); clear()
    title = b.js("document.querySelector('#rsGo').title")
    notes.append(f"queue: start button before: {b.js(gs)} · title: {title}")
    # 1. queue, then cancel
    b.js("document.querySelector('#rsGo').click()"); time.sleep(1.5)
    notes.append(f"queue: after Start: toasts [{toasts()}] · button {b.js(gs)} · queued: {b.js('!!S.queued')} · restore jobs: {len(restores())}")
    notes.append("queue: step 4 says: " + b.js("[...document.querySelectorAll('#tab-restore p.help')].map(p=>p.innerText).filter(t=>/Queued/.test(t)).join(' / ')"))
    shot(b, "queued"); clear()
    b.js("document.querySelector('#rsGo').click()"); time.sleep(1.5)
    notes.append(f"queue: after cancel: toasts [{toasts()}] · button {b.js(gs)} · queued: {b.js('!!S.queued')}"); clear()
    # 2. queue again, go to another tab, resume the preparation from the header pill
    b.js("document.querySelector('#rsGo').click()"); time.sleep(1); clear()
    b.js("showTab('changes')"); time.sleep(1)
    notes.append(f"queue: on tab {b.js('S.tab')}, queued {b.js('!!S.queued')}; pill: " + b.text("#prepPill").replace("\n", " "))
    b.js("document.querySelector('#prepBtn').click()"); t0 = time.time(); seen = []
    while time.time() - t0 < 120 and not restores():
        t = toasts()
        if t and t not in seen: seen.append(t)
        time.sleep(0.3)
    notes.append(f"queue: restore job appeared after {time.time() - t0:.1f}s (tab {b.js('S.tab')}); toasts seen: {seen + [toasts()]}")
    t0 = time.time()
    while time.time() - t0 < 120 and any(j["state"] == "running" for j in restores()): time.sleep(0.5)
    jobs = restores(); notes.append(f"queue: restore jobs: {[(j['state'], j.get('done_files'), j.get('total_files')) for j in jobs]}")
    shot(b, "queue-started")
    cmp_ = flow.compare(m, ver, {sh: dest / sh for sh in ("Documents", "Photos", "homes")})
    notes.append(f"queue: on disk vs manifest: ok={len(cmp_['ok'])} missing={len(cmp_['missing'])} wrong_bytes={cmp_['wrong_bytes']} wrong_mtime={cmp_['wrong_mtime']}")


HOSTILE_NOTES = """## v99.0.0

* faster restores <img src=x onerror="window.__pwned='img'">
* <script>window.__pwned='script'</script> nothing to see
* [Update instructions](javascript:window.__pwned='link')
* [Release page](https://evil.example/phish) **bold** and `code`
* &lt;img src=x onerror=&quot;window.__pwned='entity'&quot;&gt;
"""


def update_flow(b, base, notes, shot):
    """#179/#181: an update on offer (seeded update.json, no network) shows a header notice; its dialog renders release notes
    (anyone with release access can edit them) as escaped text: no markup runs, links stay text "label (address)"."""
    b.navigate(base + "/"); b.wait("typeof S!=='undefined' && S.info", 60)
    t0 = time.time()
    while time.time() - t0 < 15 and not b.js("(document.querySelector('#updNote')||{style:{}}).style.display==='inline-flex'"): time.sleep(0.3)
    notes.append("update: header notice: " + b.text("#updNote").replace("\n", " "))
    b.js("document.querySelector('#updMore').click()"); time.sleep(0.8); shot(b, "update-dialog")
    d = b.js("""(()=>{const n=document.querySelector('#updNotes');return {open:document.querySelector('#dlgUpd').open, title:document.querySelector('#updTitle').textContent,
        text:n.innerText, tags:[...n.querySelectorAll('*')].map(e=>e.tagName.toLowerCase()), anchors:n.querySelectorAll('a').length,
        handlers:[...n.querySelectorAll('*')].filter(e=>[...e.attributes].some(a=>/^on/i.test(a.name))).length,
        install:(document.querySelector('#updGo')||{}).textContent, download:(document.querySelector('#updDl')||{}).href}})()""")
    pwned = b.js("window.__pwned||null")
    notes.append(f"update: dialog open={d['open']} title={d['title']!r} install={d['install']!r} download={d['download']!r}")
    notes.append(f"update: notes tags={sorted(set(d['tags']))} anchors={d['anchors']} on*-handlers={d['handlers']} window.__pwned={pwned!r}")
    notes.append("update: notes text: " + d["text"].replace("\n", " | ")[:600])
    ok = d["open"] and not pwned and d["anchors"] == 0 and d["handlers"] == 0 and not set(d["tags"]) - {"p", "b", "ul", "li", "code", "br"} \
        and "Release page (https://evil.example/phish)" in d["text"]
    notes.append(f"update: verdict {'OK' if ok else 'FAILED'}")
    b.js("document.querySelector('#updLater').click()")


def csrf_flow(b, base, port, notes, work):
    """#182/#184 with a real browser: a page on another site (http://localhost:<other port>, a different site than
    http://127.0.0.1:<app port>) tries to stop the app, add a source, and read its data, through fetch, a form, an image and
    an iframe. The app must keep running, nothing may change, and the page must not read anything."""
    import http.server, socketserver, threading
    evil_page = f"""<!doctype html><title>evil</title><body><iframe name=sink></iframe>
<form id=f method=POST action="http://127.0.0.1:{port}/api/sources" enctype="text/plain" target=sink>
<input name='{{"name":"evil","local":"/","x":"' value='"}}'></form>
<img src="http://127.0.0.1:{port}/api/quit">
<script>
window.result = {{}};
fetch("http://127.0.0.1:{port}/api/sources").then(r => r.text()).then(t => result.read = t).catch(e => result.read = "blocked: " + e.message);
fetch("http://127.0.0.1:{port}/api/quit", {{mode: "no-cors"}}).then(() => result.quit = "sent").catch(e => result.quit = "blocked: " + e.message);
fetch("http://127.0.0.1:{port}/api/sources", {{method: "POST", mode: "no-cors", headers: {{"Content-Type": "text/plain"}},
      body: JSON.stringify({{name: "evil2", local: "/"}})}}).then(() => result.post = "sent").catch(e => result.post = "blocked: " + e.message);
document.getElementById("f").submit();
</script></body>"""
    (work / "evil").mkdir(); (work / "evil" / "index.html").write_text(evil_page)
    class Q(http.server.SimpleHTTPRequestHandler):
        def __init__(s, *a, **k): super().__init__(*a, directory=str(work / "evil"), **k)
        def log_message(s, *a): pass
    srv = socketserver.TCPServer(("127.0.0.1", 0), Q); eport = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    before = shots.api(base, "/api/sources")
    try:
        b.navigate(f"http://localhost:{eport}/"); time.sleep(3)
        res = b.js("window.result")
    finally:
        srv.shutdown(); srv.server_close()
    try: after = shots.api(base, "/api/sources"); alive = True
    except Exception: after, alive = None, False
    notes.append(f"csrf: page results {res}")
    notes.append(f"csrf: app alive {alive}; sources before {[s['id'] for s in before]} after {[s['id'] for s in after] if after else None}")
    ok = alive and after == before and "e2e" not in str((res or {}).get("read", ""))
    notes.append(f"csrf: verdict {'OK' if ok else 'FAILED'}")
    b.navigate(base + "/"); b.wait("typeof S!=='undefined' && S.info && S.info.readable", 60)       # the app's own page still works
    notes.append(f"csrf: own page afterwards readable={b.js('!!(S.info&&S.info.readable)')}")


def stop_flow(b, base, work, m, notes, shot):
    """#187: Stop on the running restore's bar, then Resume on the stopped one; what is on disk after Stop is byte-exact (no
    half files), the stopped restore is offered under "Restores you can resume", and after Resume the whole run is byte-exact."""
    restores = lambda: [j for j in shots.api(base, "/api/jobs") if j["label"].startswith("restore")]
    b.navigate(base + "/"); b.wait("typeof S!=='undefined' && S.info && S.info.readable", 60); time.sleep(1)
    ver = m["last_clean"]; b.js(f"selectVersion({ver})"); time.sleep(2); b.js("showTab('restore')"); time.sleep(1.5)
    for sh in ("Documents", "Photos", "homes"):
        b.js(f"(()=>{{const c=document.querySelector('#rsTree .tc[data-share=\"{sh}\"][data-path=\"\"]');if(c&&!c.checked){{c.checked=true;c.dispatchEvent(new Event('change'))}}}})()")
        t0 = time.time()
        while time.time() - t0 < 30 and not b.js(f"!!(S.snaps&&S.snaps['{sh}']&&S.snaps['{sh}'].indexed) && [...document.querySelectorAll('#wzMap td.mono')].some(t=>t.textContent.startsWith('{sh}'))"): time.sleep(0.3)
    dest = work / "restore-stop"; dest.mkdir()
    b.js(f"""(()=>{{const ins=[...document.querySelectorAll('#tab-restore input')].filter(i=>i.placeholder==='/Volumes/share/folder');
             const shares=['Documents','Photos','homes'];ins.forEach((i,k)=>{{i.value={json.dumps(str(dest))}+'/'+shares[k];i.dispatchEvent(new Event('input',{{bubbles:true}}))}})}})()""")
    time.sleep(1); b.js("document.querySelector('#rsGo').click()")
    t0 = time.time()
    while time.time() - t0 < 60 and not any((j.get("done_files") or 0) >= 4 for j in restores()): time.sleep(0.1)
    stop = b.js("(()=>{const x=[...document.querySelectorAll('#rsBar button, #rsProg button')].find(x=>x.textContent.trim()==='Stop');if(x){x.click();return true}return false})()")
    notes.append(f"stop: Stop button clicked: {stop}"); t0 = time.time()
    while time.time() - t0 < 60 and any(j["state"] == "running" for j in restores()): time.sleep(0.2)
    time.sleep(1.5); shot(b, "stopped")
    jid = b.js("(()=>{const r=document.querySelector('#rsResume');return r?true:false})()")
    saved = [j for j in shots.api(base, "/api/jobs/saved") if j.get("label", "").startswith("restore")]
    notes.append(f"stop: after Stop: bar says {b.text('#rsProg')[:250]!r}; Resume button on the bar: {jid}")
    notes.append(f"stop: saved restores: {[(j['state'], j.get('done_files'), j.get('total_files')) for j in saved]}")
    notes.append("stop: 'Restores you can resume': " + b.text("#rsSaved").replace("\n", " | ")[:300])
    part = flow.compare(m, ver, {sh: dest / sh for sh in ("Documents", "Photos", "homes")})
    leftovers = [p.as_posix() for p in dest.rglob("*") if p.name.endswith(".part") or ".hbk-restore-" in p.as_posix()]
    notes.append(f"stop: on disk after Stop: ok={len(part['ok'])} missing={len(part['missing'])} wrong_bytes={part['wrong_bytes']} wrong_mtime={part['wrong_mtime']} leftovers={leftovers}")
    b.js("document.querySelector('#rsResume') && document.querySelector('#rsResume').click()"); time.sleep(1); t0 = time.time()
    while time.time() - t0 < 120 and (not restores() or any(j["state"] == "running" for j in restores())): time.sleep(0.2)
    time.sleep(2); shot(b, "resumed")
    notes.append(f"stop: after Resume: {b.text('#rsProg')[:300]!r}")
    full = flow.compare(m, ver, {sh: dest / sh for sh in ("Documents", "Photos", "homes")})
    notes.append(f"stop: on disk after Resume: ok={len(full['ok'])} missing={len(full['missing'])} wrong_bytes={full['wrong_bytes']} wrong_mtime={full['wrong_mtime']} extra={full['extra']}")
    total = sum(len(v) for v in m["versions"][str(ver)].values())
    ok = stop and jid and 0 < len(part["ok"]) < total and not part["wrong_bytes"] and not leftovers and len(full["ok"]) == total and not full["wrong_bytes"] and not full["extra"]
    notes.append(f"stop: verdict {'OK' if ok else 'FAILED'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chrome", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--stop", action="store_true", help="#187: Stop the running restore, Resume it, byte-exact")
    ap.add_argument("--update", action="store_true", help="an update on offer with hostile release notes: notice and dialog (no network)")
    ap.add_argument("--csrf", action="store_true", help="a page on another site attacks the running app through the browser")
    ap.add_argument("--chunk-index", action="store_true", help="the backup carries its own chunk table (Pool/chunk_index)")
    ap.add_argument("--queue", action="store_true", help="#140: queue a restore while the preparation is paused, cancel it, queue again, resume")
    ap.add_argument("--fast", action="store_true", help="impatient user: tick, type the targets and press Start while the file lists still load (U5)")
    ap.add_argument("--scale", type=float, default=6); ap.add_argument("--bucket-kb", type=int, default=1024)
    a = ap.parse_args(); out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="hbk-ui-")); home = work / "home"; (home / ".hbk-explorer").mkdir(parents=True)
    hbk = work / "backups" / "e2e_1.hbk"; m = hbkgen.build(str(hbk), a.scale, a.bucket_kb * 1024, chunk_index=a.chunk_index)
    (home / ".hbk-explorer" / "sources.json").write_text(json.dumps([{"id": "e2e", "name": "E2E backup", "local": str(hbk), "remotes": [], "extra": []}]))
    if a.update:
        (home / ".hbk-explorer" / "update.json").write_text(json.dumps({"checked": time.time(), "release": {"tag_name": "v99.0.0", "name": "v99.0.0",
            "body": HOSTILE_NOTES, "html_url": "https://github.com/Niels-van-Haren-Holding-B-V/callido-restore/releases/tag/v99.0.0", "assets": []}}))
    port = int(os.environ.get("HBK_SMOKE_PORT") or shots.free_port()); assert port != 8765
    base = f"http://127.0.0.1:{port}"; log = open(work / "app.log", "w")
    env = dict(os.environ, HOME=str(home), PYTHONPATH=str(ROOT), HBK_NO_UPDATE_CHECK="1"); env.pop("HBK_NO_AUTOPREPARE", None)   # never asks GitHub
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
            if a.stop:
                stop_flow(b, base, work, m, notes, lambda b_, name: shot(b_, name)); notes.extend(f"[{k}] {v}" for k, v in b.events)
                return
            if a.update or a.csrf:
                if a.update: update_flow(b, base, notes, lambda b_, name: shot(b_, name))
                if a.csrf: csrf_flow(b, base, port, notes, work)
                notes.extend(f"[{k}] {v}" for k, v in b.events)
                return
            if a.queue:
                post(base, "/api/pool/pause", src="e2e")
                queue_flow(b, base, work, m, notes, lambda b_, name: shot(b_, name)); notes.extend(f"[{k}] {v}" for k, v in b.events)
                return

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
                notes.append(f"tree row before ticking {sh}: " + b.js(f"(()=>{{const c=document.querySelector('#rsTree .tc[data-share=\"{sh}\"][data-path=\"\"]');return c?c.parentElement.innerText.replace(/\\n/g,' '):'(no checkbox)'}})()"))
                b.js(f"(()=>{{const c=document.querySelector('#rsTree .tc[data-share=\"{sh}\"][data-path=\"\"]');if(c&&!c.checked){{c.checked=true;c.dispatchEvent(new Event('change'))}}}})()")
                if a.fast: continue
                t0 = time.time()                               # a person waits until the share shows as loaded and its target row is there
                while time.time() - t0 < 30 and not b.js(f"!!(S.snaps&&S.snaps['{sh}']&&S.snaps['{sh}'].indexed) && [...document.querySelectorAll('#wzMap td.mono')].some(t=>t.textContent.startsWith('{sh}'))"): time.sleep(0.3)
                notes.append(f"  {sh}: loaded and its row shown after {time.time() - t0:.1f}s")
            if not a.fast: time.sleep(1)
            notes.append("selection summary: " + b.text("#rsSelSum"))
            dest = work / "restore-ui"; dest.mkdir()           # the "mounted share": its subfolders are new
            # step 3: one target per shared folder (the rows' path fields), each a new folder
            if a.fast:                                         # type as soon as the rows are on the page, lists or not
                t0 = time.time()
                while time.time() - t0 < 10 and b.js("[...document.querySelectorAll('#tab-restore input')].filter(i=>i.placeholder==='/Volumes/share/folder').length") < 3: time.sleep(0.05)
                notes.append(f"fast: rows on the page after {time.time() - t0:.2f}s; lists loaded: {b.js('Object.fromEntries(Object.entries(S.snaps||{}).map(([k,v])=>[k,!!v.indexed]))')}")
            ok = b.js(f"""(()=>{{const ins=[...document.querySelectorAll('#tab-restore input')].filter(i=>i.placeholder==='/Volumes/share/folder');
                         const shares=['Documents','Photos','homes'];ins.forEach((i,k)=>{{i.value={json.dumps(str(dest))}+'/'+shares[k];
                         i.dispatchEvent(new Event('input',{{bubbles:true}}));i.dispatchEvent(new Event('change',{{bubbles:true}}))}});return ins.length}})()""")
            notes.append(f"target rows filled: {ok}")
            if a.fast: notes.append("fast: wzMap right after typing: " + str(b.js("localStorage.getItem('wzMap')")) + " · rows on page: " + str(b.js("[...document.querySelectorAll('#wzMap td.mono')].map(t=>t.textContent.slice(0,12))")))
            if a.fast:                                         # press Start at once, while the lists are loading
                gs = "(()=>{const g=document.querySelector('#rsGo');return g?{disabled:g.disabled,text:g.textContent}:null})()"
                notes.append(f"fast: loading now: {b.js('Object.fromEntries(Object.entries(S.snaps||{}).map(([k,v])=>[k,!!v.indexed]))')} · start button: {b.js(gs)}")
                b.js("document.querySelector('#rsGo').click()"); time.sleep(0.4)
                notes.append("fast: toasts right after Start: " + b.text("#toasts").replace("\n", " | ") + " · progress: " + b.text("#rsProg")[:200])
                n_exc = len([e for e in b.events if e[0] == "exception"]); notes.append(f"fast: page exceptions so far: {n_exc}")
                t0 = time.time()
                while time.time() - t0 < 30 and not b.js("['Documents','Photos','homes'].every(k=>S.snaps&&S.snaps[k]&&S.snaps[k].indexed)"): time.sleep(0.3)
                time.sleep(1.5)
                vals = b.js("[...document.querySelectorAll('#tab-restore input')].filter(i=>i.placeholder==='/Volumes/share/folder').map(i=>i.value)")
                notes.append(f"fast: lists loaded after {time.time() - t0:.1f}s; target fields now: {vals}")
                notes.append("fast: wzMap after the loads: " + str(b.js("localStorage.getItem('wzMap')")))
                b.js("document.querySelectorAll('#toasts > *').forEach(t=>t.remove())")
            time.sleep(1.5); shot(b, "target-set")
            notes.append("check/start area: " + b.text("#tab-restore")[-900:].replace("\n", " | "))
            gostate = "(()=>{const g=document.querySelector('#rsGo');return g?{disabled:g.disabled,text:g.textContent}:null})()"
            go = b.js(gostate); notes.append(f"start button: {go}")
            if go and go["disabled"]:                          # selected shares still unloaded: the one-click loader (#123)
                hit = b.js("(()=>{const x=[...document.querySelectorAll('#tab-restore button')].find(x=>/Load (it|them) now/.test(x.textContent));if(x){x.click();return x.textContent}return null})()")
                notes.append(f"one-click loader: {hit}"); t0 = time.time()
                while go and go["disabled"] and time.time() - t0 < 60: time.sleep(0.5); go = b.js(gostate)
                notes.append(f"start button after the loader ({time.time() - t0:.0f}s): {go}")
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
