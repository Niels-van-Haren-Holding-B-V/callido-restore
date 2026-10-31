"""Background jobs: ``start_job`` runs a function in a thread and exposes progress/message/result in ``JOBS`` (the UI polls
``/api/job``). Restore jobs are also persisted under ``paths.JOBS_DIR`` by ``Backup.restore``."""
import threading, time, traceback, uuid

JOBS = {}

def log(msg):
    """One timestamped line on stdout, which is app.log in the packaged app."""
    try: print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)
    except Exception: pass                                  # no usable stdout (windowed app without a log file)

def start_job(fn, label, **extra):
    """``extra`` keys (e.g. the source id of a pool job) are on the job before it is registered, so a concurrent lookup never
    sees a job without them."""
    jid = uuid.uuid4().hex[:10]
    job = {"id": jid, "label": label, "state": "running", "progress": 0.0, "message": "starting", "result": None, "error": None, "started": time.time(), **extra}
    JOBS[jid] = job
    def run():
        try:
            job["result"] = fn(job); job["state"] = "done"
            if not (isinstance(job["result"], dict) and job["result"].get("stopped")): job["progress"] = 1.0
        except Exception as e:
            # app.log (stdout in the packaged app): a failed connect or open used to leave no trace there at all.
            # Logged before the state flips, so whoever sees "error" also finds the line.
            log(f"job {label!r} failed after {time.time() - job['started']:.0f} s: {e}")
            job["error"] = f"{e}"; job["trace"] = traceback.format_exc(); job["state"] = "error"
    threading.Thread(target=run, daemon=True).start()
    return job
