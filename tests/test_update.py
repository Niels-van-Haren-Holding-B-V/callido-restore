"""Updates: version order, the check, and every refusal of the install path (signature, checksum, downgrade, platform,
network). No network: GitHub's answers are faked; signatures come from the RFC 8032 key whose secret key is public."""
import hashlib, io, json, os, shutil, tempfile, unittest, urllib.error
from unittest import mock

from callido_restore import paths, update

# RFC 8032 TEST 1 secret key; only for signing test data here.
SK = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
PK = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"


def _sign(msg):
    """Ed25519 signing with the RFC 8032 algorithm, for test data only (the app never signs)."""
    from callido_restore import ed25519 as e
    h = hashlib.sha512(SK).digest(); a = int.from_bytes(h[:32], "little"); a &= (1 << 254) - 8; a |= 1 << 254
    def comp(pt):
        zi = e._inv(pt[2]); x = pt[0] * zi % e.P; y = pt[1] * zi % e.P
        return int.to_bytes(y | ((x & 1) << 255), 32, "little")
    A = comp(e._mul(a, e.G)); r = int.from_bytes(hashlib.sha512(h[32:] + msg).digest(), "little") % e.L
    R = comp(e._mul(r, e.G)); k = int.from_bytes(hashlib.sha512(R + A + msg).digest(), "little") % e.L
    return R + int.to_bytes((r + k * a) % e.L, 32, "little")


class Resp(io.BytesIO):
    def __init__(self, data, url): super().__init__(data); self._url = url; self.headers = {"Content-Length": str(len(data))}
    def geturl(self): return self._url
    def __enter__(self): return self
    def __exit__(self, *a): self.close()


class Fixture(unittest.TestCase):
    """Fake releases and a temp settings folder; no tests of its own."""
    def setUp(self):
        self.d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, self.d, True)
        for k, v in (("CONF_DIR", self.d), ("CACHE_DIR", os.path.join(self.d, "cache"))):
            p = mock.patch.object(paths, k, __import__("pathlib").Path(v)); p.start(); self.addCleanup(p.stop)
        for k, v in (("VERSION", "1.1.0"), ("UPDATE_PUBLIC_KEYS", [PK])):
            p = mock.patch.object(update, k, v); p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(update, "platform_asset", return_value="CallidoRestore-arm64.dmg"); p.start(); self.addCleanup(p.stop)
        p = mock.patch.object(paths, "WINDOWS", False); p.start(); self.addCleanup(p.stop)        # the macOS path, on every OS
        self.payload = b"the new disk image" * 1000
        self.files = {}
        self.set_release("v1.2.0", self.payload)

    def set_release(self, tag, payload, sums=None, sig=None):
        base = f"https://github.com/{update.REPO}/releases/download/{tag}/"
        sums = sums if sums is not None else f"{hashlib.sha256(payload).hexdigest()}  CallidoRestore-arm64.dmg\n".encode()
        sig = sig if sig is not None else _sign(update.signed_message(tag, sums))
        self.files = {base + "CallidoRestore-arm64.dmg": payload, base + "SHA256SUMS": sums, base + "SHA256SUMS.sig": sig}
        self.rel = {"tag_name": tag, "name": tag, "body": "notes", "html_url": base,
                    "assets": [{"name": u.rsplit("/", 1)[1], "browser_download_url": u, "size": len(b)} for u, b in self.files.items()]}

    def fake_open(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else req
        if url not in self.files: raise urllib.error.URLError("not found")
        return Resp(self.files[url], "https://objects.githubusercontent.com/x")

    def download(self, **kw):
        with mock.patch.object(update.urllib.request, "urlopen", side_effect=self.fake_open):
            return update.download_verified(self.rel, "CallidoRestore-arm64.dmg", **kw)

    def assertNothingKept(self):
        folder = os.path.join(self.d, "cache", "updates")
        left = [f for _, _, fs in os.walk(folder) for f in fs] if os.path.isdir(folder) else []
        self.assertEqual(left, [])


class UpdateTests(Fixture):
    # ------------------------------------------------------------------ the good path
    def test_signed_and_matching_is_kept(self):
        p = self.download()
        self.assertEqual(open(p, "rb").read(), self.payload); self.assertIn(os.path.join("updates", "1.2.0"), str(p))

    # ------------------------------------------------------------------ refusals
    def test_bad_signature(self):
        sums = f"{hashlib.sha256(self.payload).hexdigest()}  CallidoRestore-arm64.dmg\n".encode()
        self.set_release("v1.2.0", self.payload, sums=sums, sig=_sign(update.signed_message("v1.2.0", sums.replace(b"  ", b" "))))
        with self.assertRaises(RuntimeError) as cm: self.download()
        self.assertIn("signature", str(cm.exception)); self.assertNothingKept()

    def test_list_replaced_without_the_key(self):
        """Whoever can change the release can change SHA256SUMS too, but cannot sign it: a re-made list with a valid-looking
        signature of another key is refused."""
        evil = b"evil installer"
        sums = f"{hashlib.sha256(evil).hexdigest()}  CallidoRestore-arm64.dmg\n".encode()
        self.set_release("v1.2.0", evil, sums=sums, sig=bytes(64))
        with self.assertRaises(RuntimeError): self.download()
        self.assertNothingKept()

    def test_old_signed_release_republished_under_a_newer_tag(self):
        """Release write access without the key: the old v1.0.0 list and its valid signature, the old image, a new tag."""
        sums = f"{hashlib.sha256(self.payload).hexdigest()}  CallidoRestore-arm64.dmg\n".encode()
        self.set_release("v9.9.9", self.payload, sums=sums, sig=_sign(update.signed_message("v1.0.0", sums)))
        with self.assertRaises(RuntimeError) as cm: self.download()
        self.assertIn("signature", str(cm.exception)); self.assertNothingKept()

    def test_a_signature_without_the_tag_is_refused(self):
        sums = f"{hashlib.sha256(self.payload).hexdigest()}  CallidoRestore-arm64.dmg\n".encode()
        self.set_release("v1.2.0", self.payload, sums=sums, sig=_sign(sums))                  # signed the list alone
        with self.assertRaises(RuntimeError): self.download()

    def test_windows_is_not_installable_without_the_windows_step(self):
        with mock.patch.object(paths, "WINDOWS", True), mock.patch.object(update, "latest_release", return_value=self.rel):
            update._check_now()
            self.assertTrue(update.status()["available"]); self.assertFalse(update.status()["installable"])

    def test_tampered_installer(self):
        self.set_release("v1.2.0", self.payload)
        self.files[[u for u in self.files if u.endswith(".dmg")][0]] = self.payload + b"x"
        with self.assertRaises(RuntimeError) as cm: self.download()
        self.assertIn("does not match", str(cm.exception)); self.assertNothingKept()

    def test_older_or_same_tag_is_refused(self):
        for tag in ("v1.1.0", "v1.0.9", "v0.9.0", "v1.1.0-rc1"):
            self.set_release(tag, self.payload)
            with self.assertRaises(RuntimeError) as cm: self.download()
            self.assertIn("not newer", str(cm.exception))
        self.assertNothingKept()

    def test_wrong_asset_name(self):
        with mock.patch.object(update.urllib.request, "urlopen", side_effect=self.fake_open):
            with self.assertRaises(RuntimeError) as cm: update.download_verified(self.rel, "CallidoRestore-x64.msi")
        self.assertIn("not this computer", str(cm.exception))

    def test_network_failure(self):
        with mock.patch.object(update.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")):
            with self.assertRaises(urllib.error.URLError): update.download_verified(self.rel, "CallidoRestore-arm64.dmg")
        self.assertNothingKept()

    def test_no_key_in_the_app(self):
        with mock.patch.object(update, "UPDATE_PUBLIC_KEYS", []), self.assertRaises(RuntimeError) as cm: self.download()
        self.assertIn("no release key", str(cm.exception))

    def test_only_github_https(self):
        with self.assertRaises(RuntimeError): update._check_url("http://github.com/x", update.ASSET_HOSTS)
        with self.assertRaises(RuntimeError): update._check_url("https://example.com/x", update.ASSET_HOSTS)
        rel = dict(self.rel, assets=[{"name": "SHA256SUMS", "browser_download_url": "https://github.com/someone-else/repo/releases/download/v9/SHA256SUMS"}])
        with self.assertRaises(RuntimeError): update._asset(rel, "SHA256SUMS")
        redirect = lambda req, timeout=None: Resp(b"x", "https://evil.example/x")
        with mock.patch.object(update.urllib.request, "urlopen", side_effect=redirect), self.assertRaises(RuntimeError) as cm:
            update.download_verified(self.rel, "CallidoRestore-arm64.dmg")
        self.assertIn("not a GitHub release host", str(cm.exception))
        self.assertNothingKept()

    # ------------------------------------------------------------------ versions, check, swap helper
    def test_version_order(self):
        self.assertTrue(update.newer("v1.1.1", "1.1.0")); self.assertTrue(update.newer("v1.10.0", "1.9.9"))
        self.assertFalse(update.newer("v1.1.0", "1.1.0")); self.assertFalse(update.newer("v1.1.0-rc1", "1.1.0"))
        self.assertTrue(update.newer("v1.2.0-rc1", "1.1.0")); self.assertFalse(update.newer("garbage", "1.1.0"))

    def test_status_never_waits_and_check_can_be_turned_off(self):
        with mock.patch.object(update, "latest_release", return_value=self.rel):
            st = update.status(force=True)
            self.assertEqual(st["current"], "1.1.0")
            for _ in range(100):
                if not update._checking[0]: break
                __import__("time").sleep(0.01)
            st = update.status()
        self.assertTrue(st["available"]); self.assertEqual(st["latest"], "1.2.0"); self.assertTrue(st["installable"])
        self.assertFalse(update.set_check(False)["check"]); self.assertFalse(update.check_enabled())
        with mock.patch.object(update, "latest_release", side_effect=urllib.error.URLError("offline")):
            update._check_now()                                  # silent: no exception, the error is only recorded
        self.assertIn("offline", json.loads(open(os.path.join(self.d, "update.json")).read())["error"])

    def test_checksum_list_parsing(self):
        h = "a" * 64
        self.assertEqual(update.expected_sha(f"{h}  CallidoRestore-arm64.dmg\n{'b'*64}  CallidoRestore-amd64.dmg", "CallidoRestore-arm64.dmg"), h)
        self.assertIsNone(update.expected_sha(f"{h}  other.dmg", "CallidoRestore-arm64.dmg"))
        self.assertEqual(update.expected_sha(f"{h}\n", "CallidoRestore-x64.msi"), h)

    def test_swap_script_restores_the_old_app_on_failure(self):
        s = update.mac_swap_script("/Applications/Callido Restore.app", "/Applications/.Callido Restore.app.new", 4242)
        self.assertIn("kill -0 4242", s); self.assertTrue(s.rstrip().endswith("open '/Applications/Callido Restore.app'"))

    @unittest.skipUnless(os.name == "posix" and shutil.which("sh"), "needs a POSIX shell")
    def test_swap_script_on_real_folders(self):
        """The helper on real folders: a clean swap; a leftover .old that cannot be removed leaves the old app untouched."""
        import subprocess, stat
        def run(script): subprocess.run(["/bin/sh", "-c", script.replace("open ", "true ")], check=True)
        base = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, base, True)
        app, new = os.path.join(base, "X.app"), os.path.join(base, ".X.app.new")
        os.makedirs(os.path.join(app, "v1")); os.makedirs(os.path.join(new, "v2"))
        run(update.mac_swap_script(app, new, 999999))
        self.assertEqual(os.listdir(app), ["v2"]); self.assertFalse(os.path.exists(new)); self.assertFalse(os.path.exists(app + ".old"))
        os.makedirs(os.path.join(new, "v3")); os.makedirs(os.path.join(app + ".old", "locked", "f"))
        os.chmod(os.path.join(app + ".old", "locked"), stat.S_IRUSR | stat.S_IXUSR)            # rm -rf of .old fails
        self.addCleanup(os.chmod, os.path.join(app + ".old", "locked"), stat.S_IRWXU)
        run(update.mac_swap_script(app, new, 999999))
        self.assertEqual(os.listdir(app), ["v2"]); self.assertNotIn("X.app", os.listdir(app + ".old"))   # nothing moved into .old


class Modes(Fixture):
    """The setting auto / mention / off, and the automatic install: same checks, waits for jobs, no retry after a failure."""
    def setUp(self):
        super().setUp()
        p = mock.patch.object(update, "_checking", [True]); p.start(); self.addCleanup(p.stop)   # no background check here
    def known(self):
        d = update._read(); d.update(checked=__import__("time").time(), release=self.rel); update._write(d)

    def test_setting(self):
        self.assertEqual(update.mode(), "mention")                                  # the default
        open(os.path.join(self.d, "update.json"), "w").write(json.dumps({"check": False}))
        self.assertEqual(update.mode(), "off")                                      # a state file from before the setting
        self.assertEqual(update.set_mode("auto")["mode"], "auto"); self.assertTrue(update.check_enabled())
        self.assertEqual(update.set_check(True)["mode"], "auto")                    # the old switch keeps auto
        self.assertEqual(update.set_check(False)["mode"], "off"); self.assertFalse(update.check_enabled())
        with self.assertRaises(ValueError): update.set_mode("always")
        with mock.patch.dict(os.environ, {"HBK_NO_UPDATE_CHECK": "1"}): update.set_mode("auto"); self.assertEqual(update.mode(), "off")

    def test_auto_due_only_with_auto_and_an_installable_release(self):
        self.known()
        self.assertFalse(update.auto_due())                                         # mention: only shown
        update.set_mode("auto")
        self.assertTrue(update.auto_due())
        with mock.patch.object(update, "UPDATE_PUBLIC_KEYS", []): self.assertFalse(update.auto_due())   # unsigned: never automatic
        self.set_release("v1.1.0", self.payload); self.known(); self.assertFalse(update.auto_due())     # not newer

    def run_install(self, **kw):
        job = {}
        with mock.patch.object(update.urllib.request, "urlopen", side_effect=self.fake_open), \
             mock.patch.object(update, "install_mac", return_value={"restarting": "x"}) as inst:
            out = update.install(job, lambda c: None, **kw)
        return out, inst

    def test_auto_install_uses_the_same_checks_and_never_opens_the_image_unasked(self):
        update.set_mode("auto"); self.known()
        out, inst = self.run_install(auto=True, may_install=lambda: True)
        self.assertEqual(out, {"restarting": "x"}); self.assertFalse(inst.call_args.kwargs["open_fallback"])

    def test_auto_install_waits_when_a_job_started_meanwhile(self):
        update.set_mode("auto"); self.known()
        with self.assertRaises(update.Postponed): self.run_install(auto=True, may_install=lambda: False)
        self.assertTrue(update.auto_due())                                          # postponed, not failed: tried again later
        self.assertTrue(os.path.isfile(os.path.join(self.d, "cache", "updates", "1.2.0", "CallidoRestore-arm64.dmg")))  # kept

    def test_failed_auto_install_is_not_retried(self):
        update.set_mode("auto"); self.set_release("v1.2.0", self.payload, sig=b"\0" * 64); self.known()
        with self.assertRaises(RuntimeError): self.run_install(auto=True, may_install=lambda: True)
        self.assertFalse(update.auto_due()); self.assertTrue(update.status()["installable"])   # still offered for a click
        self.assertNothingKept()

    def test_manual_fallback_without_opening(self):
        with mock.patch.object(update, "current_bundle", return_value=None), \
             mock.patch.object(update.subprocess, "run") as run:
            self.assertEqual(update.install_mac("x.dmg", lambda c: None, open_fallback=False), {"manual": "x.dmg"})
            run.assert_not_called()


class Later_(Fixture):
    """The automatic restart's warning: a countdown on the job, and cancelling it is "Later", not a failure."""
    def setUp(self):
        super().setUp()
        for k, v in (("_checking", [True]), ("_later", set()), ("AUTO_WARNING", 0.6)):
            p = mock.patch.object(update, k, v); p.start(); self.addCleanup(p.stop)
        update.set_mode("auto"); d = update._read(); d.update(checked=__import__("time").time(), release=self.rel); update._write(d)

    def run_job(self, job, cancel_after=None):
        import threading
        if cancel_after is not None: threading.Timer(cancel_after, lambda: job.__setitem__("cancel", True)).start()
        with mock.patch.object(update.urllib.request, "urlopen", side_effect=self.fake_open), \
             mock.patch.object(update, "install_mac", return_value={"restarting": "x"}) as inst:
            out = update.install(job, lambda c: None, auto=True, may_install=lambda: True)
        return out, inst

    def test_countdown_then_restart(self):
        job = {}; out, inst = self.run_job(job)
        self.assertEqual(out, {"restarting": "x"}); self.assertEqual(job["version"], "1.2.0"); self.assertIn("restart_at", job)
        self.assertGreater(job["restart_at"], 0)

    def test_later_during_the_countdown(self):
        job = {}; out, inst = self.run_job(job, cancel_after=0.2)
        self.assertEqual(out, {"later": "1.2.0"}); inst.assert_not_called()
        self.assertFalse(update.auto_due())                                         # not again in this app run
        self.assertTrue(update.status()["installable"])                             # a click still installs it
        self.assertNotEqual(update._read().get("auto_failed"), "1.2.0")             # and it is not a failure
        update._later.clear(); self.assertTrue(update.auto_due())                   # the next app start
        self.assertTrue(os.path.isfile(os.path.join(self.d, "cache", "updates", "1.2.0", "CallidoRestore-arm64.dmg")))

    def test_later_during_the_download(self):
        job = {"cancel": True}; out, inst = self.run_job(job)
        self.assertEqual(out, {"later": "1.2.0"}); inst.assert_not_called(); self.assertNotEqual(update._read().get("auto_failed"), "1.2.0")

    def test_a_click_has_no_countdown(self):
        job = {}
        with mock.patch.object(update.urllib.request, "urlopen", side_effect=self.fake_open), \
             mock.patch.object(update, "install_mac", return_value={"restarting": "x"}):
            update.install(job, lambda c: None)
        self.assertNotIn("restart_at", job)
