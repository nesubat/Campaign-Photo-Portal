"""
Background relay: pushes finalized (submitted) photos to Google Drive via
the Apps Script Web App (see apps-script/DriveUploader.gs).

Runs in a daemon thread started from app.py. Local upload/gallery
functionality never waits on this - it only makes Drive sync eventually
consistent, with backoff on failure so a Drive outage can't spam retries.
Photos only become eligible for sync once their batch is submitted
(db.pending_drive_uploads filters on status = 'finalized'), so nothing
reaches Drive until the user taps Submit.
"""
import base64
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

import db
from config import CATEGORIES, UPLOAD_DIR, DRIVE_SYNC_INTERVAL_SEC, load_drive_config

_stop_event = threading.Event()


def _backoff_seconds(previous_error_count):
    # 10s, 30s, 1m, 5m, capped at 15m so a prolonged outage doesn't hammer the endpoint.
    steps = [10, 30, 60, 300, 900]
    return steps[min(previous_error_count, len(steps) - 1)]


def _sync_one(upload_row, cfg):
    path = UPLOAD_DIR / upload_row["job_number"] / upload_row["category"] / upload_row["filename"]
    if not path.exists():
        db.mark_drive_error(upload_row["id"], "local file missing", None)
        return

    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")

    payload = {
        "secret": cfg["sharedSecret"],
        "jobNumber": upload_row["job_number"],
        "category": CATEGORIES.get(upload_row["category"], upload_row["category"]),
        "fileName": upload_row["filename"],
        "employeeName": upload_row["employee_name"],
        "uploadedAt": upload_row["uploaded_at"],
        "contentB64": b64,
    }

    resp = requests.post(cfg["webAppUrl"], json=payload, timeout=60)
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "unknown error from Apps Script"))

    db.mark_drive_synced(upload_row["id"], result.get("fileId", ""))


def _run_loop():
    error_counts = {}
    while not _stop_event.is_set():
        cfg = load_drive_config()
        if cfg:
            try:
                for row in db.pending_drive_uploads():
                    try:
                        _sync_one(row, cfg)
                        error_counts.pop(row["id"], None)
                    except Exception as exc:  # noqa: BLE001 - log and keep the loop alive
                        n = error_counts.get(row["id"], 0) + 1
                        error_counts[row["id"]] = n
                        retry_at = (
                            datetime.now(timezone.utc).astimezone()
                            + timedelta(seconds=_backoff_seconds(n - 1))
                        ).isoformat(timespec="seconds")
                        db.mark_drive_error(row["id"], str(exc), retry_at)
                        print(f"[drive-sync] upload {row['id']} failed: {exc}")
            except Exception as exc:  # noqa: BLE001 - never let the worker thread die
                print(f"[drive-sync] loop error: {exc}")
        _stop_event.wait(DRIVE_SYNC_INTERVAL_SEC)


def start_background_sync():
    t = threading.Thread(target=_run_loop, name="drive-sync", daemon=True)
    t.start()
    return t


def stop_background_sync():
    _stop_event.set()
