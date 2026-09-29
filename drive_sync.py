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

New Store Kits add one more step: once a kit is Final Submitted, its
"<Job> - <Kit> - Pack Log" Sheet is written in ONE call carrying the whole
kit (every pack, its items, contributors and photo links) - see _log_kit.
Unlike the consignment log that call is a full overwrite rather than a
merge, so it is always safe to repeat: a retry, or a later rewrite once more
photos have reached Drive, just replaces the same rows with fresher ones.

A submitted kit can also be reopened (db.reopen_kit), changed and submitted
again, any number of times. Nothing here treats that as a special case:
every Final Submit resets the kit's Sheet bookkeeping (db.finalize_kit), so
each submission runs through _log_kit's rules from scratch and ends in a
fresh full overwrite of the CURRENT kit - every pack, item and photo link,
old and new. Photos that already reached Drive are never uploaded again
(their rows stay drive-synced, so pending_drive_uploads_grouped skips them);
only their links are listed again. The one thing to guard against is timing:
a kit can be reopened while its Pack Log call is still in flight - see
_log_kit.
"""
import base64
import html
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

import db
import kits
import labels
from config import (
    CATEGORIES,
    CONSIGNMENT_LOGGING_CATEGORY,
    DRIVE_BATCH_TIMEOUT_SEC,
    DRIVE_FOLDER_OVERRIDES,
    DRIVE_SHEET_BATCH_CAP,
    DRIVE_SYNC_INTERVAL_SEC,
    DRIVE_UPLOAD_BATCH_MAX_BYTES,
    DRIVE_UPLOAD_BATCH_MAX_COUNT,
    KIT_SHEET_FORCE_AFTER_MIN,
    KIT_SHEET_GIVE_UP_HOURS,
    KIT_SHEET_PARTIAL_RETRY_MIN,
    NEW_STORE_KITS_CATEGORY,
    UPLOAD_DIR,
    load_drive_config,
)

_stop_event = threading.Event()

# What the pre-New-Store-Kits Apps Script answers for an action it has never
# heard of (see doPost in DriveUploader.gs) - i.e. the portal was updated but
# the Web App wasn't redeployed yet. Photos still upload fine through that old
# version; only the kit Sheet call fails, so it's worth saying why, loudly.
_UNKNOWN_ACTION_ERROR = "unknown or missing action"


def _backoff_seconds(previous_error_count):
    # 10s, 30s, 1m, 5m, capped at 15m so a prolonged outage doesn't hammer the endpoint.
    steps = [10, 30, 60, 300, 900]
    return steps[min(previous_error_count, len(steps) - 1)]


# --- Talking to the Apps Script Web App ---------------------------------------
#
# A Web App call is really two requests: the POST to script.google.com runs
# doPost, then Google redirects (302) to a one-off script.googleusercontent.com
# /macros/echo?user_content_key=... URL that hands back what doPost returned.
# When Google's side hiccups - the execution failed or timed out inside
# Google, a lock or quota error, a deployment that's still propagating right
# after "New version" - that second request answers 404, or the first one
# answers with an HTML error page instead of our JSON. requests then only
# says "404 Client Error" or "Expecting value: line 1 column 1", which tells
# nobody what went wrong, so every call goes through _call_web_app: it says
# which of the two answered, how long it took and what the page actually
# said, and - for the calls that are safe to repeat - tries once more a few
# seconds later instead of waiting for the next backoff step.

# Statuses from Google that are worth one quick retry (not our own 4xx).
_TRANSIENT_STATUSES = {404, 408, 429, 500, 502, 503, 504}
QUICK_RETRY_DELAY_SEC = 3


class WebAppError(RuntimeError):
    """A Web App call that didn't come back as our JSON. `transient` = Google's
    side, worth retrying."""

    def __init__(self, message, transient):
        super().__init__(message)
        self.transient = transient


def _answered_by(resp):
    """ "script.google.com" (doPost itself) or "script.googleusercontent.com
    (result page)" - never the full echo URL, whose key is just noise."""
    url = str(getattr(resp, "url", "") or "")
    if "googleusercontent.com" in url:
        return "script.googleusercontent.com (the result page after doPost)"
    if "script.google.com" in url:
        return "script.google.com (doPost)"
    return "the Web App"


def _page_text(resp, limit=240):
    """The readable text of an error page, tags stripped - Apps Script's own
    error page says e.g. "Exception: Lock timeout" or "Script function not
    found: doPost" in there."""
    try:
        text = str(getattr(resp, "text", "") or "")
    except Exception:  # noqa: BLE001 - diagnostics must never raise
        return ""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    return text[:limit] + ("..." if len(text) > limit else "")


def _parse_web_app_response(resp, elapsed):
    status = getattr(resp, "status_code", 200) or 200
    where = f"{_answered_by(resp)}, {elapsed:.1f}s"
    if status >= 400:
        said = _page_text(resp)
        raise WebAppError(f"HTTP {status} from {where}" + (f' - page says: "{said}"' if said else ""),
                          transient=status in _TRANSIENT_STATUSES)
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        said = _page_text(resp)
        raise WebAppError(f"answer wasn't JSON (HTTP {status} from {where})"
                          + (f' - page says: "{said}"' if said else " - empty answer"),
                          transient=True) from None


def _call_web_app(cfg, payload, timeout, retry_transient=False):
    """POSTs `payload` and returns the Web App's JSON. retry_transient: one
    more go after QUICK_RETRY_DELAY_SEC when Google's side failed - only for
    actions that are safe to repeat (uploadBatch reuses a file of the same
    name; logKit rewrites the whole Sheet). A timeout is never retried here:
    that already took DRIVE_BATCH_TIMEOUT_SEC."""
    action = payload.get("action", "?")
    attempts = 2 if retry_transient else 1
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        resp = requests.post(cfg["webAppUrl"], json=payload, timeout=timeout)
        try:
            return _parse_web_app_response(resp, time.monotonic() - started)
        except WebAppError as exc:
            if not exc.transient or attempt == attempts:
                raise
            print(f"[drive] {action}: {exc} - trying again in {QUICK_RETRY_DELAY_SEC}s")
            if _stop_event.wait(QUICK_RETRY_DELAY_SEC):
                raise


def _now_local():
    return datetime.now(timezone.utc).astimezone()


def _iso_in(seconds):
    """A local ISO timestamp `seconds` from now, in the same format as
    db.now_iso() - retry_after columns are compared to now_iso() as plain
    strings, so they must be written the same way."""
    return (_now_local() + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _drive_view_url(drive_file_id):
    return f"https://drive.google.com/file/d/{drive_file_id}/view"


def _drive_folder_name(category):
    """The Drive subfolder a category's photos land in. Normally just its
    label (CATEGORIES), but New Store Kit photos deliberately share the job's
    existing "Packing Photos" folder (the user's explicit layout:
    <Job>/Packing Photos/ holds the pictures) while still living in their own
    local folder, uploads/<Job>/new_store_kits/ - so the local path keeps
    using the slug and only the name sent to Drive is overridden here. An old,
    not-yet-redeployed Apps Script handles this fine too: to it this is just
    an ordinary "Packing Photos" upload."""
    return DRIVE_FOLDER_OVERRIDES.get(category, CATEGORIES.get(category, category))


def _row_value(row, key):
    """row[key], or None when the row has no such column. sqlite3.Row raises
    on an unknown key rather than returning None, and a missing kit column
    must never break photo uploads for every other category."""
    return row[key] if key in row.keys() else None


def _kit_note(row):
    """The kit/pack a photo belongs to ("Store 12 · Pack 3"), appended to its
    Drive file description by uploadBatch_ - or None for any non-kit photo.
    Filenames stay employee-timestamp like every other photo (they're the
    upload's idempotency key), and every kit's photos share the one Packing
    Photos folder, so this note is what tells someone browsing Drive which
    kit and pack a picture belongs to."""
    kit_name = _row_value(row, "kit_name")
    if not kit_name:
        return None
    pack_number = _row_value(row, "pack_number")
    if pack_number is None:
        # A kit photo added in the "Kit photos" section - it belongs to no pack.
        return f"{kit_name} · no pack"
    return f"{kit_name} · Pack {pack_number}"


def _submit_count(kit_row):
    """How many times this kit has been Final Submitted - the Pack Log's
    "Times submitted" row. A kit row from before kits could be reopened has no
    submit_count (or 0), yet logKit is only ever sent for a submitted kit, so
    it has been submitted at least once: that reads as 1, never 0."""
    return _row_value(kit_row, "submit_count") or 1


def _submission_key(kit_row):
    """Which submission of a kit this row describes - see the re-reads in
    _log_kit. finalized_at alone isn't enough: it's None while the kit is
    open, but only to the second otherwise, and a quick reopen + re-submit
    can land inside the same second; submit_count goes up on every one."""
    return kit_row["finalized_at"], _row_value(kit_row, "submit_count")


def check_job_in_drive(job_number, cfg, timeout=10):
    """Synchronous (not part of the background loop) - called from app.py's
    /start so a resumed "keep logs" job can rehydrate before the user starts
    adding photos. Returns {"found": False} or {"found": True, "rows": [...]}
    where each row mirrors one line of that job's Photo Log sheet."""
    payload = {"secret": cfg["sharedSecret"], "action": "checkJob", "jobNumber": job_number}
    result = _call_web_app(cfg, payload, timeout)
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
        file_entry = {
            "fileName": row["filename"],
            "employeeName": row["employee_name"],
            "uploadedAt": row["uploaded_at"],
            "contentB64": b64,
        }
        note = _kit_note(row)
        if note:
            file_entry["note"] = note
        files.append((row, file_entry))
    if not files:
        return

    payload = {
        "secret": cfg["sharedSecret"],
        "action": "uploadBatch",
        "jobNumber": job_number,
        "category": _drive_folder_name(category),
        "files": [f for _, f in files],
    }

    result = _call_web_app(cfg, payload, DRIVE_BATCH_TIMEOUT_SEC, retry_transient=True)
    if not result.get("ok"):
        # Whole-batch failure (e.g. Apps Script itself errored before getting
        # to per-file results) - every row in it gets the same backoff. Safe
        # to retry blindly next tick: the upload endpoint is idempotent by
        # filename, so any file that DID succeed just gets recognized and
        # reused rather than duplicated.
        raise RuntimeError(result.get("error", "unknown error from Apps Script"))

    results_by_name = {r.get("fileName"): r for r in result.get("results", [])}
    kit_sessions_synced = set()
    for row, f in files:
        file_result = results_by_name.get(f["fileName"])
        if file_result and file_result.get("ok"):
            db.mark_drive_synced(row["id"], file_result.get("fileId", ""))
            error_counts.pop(row["id"], None)
            if _row_value(row, "kit_name"):
                kit_sessions_synced.add(row["session_id"])
        else:
            error_text = (file_result or {}).get("error", "no result returned for this file")
            _mark_row_error(row, error_text, error_counts)
    _requeue_finished_kit_sheets(kit_sessions_synced)


def _requeue_finished_kit_sheets(session_ids):
    """A kit photo that reaches Drive AFTER its kit's Pack Log was already
    written as final can only mean _log_kit gave up waiting for it
    (KIT_SHEET_GIVE_UP_HOURS) - e.g. the portal machine was off, or Drive
    unreachable, for over a day after Final Submit, and the backlog is still
    draining a batch at a time when the 24h mark passes. Left alone, that
    photo would be missing from the Sheet for good AND its local copy would
    never be cleaned up (local_cleanup waits for the upload's sheet_status to
    be 'synced', which only a Sheet write sets). So the kit's Sheet goes back
    to 'pending', keeping the count its last write included: _log_kit then
    sees the synced count has moved and rewrites the Sheet - this same tick,
    since the kit step runs after the photo step - either complete (the
    give-up note cleared) or as a fresh final write with an updated note.

    This only ever re-queues the SHEET; the kit itself stays submitted. A
    kit someone has reopened in the meantime (finalized_at cleared) is left
    alone: its Sheet is rewritten, with this photo's link, when it's
    submitted again.

    Must never raise: every photo in the batch is already marked synced by
    now, and an exception escaping _sync_batch would make the loop back off
    the whole batch as if the upload itself had failed."""
    for session_id in session_ids:
        try:
            kit = db.get_kit(session_id)
            if not kit or not kit["finalized_at"] or kit["sheet_status"] != "synced":
                continue  # the normal case: its Sheet hasn't been finished yet
            db.mark_kit_sheet_written(session_id, [], kit["sheet_synced_count"], False)
            print(f"[kit-sheet] a photo for {kit['job_number']} / {kit['kit_name']} reached Drive "
                  "after its Pack Log was finished - rewriting it")
        except Exception as exc:  # noqa: BLE001 - see docstring
            print(f"[kit-sheet] couldn't requeue the Pack Log for session {session_id}: {exc}")


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
            "itemIds": db.item_id_labels(consignment["item_ids"]),
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

    result = _call_web_app(cfg, payload, DRIVE_BATCH_TIMEOUT_SEC)
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


def _kit_payload(kit_row, cfg):
    """The logKit request for one finalized kit, plus the ids of exactly the
    uploads whose Drive links it carries. Always the COMPLETE current state
    (every pack, every item, every synced link) - logKit_ overwrites the
    whole Sheet with it, so a repeat or a later rewrite can never leave
    stale or duplicate rows behind.

    Items come from every pack (the kit is finalized, so every pack is saved
    by now) and go through the same kits.pack_lines/series_report the upload
    page uses, so the Sheet reads exactly like the screen did at Final
    Submit - except the report's `missing` list is the full one (the
    on-screen text caps it; a Sheet cell doesn't need to). Dates are never
    null: a pack nobody ever edited (no items/photos, kept because it isn't
    the last one) falls back to when it was created, so logKit_ never has
    to guess at a blank.

    For a kit that was reopened and submitted again, finalizedBy/finalizedAt
    are the LATEST submission (Final Submit rewrites both) and submitCount
    says how many there have been; the photo links cover every submission,
    since all of the kit's photos stay 'finalized' once submitted."""
    session_id = kit_row["session_id"]

    items_by_pack = {}
    all_items = db.items_for_kit(session_id)
    for item in all_items:
        items_by_pack.setdefault(item["pack_id"], []).append(item)

    links_by_pack = {}
    for row in db.synced_kit_uploads(session_id):
        if row["drive_file_id"]:
            links_by_pack.setdefault(row["pack_id"], []).append(row)

    packs_payload = []
    sent_upload_ids = []
    for pack in db.packs_for_kit(session_id):
        pack_uploads = links_by_pack.get(pack["id"], [])
        sent_upload_ids.extend(row["id"] for row in pack_uploads)
        first_edit = pack["first_edit_at"] or pack["created_at"]
        packs_payload.append({
            "packNumber": pack["pack_number"],
            "firstEdit": first_edit,
            "lastEdit": pack["last_edit_at"] or first_edit,
            "items": kits.pack_lines(items_by_pack.get(pack["id"], [])),
            "contributors": db.split_list(pack["contributors"]),
            "photoLinks": [_drive_view_url(row["drive_file_id"]) for row in pack_uploads],
        })

    # Kit photos that belong to no pack: one extra "No pack" row in the Sheet
    # (see logKit_), so every photo of the kit is linked from its Pack Log.
    general_rows = links_by_pack.get(None, [])
    sent_upload_ids.extend(row["id"] for row in general_rows)
    general_payload = None
    if general_rows:
        uploaders = []
        for row in general_rows:
            if row["employee_name"] not in uploaders:
                uploaders.append(row["employee_name"])
        general_payload = {
            "firstEdit": min(row["uploaded_at"] for row in general_rows),
            "lastEdit": max(row["uploaded_at"] for row in general_rows),
            "contributors": uploaders,
            "photoLinks": [_drive_view_url(row["drive_file_id"]) for row in general_rows],
        }

    previous_job = _previous_sheet_job_number(kit_row)
    move_ids = []
    if previous_job:
        move_ids = [row["drive_file_id"] for rows in links_by_pack.values() for row in rows]

    report_payload = [
        {
            "series": entry["series"],
            "lastNumber": entry["lastNumber"],
            "missing": list(entry["missing"]),
            "text": entry["text"],
        }
        for entry in kits.series_report(all_items)
    ]

    payload = {
        "secret": cfg["sharedSecret"],
        "action": "logKit",
        "labelsPdf": _labels_pdf_payload(kit_row, items_by_pack),
        "jobNumber": kit_row["job_number"],
        "kitName": kit_row["kit_name"],
        # Renamed since its Sheet was last written: logKit_ renames that
        # "<Job> - <old name> - Pack Log" file instead of starting a second one.
        "previousKitName": _previous_sheet_kit_name(kit_row),
        # Moved to another job number since: logKit_ moves that Sheet from
        # "<old job>/", and the kit's photos in Drive (moveFileIds) from
        # "<old job>/Packing Photos/", into this job's folders - same files,
        # same ids, so every link stays good.
        "previousJobNumber": previous_job,
        "moveFileIds": move_ids,
        "photoFolder": _drive_folder_name(NEW_STORE_KITS_CATEGORY),
        "finalizedBy": kit_row["finalized_by"] or "",
        "finalizedAt": kit_row["finalized_at"],
        "submitCount": _submit_count(kit_row),
        "packs": packs_payload,
        "generalPhotos": general_payload,
        "report": report_payload,
    }
    return payload, sent_upload_ids


def labels_pdf_name(job_number, kit_name):
    return f"{job_number} - {kit_name} - Labels.pdf"


def _labels_pdf_payload(kit_row, items_by_pack):
    """The kit's labels as a PDF for logKit to put in "<Job>/" - or None when
    Drive already has the PDF for THIS submission. Only ever sent with the
    Pack Log write, which only happens after Final Submit, so a kit being
    packed never has a PDF that's out of date. A resubmission (reopened,
    changed, submitted again) sends a new one, and previousFileId tells
    logKit which file to move to the trash - found by id, so a renamed
    kit's old "<old name> - Labels.pdf" goes too.

    The packs are the ones Print would label: every pack with items or
    photos, in pack order. With none, contentB64 is null: the old PDF is
    still trashed and nothing new is made. If the PDF can't be drawn at all
    (no Pillow, a label stock too small), the Sheet is written anyway and
    this is logged - labels never hold up the Pack Log."""
    session_id = kit_row["session_id"]
    submit_count = _submit_count(kit_row)
    if _row_value(kit_row, "labels_submit_count") == submit_count:
        return None
    photo_packs = {u["pack_id"] for u in db.uploads_for_session(session_id) if u["pack_id"] is not None}
    packs = [
        (pack["pack_number"], items_by_pack.get(pack["id"], []))
        for pack in db.packs_for_kit(session_id)
        if items_by_pack.get(pack["id"]) or pack["id"] in photo_packs
    ]
    content = None
    if packs:
        try:
            content = base64.b64encode(labels.kit_labels_pdf(kit_row["kit_name"], packs)).decode("ascii")
        except Exception as exc:  # noqa: BLE001 - labels must never block the Sheet
            print(f"[kit-labels] {kit_row['job_number']} / {kit_row['kit_name']}: couldn't draw the labels PDF "
                  f"({exc}) - writing the Pack Log without it")
            return None
    return {
        "fileName": labels_pdf_name(kit_row["job_number"], kit_row["kit_name"]),
        "contentB64": content,
        "packCount": len(packs),
        "submitCount": submit_count,
        "previousFileId": _row_value(kit_row, "labels_file_id") or None,
    }


def _written_job_number(payload, result):
    """The job folder the Sheet is in after this write: the new one - unless
    the kit had moved job and the Apps Script is too old to know (no
    jobMoved in its answer), in which case the Sheet is still in the old job
    folder's keeping as far as we know: keep the old job so the next write
    (after a redeploy) moves it."""
    if payload.get("previousJobNumber") and not result.get("jobMoved"):
        print(f"[kit-sheet] {payload['jobNumber']} / {payload['kitName']}: Apps Script didn't move the kit's Drive "
              f"files from {payload['previousJobNumber']} - redeploy DriveUploader.gs as a New version")
        return payload["previousJobNumber"]
    return payload["jobNumber"]


def _record_labels_result(kit_row, payload, result):
    """Remembers the labels PDF logKit just made (or that there's none). An
    Apps Script from before labels answers ok without labelsFileId - say so
    once per write, and leave the bookkeeping so the next write tries again."""
    sent = payload.get("labelsPdf")
    if not sent:
        return
    if "labelsFileId" not in result:
        print(f"[kit-labels] {kit_row['job_number']} / {kit_row['kit_name']}: Apps Script didn't save the labels "
              "PDF - redeploy DriveUploader.gs as a New version")
        return
    db.record_kit_labels(kit_row["session_id"], result.get("labelsFileId") or "", sent["submitCount"])


def _previous_sheet_job_number(kit_row):
    """The job number its Pack Log was last written under, if the kit has
    moved to another job since (db.edit_kit) - else None."""
    previous = _row_value(kit_row, "sheet_job_number")
    if not previous or not _row_value(kit_row, "sheet_kit_name"):
        return None
    return previous if previous != kit_row["job_number"] else None


def _previous_sheet_kit_name(kit_row):
    """The kit name its Pack Log was last written under, if the kit has been
    renamed since (db.rename_kit) - else None. Read defensively: a row from
    before the sheet_kit_name column existed simply has no previous name."""
    try:
        previous = kit_row["sheet_kit_name"]
    except (IndexError, KeyError):
        return None
    return previous if previous and previous != kit_row["kit_name"] else None


def _log_kit(kit_row, cfg):
    """Writes (or rewrites) one finalized kit's Pack Log Sheet, deciding
    first whether now is the right moment. Returns True if a Sheet write
    actually happened, False if this tick deliberately skipped it; raises on
    a failed call (the loop owns the error backoff - see
    _mark_kit_sheet_error).

    The Sheet's Photo Links column can only list photos that have reached
    Drive, and a kit's photos sync over the next few ticks after Final
    Submit - so the ideal is ONE write, once every finalized photo is in.
    But one stubborn photo (a corrupt file, a long Drive outage) mustn't
    leave the office with no Sheet at all, hence the staged rules:
      - all photos synced -> write it, done.
      - not all synced yet, submitted < KIT_SHEET_FORCE_AFTER_MIN ago ->
        wait (no call; pending_kit_sheets brings it back next tick).
      - past that window -> write a partial Sheet, then recheck every
        KIT_SHEET_PARTIAL_RETRY_MIN and rewrite only when the synced count
        has actually moved (a rewrite of identical content is a wasted
        Apps Script execution).
      - past KIT_SHEET_GIVE_UP_HOURS -> this write is the last one: it's
        bookkept as complete, with sheet_error saying how many photos never
        made it, so the kit stops being retried forever (unless one of those
        photos does reach Drive later - see _requeue_finished_kit_sheets).
    Only the uploads whose links were actually sent get marked Sheet-synced
    (that's what lets local_cleanup delete their local copies) - a photo
    that synced after this payload was built waits for the next write.

    Every rule above is per SUBMISSION: each Final Submit resets the
    bookkeeping (db.finalize_kit) and restarts the clock (finalized_at), so a
    reopened and re-submitted kit is always rewritten - straight away when it
    gained no photos, else once its new ones have synced, with the same
    partial/give-up fallbacks. What those rules can't see is a reopen landing
    while this runs, so the kit is re-read twice:
      - first, since the loop's row can be minutes old (each kit ahead of it
        in the tick makes its own call): a kit reopened since must not be
        written mid-edit as "Packed completely", nor judged by an older
        submission's bookkeeping;
      - again after the call, which can itself take minutes: if the kit was
        reopened meanwhile, this write's bookkeeping belongs to a submission
        that no longer exists - recording it would wipe the reset of a
        re-submission made in that time, and its changes would never reach
        the Sheet. So it's dropped; the next submission rewrites the Sheet."""
    session_id = kit_row["session_id"]
    kit_row = db.get_kit(session_id)
    if kit_row is None or not kit_row["finalized_at"]:
        return False  # deleted, or reopened since the loop listed it
    total, synced = db.kit_upload_sync_counts(session_id)
    complete = synced == total

    age = _now_local() - datetime.fromisoformat(kit_row["finalized_at"])
    give_up = age >= timedelta(hours=KIT_SHEET_GIVE_UP_HOURS)
    retry_later = _iso_in(KIT_SHEET_PARTIAL_RETRY_MIN * 60)

    if not complete and not give_up:
        if age < timedelta(minutes=KIT_SHEET_FORCE_AFTER_MIN):
            return False
        if kit_row["sheet_synced_at"] and synced == kit_row["sheet_synced_count"]:
            # The partial Sheet already written is still exactly current -
            # just come back later rather than rewriting the same content.
            db.mark_kit_sheet_written(
                session_id, [], synced, False, retry_after_iso=retry_later
            )
            return False

    payload, sent_upload_ids = _kit_payload(kit_row, cfg)

    result = _call_web_app(cfg, payload, DRIVE_BATCH_TIMEOUT_SEC, retry_transient=True)
    if not result.get("ok"):
        # Nothing to reconcile per-pack: logKit_ rewrites the whole Sheet in
        # one go, so a retry just sends the full snapshot again.
        raise RuntimeError(result.get("error", "unknown error from Apps Script"))

    latest = db.get_kit(session_id)
    if latest is not None:
        _record_labels_result(kit_row, payload, result)
    if latest is None or _submission_key(latest) != _submission_key(kit_row):
        print(f"[kit-sheet] {kit_row['job_number']} / {kit_row['kit_name']} was reopened while its "
              "Pack Log was being written - leaving the Sheet to its next submission")
        # The Sheet file IS now named after this payload's kit name, whatever
        # happens to the bookkeeping - remember it, or a rename made in the
        # meantime would point the next write at a file name that's gone.
        if latest is not None:
            db.record_kit_sheet_name(session_id, payload["kitName"], _written_job_number(payload, result))
        return True

    final = complete or give_up
    note = None
    if final and not complete:
        note = f"{total - synced} photo(s) never reached Drive"
        print(f"[kit-sheet] giving up waiting on {kit_row['job_number']} / {kit_row['kit_name']}: {note}")
    db.mark_kit_sheet_written(
        session_id,
        sent_upload_ids,
        synced,
        final,
        retry_after_iso=None if final else retry_later,
        note=note,
        sheet_kit_name=payload["kitName"],
        sheet_job_number=_written_job_number(payload, result),
    )
    return True


def _mark_kit_sheet_error(kit_row, error_text, kit_error_counts):
    """Backs one kit's Sheet write off after a failed call - per kit (the
    write is all-or-nothing for the whole kit, so there's nothing finer to
    track), using the same backoff ladder as photo uploads. The one failure
    worth calling out by name is an out-of-date Apps Script: the portal
    knows the logKit action but the live Web App doesn't until it's
    redeployed, and nothing else would ever tell anyone - photos keep
    syncing fine through the old version, so the only symptom is a Sheet
    that silently never appears."""
    session_id = kit_row["session_id"]
    n = kit_error_counts.get(session_id, 0) + 1
    kit_error_counts[session_id] = n
    db.mark_kit_sheet_error(session_id, error_text, _iso_in(_backoff_seconds(n - 1)))
    print(f"[kit-sheet] {kit_row['job_number']} / {kit_row['kit_name']} failed: {error_text}")
    if _UNKNOWN_ACTION_ERROR in (error_text or ""):
        print("[kit-sheet] Apps Script is out of date - redeploy DriveUploader.gs as a New version")


def _resize_sheet_columns(job_number, cfg, timeout=15):
    """One-off formatting pass, not part of the regular sync - see
    _maybe_resize_sheet. Tiny payload/response (no file bytes involved), so a
    short timeout is plenty."""
    payload = {"secret": cfg["sharedSecret"], "action": "resizeSheetColumns", "jobNumber": job_number}
    result = _call_web_app(cfg, payload, timeout)
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
    kit_sheet_error_counts = {}  # session_id -> consecutive failed Pack Log writes
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

            # New Store Kit Pack Logs - after the photo step on purpose, so a
            # kit whose last photos just synced this tick gets its complete
            # Sheet this same tick instead of one tick later.
            try:
                for kit_row in db.pending_kit_sheets():
                    try:
                        if _log_kit(kit_row, cfg):
                            kit_sheet_error_counts.pop(kit_row["session_id"], None)
                    except Exception as exc:  # noqa: BLE001 - log and keep the loop alive
                        _mark_kit_sheet_error(kit_row, str(exc), kit_sheet_error_counts)
            except Exception as exc:  # noqa: BLE001 - never let the worker thread die
                print(f"[kit-sheet] loop error: {exc}")

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
