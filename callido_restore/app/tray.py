"""The app's icon in the macOS menu bar / the Windows notification area (system tray), shown while the "keep running" setting
(appsettings ``tray``) is on. Hovering it shows what runs (files done, download and upload speed, the current file); its menu
shows the same lines, "Show Callido Restore" and "Quit". With the setting on, closing the window hides it (the app, and a
restore, keep running); the icon or its menu brings it back.

No extra packages: macOS uses AppKit through PyObjC (already there for pywebview's window), Windows uses Shell_NotifyIconW
through ctypes on a thread of its own with its own hidden window and message loop. Everything here is best effort: when the icon
cannot be created the app works as before (closing the window quits)."""
import ctypes, os, sys, threading, time

from callido_restore.about import APP_NAME


# ----------------------------------------------------------------------------- what the icon says
def rate(n):
    """bytes/s as the page shows them (1024-based)."""
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024: return f"{n:.0f} {u}/s" if u == "B" else f"{n:.1f} {u}/s"
        n /= 1024
    return f"{n:.1f} TB/s"


def status_lines(jobs):
    """Up to three short lines about the running jobs, for the hover text and the menu."""
    run = [j for j in jobs if j.get("state") == "running"]
    if not run: return ["Nothing running"]
    r = next((j for j in run if j.get("label") == "restore"), None)
    if r:
        done, total = r.get("done_files") or 0, r.get("total_files") or 0
        head = f"Restoring {done:,} / {total:,} files" if total else "Restoring"
        if r.get("progress") is not None: head += f" · {int(round(100 * float(r['progress'])))} %"
        errs = r.get("errors_total") or 0
        if errs: head += f" · {errs:,} error{'s' if errs != 1 else ''}"
        rates = r.get("rates") or {}
        lines = [head, f"↓ {rate(rates.get('download'))} from the backup · ↑ {rate(rates.get('upload'))} to the target"]
        msg = r.get("message") or ""
        name = msg.split(": ", 1)[1] if ": " in msg and msg.split(": ", 1)[0].replace("/", "").isdigit() else msg
        name = name.rsplit("/", 1)[-1]
        if name: lines.append(name)
        return lines
    j = run[0]
    head = f"{j.get('label') or 'working'} · {int(round(100 * float(j.get('progress') or 0)))} %"
    return [head] + ([j["message"][:80]] if j.get("message") else [])


def tooltip(lines, limit=127):
    """The hover text: the lines, cut to what Windows allows (128 characters with the terminator)."""
    t = APP_NAME + "\n" + "\n".join(lines)
    return t if len(t) <= limit else t[:limit - 1] + "…"


# ----------------------------------------------------------------------------- the icon
class Tray:
    """``Tray(on_show, on_quit, jobs)``: start() shows the icon, update() refreshes its text from ``jobs()``, stop() removes it.
    on_show / on_quit are called on a thread of their own (never the UI thread: they may wait for the window)."""
    def __init__(self, on_show, on_quit, jobs):
        self.on_show, self.on_quit, self.jobs = on_show, on_quit, jobs
        self.impl = None; self._ticker = None; self._lock = threading.Lock()

    def _call(self, fn): threading.Thread(target=fn, daemon=True, name="tray-action").start()

    def start(self):
        with self._lock:
            if self.impl: return True
            impl = _MacIcon(self) if sys.platform == "darwin" else _WinIcon(self) if os.name == "nt" else None
            if impl is None: return False
            impl.start(); self.impl = impl
        if not self._ticker:
            self._ticker = threading.Thread(target=self._tick, daemon=True, name="tray-status"); self._ticker.start()
        self.update()
        return True

    def stop(self):
        with self._lock:
            impl, self.impl = self.impl, None
        if impl: impl.stop()

    @property
    def shown(self):
        """the icon is really there (a close may only hide the window then: it is the way back)"""
        impl = self.impl
        return impl is not None and impl.alive()

    def update(self):
        impl = self.impl
        if not impl: return
        try: lines = status_lines(self.jobs())
        except Exception: lines = ["…"]
        impl.set_status(lines)

    def _tick(self):
        while True:
            time.sleep(2)
            try: self.update()
            except Exception: pass

    # macOS: the window is hidden -> no Dock icon (a menu-bar app); shown -> a normal app again
    def dock(self, visible):
        if isinstance(self.impl, _MacIcon): self.impl.dock(visible)


# ----------------------------------------------------------------------------- macOS: NSStatusItem
_MAC_TARGET = None

def _mac_target_class():
    global _MAC_TARGET
    if _MAC_TARGET is None:
        from Foundation import NSObject
        class CallidoTrayTarget(NSObject):
            def showApp_(self, sender): self.tray._call(self.tray.on_show)
            def quitApp_(self, sender): self.tray._call(self.tray.on_quit)
        _MAC_TARGET = CallidoTrayTarget
    return _MAC_TARGET


class _MacIcon:
    LINES = 3

    def __init__(self, tray): self.tray = tray; self.item = None; self.status_items = []

    def _main(self, fn, *a):
        from PyObjCTools import AppHelper
        AppHelper.callAfter(fn, *a)

    def start(self): self._main(self._create)

    def alive(self): return self.item is not None

    def _create(self):
        try: self._build()
        except Exception as e:
            self.item = None; print(f"tray: menu-bar icon failed ({e}); closing the window quits the app", flush=True)

    def _build(self):
        import AppKit
        self.target = _mac_target_class().alloc().init(); self.target.tray = self.tray
        self.item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        button = self.item.button()
        img = None
        if hasattr(AppKit.NSImage, "imageWithSystemSymbolName_accessibilityDescription_"):     # macOS 11+
            img = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_("externaldrive.badge.timemachine", APP_NAME)
        if img is not None: img.setTemplate_(True); button.setImage_(img)
        else: button.setTitle_("CR")
        menu = AppKit.NSMenu.alloc().init(); menu.setAutoenablesItems_(False)
        for _ in range(self.LINES):
            it = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("", None, ""); it.setEnabled_(False); it.setHidden_(True)
            menu.addItem_(it); self.status_items.append(it)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        for title, action in ((f"Show {APP_NAME}", "showApp:"), (f"Quit {APP_NAME}", "quitApp:")):
            it = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, ""); it.setTarget_(self.target); menu.addItem_(it)
        self.item.setMenu_(menu)

    def set_status(self, lines):
        def apply():
            if not self.item: return
            self.item.button().setToolTip_(tooltip(lines, limit=1000))
            for k, it in enumerate(self.status_items):
                it.setTitle_(lines[k] if k < len(lines) else ""); it.setHidden_(k >= len(lines))
        self._main(apply)

    def dock(self, visible):
        def apply():
            import AppKit
            app = AppKit.NSApplication.sharedApplication()
            app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular if visible else AppKit.NSApplicationActivationPolicyAccessory)
            if visible: app.activateIgnoringOtherApps_(True)
        self._main(apply)

    def stop(self):
        def apply():
            import AppKit
            if self.item: AppKit.NSStatusBar.systemStatusBar().removeStatusItem_(self.item); self.item = None
        self._main(apply)


def mac_close_was_the_window():
    """macOS: pywebview runs the same closing handler for the window's close button and for Quit (Cmd+Q, the Dock, log out).
    True only when the last event this app received, less than 2 s ago, was a mouse click or Cmd+W: then the window is hidden;
    anything else quits as before. Fed by ``mac_watch_events``."""
    t, kind = _LAST_EVENT
    return kind in ("mouse", "cmd-w") and time.monotonic() - t < 2

_LAST_EVENT = [0.0, ""]

def mac_watch_events():
    """Record the kind of the app's last mouse / key event (an NSEvent local monitor, on the main thread)."""
    import AppKit
    from PyObjCTools import AppHelper
    def handler(ev):
        try:
            if ev.type() == AppKit.NSEventTypeKeyDown:
                cmd = bool(ev.modifierFlags() & AppKit.NSEventModifierFlagCommand)
                ch = (ev.charactersIgnoringModifiers() or "").lower()
                _LAST_EVENT[:] = [time.monotonic(), "cmd-w" if cmd and ch == "w" else "cmd-q" if cmd and ch == "q" else "key"]
            else: _LAST_EVENT[:] = [time.monotonic(), "mouse"]
        except Exception: pass
        return ev
    mask = AppKit.NSEventMaskLeftMouseDown | AppKit.NSEventMaskLeftMouseUp | AppKit.NSEventMaskKeyDown
    AppHelper.callAfter(lambda: _MONITOR.append(AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(mask, handler)))

_MONITOR = []


# ----------------------------------------------------------------------------- Windows: Shell_NotifyIconW
class _GUID(ctypes.Structure):
    _fields_ = [("a", ctypes.c_ubyte * 16)]

def _notifyicondata():
    """NOTIFYICONDATAW (Vista+ layout; 976 bytes on 64-bit Windows)."""
    P = ctypes.c_void_p
    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint32), ("hWnd", P), ("uID", ctypes.c_uint32), ("uFlags", ctypes.c_uint32),
                    ("uCallbackMessage", ctypes.c_uint32), ("hIcon", P), ("szTip", ctypes.c_wchar * 128),
                    ("dwState", ctypes.c_uint32), ("dwStateMask", ctypes.c_uint32), ("szInfo", ctypes.c_wchar * 256),
                    ("uVersion", ctypes.c_uint32), ("szInfoTitle", ctypes.c_wchar * 64), ("dwInfoFlags", ctypes.c_uint32),
                    ("guidItem", _GUID), ("hBalloonIcon", P)]
    return NOTIFYICONDATAW


class _WinIcon:
    WM_TRAY = 0x8000 + 1                                        # WM_APP + 1
    NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
    NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4
    WM_DESTROY, WM_CLOSE, WM_COMMAND, WM_NULL = 0x0002, 0x0010, 0x0111, 0x0000
    WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_RBUTTONUP = 0x0202, 0x0203, 0x0205
    ID_SHOW, ID_QUIT, ID_STATUS = 1, 2, 10

    def __init__(self, tray):
        self.tray = tray; self.hwnd = None; self.lines = ["Nothing running"]; self.ready = threading.Event(); self.err = None

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="tray-win").start()
        self.ready.wait(10)
        if self.err: raise self.err

    def _api(self):
        import ctypes
        from ctypes import wintypes as wt
        u, s, k = ctypes.WinDLL("user32", use_last_error=True), ctypes.WinDLL("shell32"), ctypes.WinDLL("kernel32")
        LRESULT, WPARAM, LPARAM, P = ctypes.c_ssize_t, ctypes.c_size_t, ctypes.c_ssize_t, ctypes.c_void_p
        self.WNDPROC = ctypes.WINFUNCTYPE(LRESULT, P, ctypes.c_uint, WPARAM, LPARAM)
        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", self.WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", P), ("hIcon", P), ("hCursor", P), ("hbrBackground", P), ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]
        self.WNDCLASSW = WNDCLASSW
        sig = {u.DefWindowProcW: (LRESULT, [P, ctypes.c_uint, WPARAM, LPARAM]), u.RegisterClassW: (ctypes.c_uint16, [ctypes.POINTER(WNDCLASSW)]),
               u.CreateWindowExW: (P, [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, P, P, P, P]),
               u.GetMessageW: (wt.BOOL, [P, P, ctypes.c_uint, ctypes.c_uint]), u.TranslateMessage: (wt.BOOL, [P]), u.DispatchMessageW: (LRESULT, [P]),
               u.PostMessageW: (wt.BOOL, [P, ctypes.c_uint, WPARAM, LPARAM]), u.PostQuitMessage: (None, [ctypes.c_int]),
               u.DestroyWindow: (wt.BOOL, [P]), u.CreatePopupMenu: (P, []), u.AppendMenuW: (wt.BOOL, [P, ctypes.c_uint, ctypes.c_size_t, wt.LPCWSTR]),
               u.TrackPopupMenu: (ctypes.c_int, [P, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_int, P, P]), u.DestroyMenu: (wt.BOOL, [P]),
               u.GetCursorPos: (wt.BOOL, [ctypes.POINTER(wt.POINT)]), u.SetForegroundWindow: (wt.BOOL, [P]),
               u.LoadIconW: (P, [P, P]), u.LoadImageW: (P, [P, wt.LPCWSTR, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_uint]),
               u.RegisterWindowMessageW: (ctypes.c_uint, [wt.LPCWSTR]), s.Shell_NotifyIconW: (wt.BOOL, [wt.DWORD, P]),
               s.ExtractIconW: (P, [P, wt.LPCWSTR, ctypes.c_uint]), k.GetModuleHandleW: (P, [wt.LPCWSTR])}
        for f, (res, args) in sig.items(): f.restype, f.argtypes = res, args
        return ctypes, wt, u, s, k

    def _icon(self, ctypes, u, s, k):
        """the app's own icon: the packaged exe carries AppIcon.ico; from source the file in assets/; else Windows' default"""
        if getattr(sys, "frozen", False):
            h = s.ExtractIconW(k.GetModuleHandleW(None), sys.executable, 0)
            if h and h != 1: return h
        ico = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "assets", "AppIcon.ico")
        if os.path.isfile(ico):
            h = u.LoadImageW(None, ico, 1, 0, 0, 0x10 | 0x40)     # IMAGE_ICON, LR_LOADFROMFILE | LR_DEFAULTSIZE
            if h: return h
        return u.LoadIconW(None, ctypes.c_void_p(32512))         # IDI_APPLICATION

    def _nid(self, flags):
        nid = self.NID(); nid.cbSize = ctypes.sizeof(self.NID); nid.hWnd = self.hwnd; nid.uID = 1; nid.uFlags = flags
        nid.uCallbackMessage = self.WM_TRAY; nid.hIcon = self.hicon; nid.szTip = tooltip(self.lines)
        return nid

    def _notify(self, op, flags):
        nid = self._nid(flags)
        return self.s.Shell_NotifyIconW(op, ctypes.byref(nid))

    def _run(self):
        try:
            _, wt, u, s, k = self._api(); self.u, self.s = u, s; self.NID = _notifyicondata()
            taskbar_created = u.RegisterWindowMessageW("TaskbarCreated")   # Explorer restarted: the icon must be added again
            def wndproc(hwnd, msg, wp, lp):
                try:
                    if msg == self.WM_TRAY:
                        ev = lp & 0xFFFF
                        if ev in (self.WM_LBUTTONUP, self.WM_LBUTTONDBLCLK): self.tray._call(self.tray.on_show)
                        elif ev == self.WM_RBUTTONUP: self._menu()
                        return 0
                    if msg == taskbar_created: self._notify(self.NIM_ADD, self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP); return 0
                    if msg == self.WM_CLOSE: u.DestroyWindow(hwnd); return 0
                    if msg == self.WM_DESTROY:
                        self._notify(self.NIM_DELETE, 0); u.PostQuitMessage(0); return 0
                except Exception: pass
                return u.DefWindowProcW(hwnd, msg, wp, lp)
            self._proc = self.WNDPROC(wndproc)                       # kept: Windows calls it for the window's lifetime
            hinst = k.GetModuleHandleW(None)
            wc = self.WNDCLASSW(); wc.lpfnWndProc = self._proc; wc.hInstance = hinst; wc.lpszClassName = "CallidoRestoreTray"
            u.RegisterClassW(ctypes.byref(wc))
            self.hwnd = u.CreateWindowExW(0, "CallidoRestoreTray", APP_NAME, 0, 0, 0, 0, 0, None, None, hinst, None)
            if not self.hwnd: raise OSError(f"CreateWindowExW failed ({ctypes.get_last_error()})")
            self.hicon = self._icon(ctypes, u, s, k)
            if not self._notify(self.NIM_ADD, self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP): raise OSError("Shell_NotifyIconW(NIM_ADD) failed")
        except Exception as e:
            self.err = e; self.ready.set(); return
        self.ready.set()
        msg = wt.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u.TranslateMessage(ctypes.byref(msg)); u.DispatchMessageW(ctypes.byref(msg))

    def _menu(self):
        u = self.u; from ctypes import wintypes as wt
        m = u.CreatePopupMenu()
        for k, line in enumerate(self.lines):
            u.AppendMenuW(m, 0x0001, self.ID_STATUS + k, line)      # MF_GRAYED: information only
        u.AppendMenuW(m, 0x0800, 0, None)                           # MF_SEPARATOR
        u.AppendMenuW(m, 0, self.ID_SHOW, f"Show {APP_NAME}")
        u.AppendMenuW(m, 0, self.ID_QUIT, f"Quit {APP_NAME}")
        pt = wt.POINT(); u.GetCursorPos(ctypes.byref(pt))
        u.SetForegroundWindow(self.hwnd)                            # or the menu does not close when clicking elsewhere
        cmd = u.TrackPopupMenu(m, 0x0100 | 0x0002, pt.x, pt.y, 0, self.hwnd, None)   # TPM_RETURNCMD | TPM_RIGHTBUTTON
        u.PostMessageW(self.hwnd, self.WM_NULL, 0, 0); u.DestroyMenu(m)
        if cmd == self.ID_SHOW: self.tray._call(self.tray.on_show)
        elif cmd == self.ID_QUIT: self.tray._call(self.tray.on_quit)

    def alive(self): return self.hwnd is not None

    def set_status(self, lines):
        self.lines = list(lines)
        if self.hwnd: self._notify(self.NIM_MODIFY, self.NIF_TIP)

    def stop(self):
        if self.hwnd: self.u.PostMessageW(self.hwnd, self.WM_CLOSE, 0, 0)
