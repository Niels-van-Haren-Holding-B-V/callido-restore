"""Background jobs: ``start_job`` runs a function in a thread and exposes progress/message/result in ``JOBS`` (the UI polls
``/api/job``). Restore jobs are also persisted under ``paths.JOBS_DIR`` by ``Backup.restore``."""
import threading, time, traceback, uuid

JOBS = {}

def start_job(fn, label):
    jid = uuid.uuid4().hex[:10]
    job = {"id": jid, "label": label, "state": "running", "progress": 0.0, "message": "starting", "result": None, "error": None, "started": time.time()}
    JOBS[jid] = job
    def run():
        try:
            job["result"] = fn(job); job["state"] = "done"
            if not (isinstance(job["result"], dict) and job["result"].get("stopped")): job["progress"] = 1.0
        except Exception as e:
            job["state"] = "error"; job["error"] = f"{e}"; job["trace"] = traceback.format_exc()
    threading.Thread(target=run, daemon=True).start()
    return job
