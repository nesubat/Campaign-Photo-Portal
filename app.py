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
from config import CATEGORIES, HOST, PORT, THUMB_MAX_PX, UPLOAD_DIR, load_drive_config, load_employees

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


@app.route("/")
def index():
    submitted = request.args.get("submitted", type=int)
    return render_template(
        "index.html", employees=load_employees(), categories=CATEGORIES, submitted=submitted
    )


@app.route("/start", methods=["POST"])
def start():
    raw_job = request.form.get("job_number", "")
    job_number = sanitize_for_filename(raw_job)
    if not job_number:
        return render_template(
            "index.html", employees=load_employees(), categories=CATEGORIES,
            error="Enter a valid Job Number.",
        )

    employee_name = (request.form.get("employee_name") or "").strip()
    if employee_name == "__other__":
        employee_name = (request.form.get("employee_name_other") or "").strip()
    if not employee_name:
        return render_template(
            "index.html", employees=load_employees(), categories=CATEGORIES,
            error="Enter or select your name.",
        )

    category = (request.form.get("category") or "").strip()
    if category not in CATEGORIES:
        return render_template(
            "index.html", employees=load_employees(), categories=CATEGORIES,
            error="Select Packing Photos or Dispatch Photos.",
        )

    job_dir(job_number, category).mkdir(parents=True, exist_ok=True)
    db.ensure_job(job_number)

    session_id = uuid.uuid4().hex
    db.create_session(session_id, job_number, employee_name, category)

    return redirect(f"/session/{session_id}")


@app.route("/session/<session_id>")
def session_page(session_id):
    sess = db.get_session(session_id)
    if not sess:
        abort(404)
    photos = db.uploads_for_job(sess["job_number"])
    return render_template(
        "upload.html",
        session=sess,
        category_label=CATEGORIES.get(sess["category"], sess["category"]),
        photos=photos,
        finalized=bool(sess["finalized_at"]),
    )


@app.route("/api/upload", methods=["POST"])
def api_upload():
    session_id = request.form.get("session_id", "")
    sess = db.get_session(session_id)
    if not sess:
        return jsonify(ok=False, error="Session not found - reopen the job."), 404
    if sess["finalized_at"]:
        return jsonify(ok=False, error="This batch was already submitted."), 400

    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify(ok=False, error="No file received."), 400

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else "jpg"
    if ext not in ALLOWED_EXT:
        ext = "jpg"

    job_number = sess["job_number"]
    category = sess["category"]
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
        session_id, job_number, sess["employee_name"], category, unique_name, thumb_name
    )

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
