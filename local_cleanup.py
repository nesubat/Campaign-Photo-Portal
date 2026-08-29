"""
Background worker: deletes the local copy (full-res + thumbnail) of photos
that have been confirmed synced to Google Drive - AND, for anything tagged
to a consignment, confirmed logged in that job's Sheet too, see
db.uploads_ready_for_local_cleanup - for at least LOCAL_CLEANUP_AFTER_DAYS,
to free disk space on the portal machine - then prunes any job/category/
thumbs directory that's now empty as a result.

Runs in a daemon thread started from app.py/serve.py. Never touches a photo
that hasn't synced (to both Drive and the Sheet, where applicable) yet, so a
Drive or Apps Script outage just delays cleanup - it never causes data loss.
The directory pruning step is safe the same way even when
two people are working the same job at once: it only ever calls rmdir(),
which refuses (raises OSError) if anything is still inside - it can never
force-delete a file. So if a second, concurrent or later session on the same
job still has anything sitting there (staged, unsynced, or too-recently-
synced), that job's directories simply aren't empty yet and pruning is a
no-op until they genuinely are - no explicit "is another session active"
tracking needed.
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


def _rmdir_if_empty(path):
    try:
        path.rmdir()  # raises OSError if anything's still inside - never force-deletes
    except OSError:
        pass


def _prune_empty_dirs():
    if not UPLOAD_DIR.exists():
        return
    for job_dir in list(UPLOAD_DIR.iterdir()):
        if not job_dir.is_dir():
            continue
        for category_dir in list(job_dir.iterdir()):
            if not category_dir.is_dir():
                continue
            thumbs_dir = category_dir / "thumbs"
            if thumbs_dir.is_dir():
                _rmdir_if_empty(thumbs_dir)
            _rmdir_if_empty(category_dir)
        _rmdir_if_empty(job_dir)


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

        try:
            _prune_empty_dirs()
        except Exception as exc:  # noqa: BLE001 - never let the worker thread die
            print(f"[local-cleanup] prune error: {exc}")

        _stop_event.wait(LOCAL_CLEANUP_INTERVAL_SEC)


def start_background_cleanup():
    t = threading.Thread(target=_run_loop, name="local-cleanup", daemon=True)
    t.start()
    return t


def stop_background_cleanup():
    _stop_event.set()
