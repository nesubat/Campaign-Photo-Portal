"""
Campaign Photo Portal - Flask app.

Flow:
  1. GET  /                 -> job number + "who are you" form
  2. POST /start            -> creates/finds the job folder, starts a session, redirects in
  3. GET  /session/<id>     -> camera-capture upload page + live gallery for that job
  4. POST /api/upload       -> one photo lands here the instant it's picked; saved to disk
                               immediately (fast, local)
  5. POST /api/finalize     -> marks the session's photos as finalized (the "Submit" button);
                               only finalized photos are picked up for background Drive sync
  6. GET  /gallery/<job>    -> read-only view of everything uploaded for a job (for supervisors)

Run for quick local testing: py app.py
Run for real use (all-day, several phones):  py serve.py   <- use this one day-to-day

Both bind 0.0.0.0:5000 so phones on the hotspot/LAN can reach it.
"""
import io
import re
import uuid

from flask import Flask, abort, jsonify, redirect, render_template, request, send_from_directory
from PIL import Image, ImageOps

import db
import drive_sync
import local_cleanup
from config import (
    CATEGORIES,
    CONSIGNMENT_LOGGING_CATEGORY,
    HOST,
    PORT,
    THUMB_MAX_PX,
    UPLOAD_DIR,
    load_drive_config,
    load_employees,
    save_employees,
)

app = Flask(__name__)

_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._ -]+")
ALLOWED_EXT = {"jpg", "jpeg", "png", "webp", "heic", "heif"}


def sanitize_for_filename(raw: str) -> str:
    cleaned = _SAFE_CHARS.sub("_", raw.strip())
    cleaned = cleaned.strip(". ")  # no leading/trailing dots or spaces (Windows folder rules)
    return cleaned


def job_dir(job_number, category):
    return UPLOAD_DIR / job_number / category


def thumb_dir(job_number, category):
    d = job_dir(job_number, category) / "thumbs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _render_index(**extra):
    return render_template(
        "index.html", employees=load_employees(), categories=CATEGORIES,
        consignment_logging_category=CONSIGNMENT_LOGGING_CATEGORY, **extra
    )


@app.route("/")
def index():
    return _render_index(
        submitted=request.args.get("submitted", type=int),
        submitted_job=request.args.get("job"),
    )


@app.route("/api/employees/remove", methods=["POST"])
def api_employees_remove():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify(ok=False, error="Missing name."), 400

    employees = load_employees()
    if name in employees:
        employees = [e for e in employees if e != name]
        save_employees(employees)
    return jsonify(ok=True, employees=employees)


@app.route("/start", methods=["POST"])
def start():
    raw_job = request.form.get("job_number", "")
    job_number = sanitize_for_filename(raw_job)
    if not job_number:
        return _render_index(error="Enter a valid Job Number.")

    employee_name = (request.form.get("employee_name") or "").strip()
    if employee_name == "__other__":
        employee_name = (request.form.get("employee_name_other") or "").strip()
        if employee_name:
            employees = load_employees()
            if employee_name not in employees:
                employees.append(employee_name)
                save_employees(employees)
    if not employee_name:
        return _render_index(error="Enter or select your name.")

    category = (request.form.get("category") or "").strip()
    if category not in CATEGORIES:
        return _render_index(error="Select Packing Photos or Dispatch Photos.")

    job_dir(job_number, category).mkdir(parents=True, exist_ok=True)
    db.ensure_job(job_number)

    keep_logs = category == CONSIGNMENT_LOGGING_CATEGORY and request.form.get("keep_logs") == "on"

    resumed_count = 0
    if keep_logs and not db.has_consignments(job_number, category):
        resumed_count = _rehydrate_from_drive(job_number, category)

    session_id = uuid.uuid4().hex
    db.create_session(session_id, job_number, employee_name, category, keep_logs=keep_logs)

    suffix = f"?resumed={resumed_count}" if resumed_count else ""
    return redirect(f"/session/{session_id}{suffix}")


def _rehydrate_from_drive(job_number, category):
    """This job's local data may have been cleaned up (or may never have
    existed on this machine) since it was last worked on - see if Drive's
    copy of its Photo Log sheet knows about consignments we don't, and
    restore just enough locally (not the photos themselves) to resume it
    correctly: existing-consignment detection, item IDs, and the "N photos
    already logged" count. Best-effort - Drive being slow/misconfigured must
    never block someone from starting a session."""
    cfg = load_drive_config()
    if not cfg:
        return 0
    try:
        result = drive_sync.check_job_in_drive(job_number, cfg)
    except Exception as exc:  # noqa: BLE001 - never block starting a session on this
        print(f"[start] Drive job-check failed for {job_number}: {exc}")
        return 0
    if not result.get("found"):
        return 0

    count = 0
    for row in result.get("rows", []):
        try:
            db.rehydrate_consignment(
                job_number, category, row["keyValue"], row.get("itemIds", []),
                row.get("contributors", []), len(row.get("photoLinks", [])),
                created_at=row.get("firstLogged"), updated_at=row.get("lastUpdated"),
            )
            count += 1
        except Exception as exc:  # noqa: BLE001 - one bad row shouldn't block the rest
            print(f"[start] Could not import a consignment row for {job_number}: {exc}")
    return count


def _group_photos_by_consignment(photos):
    """Groups a session's photos into per-consignment sections. Within each
    section, newest photo first (matches how the client prepends new
    uploads); sections themselves ordered by whichever was most recently
    active first, so scanning a new consignment naturally pushes earlier
    ones down - matching the physical "pack one box, seal it, move to the
    next" workflow."""
    groups = {}
    order = []
    for p in photos:
        cid = p["consignment_id"]
        if cid not in groups:
            groups[cid] = {
                "consignment_id": cid,
                "consignment_value": p["consignment_value"],
                "consignment_item_ids": p["consignment_item_ids"],
                "photos": [],
            }
            order.append(cid)
        groups[cid]["photos"].append(p)
    sections = [groups[cid] for cid in order]
    for section in sections:
        section["photos"].reverse()
    sections.sort(key=lambda s: s["photos"][0]["uploaded_at"], reverse=True)
    return sections


def _section_json(section):
    return {
        "consignmentId": section["consignment_id"],
        "keyValue": section["consignment_value"],
        "itemIds": db.group_item_ids(section["consignment_item_ids"] or ""),
        "photos": [
            {
                "id": p["id"],
                "thumbUrl": f"/media/{p['job_number']}/{p['category']}/thumbs/{p['thumb_filename']}",
                "fullUrl": f"/media/{p['job_number']}/{p['category']}/{p['filename']}",
            }
            for p in section["photos"]
        ],
    }


@app.route("/session/<session_id>")
def session_page(session_id):
    sess = db.get_session(session_id)
    if not sess:
        abort(404)
    photos = db.uploads_for_session(session_id)
    consignment_logging = sess["category"] == CONSIGNMENT_LOGGING_CATEGORY and bool(sess["keep_logs"])
    sections = _group_photos_by_consignment(photos) if consignment_logging else []
    consignment_values = (
        db.consignment_values_for_job(sess["job_number"], sess["category"])
        if consignment_logging else []
    )
    return render_template(
        "upload.html",
        session=sess,
        category_label=CATEGORIES.get(sess["category"], sess["category"]),
        photos=photos,
        finalized=bool(sess["finalized_at"]),
        consignment_logging=consignment_logging,
        sections_json=[_section_json(s) for s in sections],
        consignment_values=consignment_values,
        resumed_count=request.args.get("resumed", type=int),
    )


def _consignment_json(row, existing):
    return dict(
        ok=True,
        existing=existing,
        consignment_id=row["id"],
        key_type=row["key_type"],
        key_value=row["key_value"],
        item_ids=db.group_item_ids(row["item_ids"]),
        contributors=db.split_list(row["contributors"]),
        photo_count=row["photo_count"],
    )


@app.route("/api/consignment/resolve", methods=["POST"])
def api_consignment_resolve():
    data = request.get_json(silent=True) or {}
    sess = db.get_session(data.get("session_id", ""))
    if not sess:
        return jsonify(ok=False, error="Session not found - reopen the job."), 404
    if sess["finalized_at"]:
        return jsonify(ok=False, error="This batch was already submitted."), 400
    if sess["category"] != CONSIGNMENT_LOGGING_CATEGORY or not sess["keep_logs"]:
        return jsonify(ok=False, error="This session isn't keeping logs."), 400

    key_type = data.get("key_type")
    if key_type not in ("consignment", "store"):
        return jsonify(ok=False, error="Invalid type."), 400
    raw_value = (data.get("key_value") or "").strip()
    if not raw_value:
        return jsonify(ok=False, error="Enter a consignment number or store name."), 400

    job_number = sess["job_number"]
    category = sess["category"]
    key_norm = raw_value.casefold()

    row, existing = db.find_or_create_consignment(
        job_number, category, key_type, raw_value, key_norm, sess["employee_name"]
    )
    return jsonify(**_consignment_json(row, existing=existing))


@app.route("/api/consignment/item", methods=["POST"])
def api_consignment_item():
    data = request.get_json(silent=True) or {}
    sess = db.get_session(data.get("session_id", ""))
    if not sess:
        return jsonify(ok=False, error="Session not found."), 404

    row = db.get_consignment(data.get("consignment_id"))
    if not row or row["job_number"] != sess["job_number"] or row["category"] != sess["category"]:
        return jsonify(ok=False, error="Consignment not found."), 404

    item_id = (data.get("item_id") or "").strip()
    if not item_id:
        return jsonify(ok=False, error="Enter an Item ID."), 400

    item_ids = db.add_consignment_item_id(row["id"], item_id)
    return jsonify(ok=True, item_ids=item_ids)


@app.route("/api/consignment/item/decrement", methods=["POST"])
def api_consignment_item_decrement():
    data = request.get_json(silent=True) or {}
    sess = db.get_session(data.get("session_id", ""))
    if not sess:
        return jsonify(ok=False, error="Session not found."), 404

    row = db.get_consignment(data.get("consignment_id"))
    if not row or row["job_number"] != sess["job_number"] or row["category"] != sess["category"]:
        return jsonify(ok=False, error="Consignment not found."), 404

    item_id = (data.get("item_id") or "").strip()
    if not item_id:
        return jsonify(ok=False, error="Missing Item ID."), 400

    item_ids = db.decrement_consignment_item_id(row["id"], item_id)
    return jsonify(ok=True, item_ids=item_ids)


@app.route("/api/upload", methods=["POST"])
def api_upload():
    session_id = request.form.get("session_id", "")
    sess = db.get_session(session_id)
    if not sess:
        return jsonify(ok=False, error="Session not found - reopen the job."), 404
    if sess["finalized_at"]:
        return jsonify(ok=False, error="This batch was already submitted."), 400

    job_number = sess["job_number"]
    category = sess["category"]

    consignment = None
    if category == CONSIGNMENT_LOGGING_CATEGORY and sess["keep_logs"]:
        consignment = db.get_consignment(request.form.get("consignment_id", type=int))
        if not consignment or consignment["job_number"] != job_number or consignment["category"] != category:
            return jsonify(ok=False, error="Scan a consignment number (or enter a store name) first."), 400

    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify(ok=False, error="No file received."), 400

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else "jpg"
    if ext not in ALLOWED_EXT:
        ext = "jpg"

    # Filename is always employee-timestamp, regardless of whether this photo
    # is tagged to a consignment - logging is metadata (see the uploads table's
    # consignment_id), never encoded into the filename itself.
    unique_name = f"{sanitize_for_filename(sess['employee_name'])}-{db.now_filename_stamp()}.{ext}"

    raw_bytes = file.read()
    job_dir(job_number, category).mkdir(parents=True, exist_ok=True)
    full_path = job_dir(job_number, category) / unique_name
    with open(full_path, "wb") as f:
        f.write(raw_bytes)

    thumb_name = unique_name
    try:
        img = Image.open(io.BytesIO(raw_bytes))
        img = ImageOps.exif_transpose(img)  # fix sideways phone photos
        img.thumbnail((THUMB_MAX_PX, THUMB_MAX_PX))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        thumb_path = thumb_dir(job_number, category) / (unique_name.rsplit(".", 1)[0] + ".jpg")
        thumb_name = thumb_path.name
        img.save(thumb_path, "JPEG", quality=80)
    except Exception as exc:  # noqa: BLE001 - a bad/odd image shouldn't block the upload
        print(f"[upload] thumbnail failed for {unique_name}: {exc}")
        thumb_name = unique_name  # gallery will fall back to the full image

    upload_id = db.add_upload(
        session_id, job_number, sess["employee_name"], category, unique_name, thumb_name,
        consignment_id=consignment["id"] if consignment else None,
    )
    if consignment:
        db.increment_photo_count(consignment["id"])

    return jsonify(
        ok=True,
        id=upload_id,
        thumbUrl=f"/media/{job_number}/{category}/thumbs/{thumb_name}",
        fullUrl=f"/media/{job_number}/{category}/{unique_name}",
    )


@app.route("/api/delete", methods=["POST"])
def api_delete():
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "")
    upload_id = data.get("upload_id")

    sess = db.get_session(session_id)
    if not sess:
        return jsonify(ok=False, error="Session not found."), 404
    if sess["finalized_at"]:
        return jsonify(ok=False, error="This batch was already submitted."), 400

    row = db.get_upload(upload_id)
    if not row or row["session_id"] != session_id:
        return jsonify(ok=False, error="Photo not found."), 404

    full_path = job_dir(row["job_number"], row["category"]) / row["filename"]
    thumb_path = thumb_dir(row["job_number"], row["category"]) / row["thumb_filename"]
    full_path.unlink(missing_ok=True)
    thumb_path.unlink(missing_ok=True)
    if row["consignment_id"]:
        db.decrement_photo_count(row["consignment_id"])
    db.delete_upload(upload_id)

    return jsonify(ok=True)


@app.route("/api/finalize", methods=["POST"])
def api_finalize():
    session_id = (request.get_json(silent=True) or {}).get("session_id", "")
    sess = db.get_session(session_id)
    if not sess:
        return jsonify(ok=False, error="Session not found."), 404
    count = db.finalize_session(session_id)
    return jsonify(ok=True, count=count)


@app.route("/gallery/<job_number>")
def gallery(job_number):
    job_number = sanitize_for_filename(job_number)
    photos = db.uploads_for_job(job_number)
    return render_template(
        "gallery.html", job_number=job_number, photos=photos, categories=CATEGORIES
    )


def _drive_view_url(drive_file_id):
    return f"https://drive.google.com/file/d/{drive_file_id}/view"


@app.route("/media/<job_number>/<category>/thumbs/<filename>")
def media_thumb(job_number, category, filename):
    if category not in CATEGORIES:
        abort(404)
    try:
        return send_from_directory(thumb_dir(job_number, category), filename)
    except FileNotFoundError:
        row = db.get_upload_by_thumb_filename(job_number, category, filename)
        if row and row["drive_file_id"]:
            return redirect(_drive_view_url(row["drive_file_id"]))
        abort(404)


@app.route("/media/<job_number>/<category>/<filename>")
def media_full(job_number, category, filename):
    if category not in CATEGORIES:
        abort(404)
    try:
        return send_from_directory(job_dir(job_number, category), filename)
    except FileNotFoundError:
        row = db.get_upload_by_filename(job_number, category, filename)
        if row and row["drive_file_id"]:
            return redirect(_drive_view_url(row["drive_file_id"]))
        abort(404)


if __name__ == "__main__":
    db.init_db()
    if load_drive_config():
        print("[startup] Drive sync configured - background relay starting.")
    else:
        print(
            "[startup] Drive sync NOT configured yet (.env missing or incomplete - see "
            ".env.example). Photos will save locally only until you deploy "
            "apps-script/DriveUploader.gs and fill that file in."
        )
    drive_sync.start_background_sync()
    local_cleanup.start_background_cleanup()
    app.run(host=HOST, port=PORT, threaded=True)
