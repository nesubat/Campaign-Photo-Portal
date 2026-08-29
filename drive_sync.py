"""
Background relay: pushes finalized (submitted) photos to Google Drive via
the Apps Script Web App (see apps-script/DriveUploader.gs), then updates each
touched consignment's Sheet row once its newly-synced photos are all in.

Runs in a daemon thread started from app.py. Local upload/gallery
functionality never waits on this - it only makes Drive sync eventually
consistent, with backoff on failure so a Drive outage can't spam retries.
Photos only become eligible for sync once their batch is submitted
(db.pending_drive_uploads_grouped filters on status = 'finalized'), so
nothing reaches Drive until the user taps Submit.

Uploads and Sheet updates are both BATCHED - one Apps Script execution per
batch, rather than one per photo/consignment. Sheet-update batches are
capped by consignment count (DRIVE_SHEET_BATCH_CAP) since that payload is
just text. Upload batches are capped by actual file SIZE
(DRIVE_UPLOAD_BATCH_MAX_BYTES, with DRIVE_UPLOAD_BATCH_MAX_COUNT as a
fallback) - see _split_into_batches - because real phone photos run several
MB each, so a count-only cap can silently balloon into a huge request that
fails outright on a slow connection (this happened in practice: 20 photos at
~6MB average produced a ~150MB base64 payload that timed out mid-send).
Grouping by (job_number, category) still matters even for small
consignments, despite each being capped at only a handful of photos: many
small consignments in one job (e.g. 100 consignments x 1 photo each) still
need combining across consignments, or batching "by consignment" alone
would mean 100 executions anyway. Apps Script still tries each file/
consignment independently within a batch and reports per-item results, so
one bad item can't block the rest of its batch; and its upload endpoint is
idempotent by filename, so retrying an entire batch after an uncertain
failure (e.g. a timeout where we never learned what succeeded) is always
safe - already-created files are recognized and reused, never duplicated.
"""
import base64
import threading
from datetime import datetime, timedelta, timezone

import requests

import db
from config import (
    CATEGORIES,
    CONSIGNMENT_LOGGING_CATEGORY,
    DRIVE_BATCH_TIMEOUT_SEC,
    DRIVE_SHEET_BATCH_CAP,
    DRIVE_SYNC_INTERVAL_SEC,
    DRIVE_UPLOAD_BATCH_MAX_BYTES,
    DRIVE_UPLOAD_BATCH_MAX_COUNT,
    UPLOAD_DIR,
    load_drive_config,
)

_stop_event = threading.Event()


def _backoff_seconds(previous_error_count):
    # 10s, 30s, 1m, 5m, capped at 15m so a prolonged outage doesn't hammer the endpoint.
    steps = [10, 30, 60, 300, 900]
    return steps[min(previous_error_count, len(steps) - 1)]


def _drive_view_url(drive_file_id):
    return f"https://drive.google.com/file/d/{drive_file_id}/view"


def check_job_in_drive(job_number, cfg, timeout=10):
    """Synchronous (not part of the background loop) - called from app.py's
    /start so a resumed "keep logs" job can rehydrate before the user starts
    adding photos. Returns {"found": False} or {"found": True, "rows": [...]}
    where each row mirrors one line of that job's Photo Log sheet."""
    payload = {"secret": cfg["sharedSecret"], "action": "checkJob", "jobNumber": job_number}
    resp = requests.post(cfg["webAppUrl"], json=payload, timeout=timeout)
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "unknown error from Apps Script"))
    return result


def _mark_row_error(row, error_text, error_counts):
    n = error_counts.get(row["id"], 0) + 1
    error_counts[row["id"]] = n
    retry_at = (
        datetime.now(timezone.utc).astimezone() + timedelta(seconds=_backoff_seconds(n - 1))
    ).isoformat(timespec="seconds")
    db.mark_drive_error(row["id"], error_text, retry_at)


def _split_into_batches(rows, max_bytes, max_count):
    """Slices one (job_number, category) group of rows into upload batches
    bounded by whichever limit is hit first: total raw file size, or photo
    count. Checks each file's REAL size on disk, since that's what actually
    determines request size/transfer time - a fixed photo-count cap has no
    way to know a batch of 20 real photos is wildly different from 20 tiny
    test fixtures. Always keeps at least one file per batch, even if that
    single file's own size exceeds max_bytes - there's no way to split one
    file across multiple requests, so an oversized single photo just goes
    out alone rather than being silently dropped. A missing file (already a
    separate error case _sync_batch reports) is treated as size 0 here so it
    can't skew the sizing of the rest of the batch."""
    batches = []
    current = []
    current_bytes = 0
    for row in rows:
        path = UPLOAD_DIR / row["job_number"] / row["category"] / row["filename"]
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if current and (len(current) >= max_count or current_bytes + size > max_bytes):
            batches.append(current)
            current = []
            current_bytes = 0
        current.append(row)
        current_bytes += size
    if current:
        batches.append(current)
    return batches


def _sync_batch(rows, cfg, error_counts):
    """One Apps Script call for every photo in `rows` (all from the same
    job+category - guaranteed by how batches are grouped in db.py). Each
    file is tried independently server-side and its own result reported, so
    a bad file in the batch doesn't block or fail the others - each row gets
    marked/backed-off individually, exactly as if synced one at a time."""
    job_number = rows[0]["job_number"]
    category = rows[0]["category"]

    files = []
    for row in rows:
        path = UPLOAD_DIR / row["job_number"] / row["category"] / row["filename"]
        if not path.exists():
            db.mark_drive_error(row["id"], "local file missing", None)
            continue
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        files.append((row, {
            "fileName": row["filename"],
            "employeeName": row["employee_name"],
            "uploadedAt": row["uploaded_at"],
            "contentB64": b64,
        }))
    if not files:
        return

    payload = {
        "secret": cfg["sharedSecret"],
        "action": "uploadBatch",
        "jobNumber": job_number,
        "category": CATEGORIES.get(category, category),
        "files": [f for _, f in files],
    }

    resp = requests.post(cfg["webAppUrl"], json=payload, timeout=DRIVE_BATCH_TIMEOUT_SEC)
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        # Whole-batch failure (e.g. Apps Script itself errored before getting
        # to per-file results) - every row in it gets the same backoff. Safe
        # to retry blindly next tick: the upload endpoint is idempotent by
        # filename, so any file that DID succeed just gets recognized and
        # reused rather than duplicated.
        raise RuntimeError(result.get("error", "unknown error from Apps Script"))

    results_by_name = {r.get("fileName"): r for r in result.get("results", [])}
    for row, f in files:
        file_result = results_by_name.get(f["fileName"])
        if file_result and file_result.get("ok"):
            db.mark_drive_synced(row["id"], file_result.get("fileId", ""))
            error_counts.pop(row["id"], None)
        else:
            error_text = (file_result or {}).get("error", "no result returned for this file")
            _mark_row_error(row, error_text, error_counts)


def _log_consignment_batch(job_number, consignment_map, cfg, sheet_error_counts):
    """One Apps Script call updates every consignment in `consignment_map`
    (all belonging to `job_number`) in its own Sheet row. Each entry pushes
    the CURRENT full state (every item ID, contributor, and synced photo
    link so far) for that consignment - sending the whole snapshot each time,
    rather than asking Apps Script to append, means a retry after a partial
    failure just re-sends the same values instead of risking duplicate
    entries; Apps Script itself also merges rather than overwrites, so even
    a stale/incomplete snapshot can't erase existing data. Consignments are
    tried independently server-side too, so one bad one can't block the
    rest of the batch."""
    consignments_payload = []
    consignment_ids_in_order = []
    for consignment_id, upload_ids in consignment_map.items():
        consignment = db.get_consignment(consignment_id)
        if not consignment:
            for uid in upload_ids:
                db.mark_sheet_synced(uid)  # consignment was removed - nothing to log
            continue
        links = [
            _drive_view_url(row["drive_file_id"])
            for row in db.synced_uploads_for_consignment(consignment_id)
            if row["drive_file_id"]
        ]
        consignments_payload.append({
            "keyType": consignment["key_type"],
            "keyValue": consignment["key_value"],
            "itemIds": db.split_list(consignment["item_ids"]),
            "contributors": db.split_list(consignment["contributors"]),
            "photoLinks": links,
            # The actual local moment this consignment was scanned/touched -
            # NOT when this batched Sheet update happens to run, which can be
            # minutes later. See logConsignments_ in DriveUploader.gs.
            "firstLogged": consignment["created_at"],
            "lastUpdated": consignment["updated_at"],
        })
        consignment_ids_in_order.append(consignment_id)

    if not consignments_payload:
        return

    payload = {
        "secret": cfg["sharedSecret"],
        "action": "logConsignments",
        "jobNumber": job_number,
        "consignments": consignments_payload,
    }

    resp = requests.post(cfg["webAppUrl"], json=payload, timeout=DRIVE_BATCH_TIMEOUT_SEC)
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        # Whole-batch failure - every consignment's uploads in it get the
        # same backoff. Safe to retry blindly: each entry is sent as a full
        # snapshot and merged, not appended, so a repeat is harmless.
        raise RuntimeError(result.get("error", "unknown error from Apps Script"))

    results_by_key = {r.get("keyValue"): r for r in result.get("results", [])}
    for consignment_id in consignment_ids_in_order:
        upload_ids = consignment_map[consignment_id]
        consignment = db.get_consignment(consignment_id)
        key_value = consignment["key_value"] if consignment else None
        item_result = results_by_key.get(key_value)
        if item_result and item_result.get("ok"):
            for uid in upload_ids:
                db.mark_sheet_synced(uid)
                sheet_error_counts.pop(uid, None)
        else:
            error_text = (item_result or {}).get("error", "no result returned for this consignment")
            for uid in upload_ids:
                n = sheet_error_counts.get(uid, 0) + 1
                sheet_error_counts[uid] = n
                retry_at = (
                    datetime.now(timezone.utc).astimezone()
                    + timedelta(seconds=_backoff_seconds(n - 1))
                ).isoformat(timespec="seconds")
                db.mark_sheet_error(uid, error_text, retry_at)


def _resize_sheet_columns(job_number, cfg, timeout=15):
    """One-off formatting pass, not part of the regular sync - see
    _maybe_resize_sheet. Tiny payload/response (no file bytes involved), so a
    short timeout is plenty."""
    payload = {"secret": cfg["sharedSecret"], "action": "resizeSheetColumns", "jobNumber": job_number}
    resp = requests.post(cfg["webAppUrl"], json=payload, timeout=timeout)
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "unknown error from Apps Script"))


def _maybe_resize_sheet(job_number, category, cfg):
    """Fires the Sheet's one-time column auto-fit the moment a job+category
    has nothing left pending - not on a schedule, not per-batch, just a cheap
    local check (is_sheet_resized/job_fully_synced are plain DB reads) run
    only for job/category pairs this tick actually touched, so a job that
    never changes never gets rechecked. Only ever fires once per job -
    mark_sheet_resized makes sure a later, unrelated batch for the same job
    (e.g. someone reopens it and logs more consignments) doesn't retrigger it
    on every subsequent sync."""
    if category != CONSIGNMENT_LOGGING_CATEGORY or db.is_sheet_resized(job_number, category):
        return
    if not db.job_fully_synced(job_number, category):
        return
    _resize_sheet_columns(job_number, cfg)
    db.mark_sheet_resized(job_number, category)


def _run_loop():
    error_counts = {}
    sheet_error_counts = {}
    while not _stop_event.is_set():
        cfg = load_drive_config()
        if cfg:
            touched = set()  # (job_number, category) pairs this tick actually did work for

            try:
                for key, group_rows in db.pending_drive_uploads_grouped().items():
                    touched.add(key)
                    for batch in _split_into_batches(
                        group_rows, DRIVE_UPLOAD_BATCH_MAX_BYTES, DRIVE_UPLOAD_BATCH_MAX_COUNT
                    ):
                        try:
                            _sync_batch(batch, cfg, error_counts)
                        except Exception as exc:  # noqa: BLE001 - log and keep the loop alive
                            for row in batch:
                                _mark_row_error(row, str(exc), error_counts)
                            print(f"[drive-sync] batch of {len(batch)} failed: {exc}")
            except Exception as exc:  # noqa: BLE001 - never let the worker thread die
                print(f"[drive-sync] loop error: {exc}")

            try:
                for job_number, consignment_map in db.pending_sheet_log_batches(DRIVE_SHEET_BATCH_CAP):
                    touched.add((job_number, CONSIGNMENT_LOGGING_CATEGORY))
                    try:
                        _log_consignment_batch(job_number, consignment_map, cfg, sheet_error_counts)
                    except Exception as exc:  # noqa: BLE001 - log and keep the loop alive
                        for upload_ids in consignment_map.values():
                            for uid in upload_ids:
                                n = sheet_error_counts.get(uid, 0) + 1
                                sheet_error_counts[uid] = n
                                retry_at = (
                                    datetime.now(timezone.utc).astimezone()
                                    + timedelta(seconds=_backoff_seconds(n - 1))
                                ).isoformat(timespec="seconds")
                                db.mark_sheet_error(uid, str(exc), retry_at)
                        print(f"[sheet-log] batch of {len(consignment_map)} consignments failed: {exc}")
            except Exception as exc:  # noqa: BLE001 - never let the worker thread die
                print(f"[sheet-log] loop error: {exc}")

            for job_number, category in touched:
                try:
                    _maybe_resize_sheet(job_number, category, cfg)
                except Exception as exc:  # noqa: BLE001 - purely cosmetic, never let it break the loop
                    print(f"[sheet-resize] failed for {job_number}: {exc}")
        _stop_event.wait(DRIVE_SYNC_INTERVAL_SEC)


def start_background_sync():
    t = threading.Thread(target=_run_loop, name="drive-sync", daemon=True)
    t.start()
    return t


def stop_background_sync():
    _stop_event.set()
