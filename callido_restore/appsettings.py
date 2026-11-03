"""App window settings in ~/.hbk-explorer/app.json: ``tray`` = keep running in the menu bar (macOS) or the notification area
(Windows) when the window is closed. ``on_change`` lets the app window react (show or remove the icon) when the page changes it;
``TRAY_AVAILABLE`` is set by the app window once its icon works (the page offers the setting only then)."""
import json, threading
from callido_restore import paths

DEFAULTS = {"tray": True}
TRAY_AVAILABLE = [False]
_lock = threading.Lock(); _listeners = []


def _file(): return paths.CONF_DIR / "app.json"


def load():
    try: d = json.loads(_file().read_text())
    except (OSError, ValueError): d = {}
    return {k: (d[k] if isinstance(d.get(k), type(v)) else v) for k, v in DEFAULTS.items()}


def get(key): return load()[key]


def update(**kw):
    """Change settings (unknown keys and wrong types are refused); listeners get the new settings."""
    with _lock:
        d = load()
        for k, v in kw.items():
            if k not in DEFAULTS or not isinstance(v, type(DEFAULTS[k])): raise ValueError(f"unknown setting {k}={v!r}")
            d[k] = v
        paths.CONF_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _file().with_suffix(".tmp"); tmp.write_text(json.dumps(d)); tmp.replace(_file())
    for cb in list(_listeners):
        try: cb(d)
        except Exception: pass
    return d


def on_change(cb): _listeners.append(cb)
