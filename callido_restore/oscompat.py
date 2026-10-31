"""Windows counterparts of the POSIX process calls the app relies on (signals, process groups, ``resource``). Bottom layer like
``paths``: imports nothing from the package. On macOS and Linux every function here keeps the existing behaviour."""
import ctypes, os, subprocess

WINDOWS = os.name == "nt"
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000


def popen_kw(new_group=False):
    """Extra ``subprocess`` arguments for a child of the app. Windows: no console window (the app is a windowed exe, so every
    rclone.exe call would flash one), and with ``new_group`` its own process group. POSIX: nothing, or a new session for
    ``new_group`` (the mapper: Stop ends it together with its scan workers)."""
    if WINDOWS: return {"creationflags": CREATE_NO_WINDOW | (CREATE_NEW_PROCESS_GROUP if new_group else 0)}
    return {"start_new_session": True} if new_group else {}


def _kernel32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = ctypes.c_void_p
    k.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
    k.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong))
    k.CloseHandle.argtypes = (ctypes.c_void_p,)
    k.GetCurrentProcess.restype = ctypes.c_void_p
    return k


def pid_alive(pid):
    """Windows: True when a process with this pid runs. (``os.kill(pid, 0)`` is no probe there: 0 is CTRL_C_EVENT.)"""
    k = _kernel32()
    h = k.OpenProcess(0x1000, False, int(pid))                     # PROCESS_QUERY_LIMITED_INFORMATION
    if not h: return ctypes.get_last_error() == 5                  # ERROR_ACCESS_DENIED: it exists, it is just not ours
    try:
        code = ctypes.c_ulong()
        if not k.GetExitCodeProcess(h, ctypes.byref(code)): return True
        return code.value == 259                                   # STILL_ACTIVE
    finally: k.CloseHandle(h)


class _MemCounters(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + [(n, ctypes.c_size_t) for n in (
        "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
        "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]


def peak_rss_mb():
    """Windows: peak working set of this process in MB (what ``ru_maxrss`` reports on POSIX); 0 when it cannot be read."""
    k = _kernel32()
    c = _MemCounters(); c.cb = ctypes.sizeof(c)
    fn = k.K32GetProcessMemoryInfo
    fn.argtypes = (ctypes.c_void_p, ctypes.POINTER(_MemCounters), ctypes.c_ulong)
    if not fn(k.GetCurrentProcess(), ctypes.byref(c), c.cb): return 0
    return c.PeakWorkingSetSize / (1 << 20)


def kill_tree(pid):
    """Windows: end a process and all its children (the mapper and its scan workers). True when taskkill succeeded."""
    r = subprocess.run(["taskkill", "/T", "/F", "/PID", str(int(pid))], capture_output=True, **popen_kw())
    return r.returncode == 0
