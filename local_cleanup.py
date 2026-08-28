"""
Background worker: deletes the local copy (full-res + thumbnail) of photos
that have been confirmed synced to Google Drive for at least
LOCAL_CLEANUP_AFTER_DAYS, to free disk space on the portal machine.

Runs in a daemon thread started from app.py/serve.py. Never touches a photo
that hasn't synced yet, so a Drive outage just delays cleanup - it never
causes data loss.
"""
import threading
from datetime import datetime, timedelta, timezone

import db
from config import LOCAL_CLEANUP_AFTER_DAYS, LOCAL_CLEANUP_INTERVAL_SEC, UPLOAD_DIR

_stop_event = threading.Event()


def _cleanup_one(row):
    full_path = UPLOAD_DIR / row["job_number"] / row["category"] / row["filename"]
    thumb_path = UPLOAD_DIR / row["job_number"] / row["category"] / "thumbs" / row["thumb_filename"]
    full_path.unlink(missing_ok=True)
    thumb_path.unlink(missing_ok=True)
    db.mark_local_cleaned(row["id"])


def _run_loop():
    while not _stop_event.is_set():
        cutoff = (
            datetime.now(timezone.utc).astimezone() - timedelta(days=LOCAL_CLEANUP_AFTER_DAYS)
        ).isoformat(timespec="seconds")
        try:
            for row in db.uploads_ready_for_local_cleanup(cutoff):
                try:
                    _cleanup_one(row)
                except Exception as exc:  # noqa: BLE001 - log and keep the loop alive
                    print(f"[local-cleanup] upload {row['id']} failed: {exc}")
        except Exception as exc:  # noqa: BLE001 - never let the worker thread die
            print(f"[local-cleanup] loop error: {exc}")
        _stop_event.wait(LOCAL_CLEANUP_INTERVAL_SEC)


def start_background_cleanup():
    t = threading.Thread(target=_run_loop, name="local-cleanup", daemon=True)
    t.start()
    return t


def stop_background_cleanup():
    _stop_event.set()
