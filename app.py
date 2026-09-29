"""
Campaign Photo Portal - Flask app.

Flow:
  0. GET  /login            -> name + 4-digit PIN (see auth.py); everything below requires this
  1. GET  /                 -> job number form (name comes from the logged-in account)
  2. POST /start            -> creates/finds the job folder, starts a session, redirects in
  3. GET  /session/<id>     -> camera-capture upload page + live gallery for that job
  4. POST /api/upload       -> one photo lands here the instant it's picked; saved to disk
                               immediately (fast, local)
  5. POST /api/finalize     -> marks the session's photos as finalized (the "Submit" button);
                               only finalized photos are picked up for background Drive sync
  6. GET  /gallery/<job>    -> read-only view of everything uploaded for a job (for supervisors)
  7. GET  /admin            -> admin-only: create/manage user accounts, reset PINs

New Store Kits (config.NEW_STORE_KITS_CATEGORY) take a different path from
step 2 on: /start joins or creates ONE shared session for the job + kit name
(or reopens it, if that kit was already submitted), and the upload page
(static/kit.js) works through the /api/kit/* JSON endpoints below - numbered
packs reserved by one person at a time, item numbers typed per pack, pack
labels printed on the server's Zebra printer (labels.py), and a
collaborative Final Submit instead of Submit.

Run for quick local testing: py app.py
Run for real use (all-day, several phones):  py serve.py   <- use this one day-to-day

Both bind 0.0.0.0:5000 so phones on the hotspot/LAN can reach it.
"""
import io
import re
import sqlite3
import time
import uuid
from datetime import datetime, timedelta
from urllib.parse import quote

from flask import (
    Flask, abort, flash, g, jsonify, redirect, render_template, request, send_from_directory,
    session,
)
from PIL import Image, ImageOps
from werkzeug.exceptions import NotFound

import auth
import db
import drive_sync
import kits
import labels
import local_cleanup
from config import (
    CATEGORIES,
    CONSIGNMENT_LOGGING_CATEGORY,
    HOST,
    KIT_NAME_MAX_LEN,
    KIT_POLL_INTERVAL_SEC,
    NEW_STORE_KITS_CATEGORY,
    PACK_IDLE_TAKEOVER_SEC,
    PORT,
    SECRET_KEY,
    SESSION_LIFETIME_DAYS,
    THUMB_MAX_PX,
    UPLOAD_DIR,
    load_drive_config,
    load_label_config,
)

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=SESSION_LIFETIME_DAYS)
if not SECRET_KEY:
    print("[startup] SECRET_KEY not set in .env - login sessions will not work until you set one.")

_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._ -]+")
ALLOWED_EXT = {"jpg", "jpeg", "png", "webp", "heic", "heif"}
PIN_RE = re.compile(r"^\d{4}$")
JOB_NUMBER_RE = re.compile(r"^[Jj]\d{6}$")


@app.before_request
def _load_user():
    auth.load_current_user()


@app.context_processor
def _inject_user():
    return {"current_user": g.user}


def _format_started_at(iso_str):
    """Reformats a stored started_at timestamp (already in the server's own
    local time - see db.now_iso, which calls astimezone()) into
    "YYYY/MM/DD hh:mm:ss AM/PM ZONE" for display. The zone label is read off
    the timestamp's actual UTC offset (AEDT during daylight saving, AEST
    otherwise) rather than hardcoded, so it stays correct across the
    changeover instead of just always saying one or the other."""
    if not iso_str:
        return ""
    dt = datetime.fromisoformat(iso_str)
    offset = dt.utcoffset()
    if offset == timedelta(hours=11):
        zone = "AEDT"
    elif offset == timedelta(hours=10):
        zone = "AEST"
    else:
        zone = dt.strftime("%z")
    return f"{dt.strftime('%Y/%m/%d %I:%M:%S %p')} {zone}"


app.jinja_env.filters["started_at"] = _format_started_at


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
    """Every render of the job-number page goes through here, so the
    template always gets every variable - explicit None/"" defaults rather
    than Jinja's Undefined, which counts as "is not none" (an error
    re-render used to show an empty "Batch submitted" banner because of it).
    form_* carry what the person typed back into the form on an error."""
    for key in ("submitted", "submitted_job", "submitted_kit", "kit_choice"):
        extra.setdefault(key, None)
    for key in ("form_job_number", "form_category", "form_kit_name"):
        extra.setdefault(key, "")
    open_kit_rows = db.list_open_kits()
    return render_template(
        "index.html", categories=CATEGORIES,
        consignment_logging_category=CONSIGNMENT_LOGGING_CATEGORY,
        new_store_kits_category=NEW_STORE_KITS_CATEGORY,
        open_sessions=db.list_open_sessions(g.user["name"]),
        open_kits=_open_kits_for_index(open_kit_rows),
        # Oldest first, like the "already has kits" choice on /start.
        open_kit_names=[
            {"jobNumber": k["job_number"], "kitName": k["kit_name"]}
            for k in sorted(open_kit_rows, key=lambda k: k["started_at"])
        ],
        # Every kit, open or submitted - the kit-name suggestions offer a
        # finished kit too, since typing its name reopens it (see _start_kit).
        kit_names=[
            {
                "jobNumber": k["job_number"],
                "kitName": k["kit_name"],
                "status": "submitted" if k["finalized_at"] else "open",
                "submittedAt": _kit_submitted_at(k),
            }
            for k in db.list_kit_names()
        ],
        recent_job_numbers=db.recent_job_numbers(),
        **extra
    )


def _kit_submitted_at(kit_row):
    """Display time of a SUBMITTED kit's latest Final Submit, else None (an
    open kit - reopened or never submitted - has no "submitted" to show)."""
    if not kit_row["finalized_at"]:
        return None
    return _format_started_at(kit_row["last_submitted_at"] or kit_row["finalized_at"]) or None


def _open_kits_for_index(kit_rows):
    """The start page's kit cards - every open kit, for every user (a kit is
    shared; anyone might be joining it), with who's working on which pack
    right now and whether THIS viewer left a pack reserved there."""
    held = db.held_packs_by_user(g.user["name"])
    now_ts = int(time.time())
    cards = []
    for kit in kit_rows:
        packs = db.packs_for_kit(kit["session_id"])
        cards.append({
            "session_id": kit["session_id"],
            "job_number": kit["job_number"],
            "kit_name": kit["kit_name"],
            "created_by": kit["created_by"],
            "started_at": kit["started_at"],
            "photo_count": kit["photo_count"],
            "pack_count": kit["pack_count"],
            "collaborators": db.merge_collaborators(
                kit["created_by"], packs, db.general_photo_uploaders(kit["session_id"])
            ),
            "working": _kit_working(packs, now_ts),
            "can_delete": _can_delete_kit(kit),
            "my_pack_number": held.get(kit["session_id"]),
            "reopened": bool(kit["reopen_count"]),
        })
    return cards


def _move_upload_files(uploads, old_job_number, new_job_number, category, strict=True):
    """Moves these uploads' photo + thumbnail files from the old job's folder
    to the new one's. strict: all or nothing - on any failure what was moved
    goes back and the OSError is raised. Not strict: a best-effort sweep
    (files already moved, or missing, are skipped). Returns [(src, dst)]."""
    job_dir(new_job_number, category).mkdir(parents=True, exist_ok=True)
    thumb_dir(new_job_number, category)  # creates the thumbs subfolder too
    moved = []
    try:
        for u in uploads:
            for filename, src_dir, dst_dir in (
                (u["filename"], job_dir(old_job_number, category), job_dir(new_job_number, category)),
                (u["thumb_filename"], thumb_dir(old_job_number, category), thumb_dir(new_job_number, category)),
            ):
                if not filename:
                    continue
                src, dst = src_dir / filename, dst_dir / filename
                if not src.exists() or (dst.exists() and not strict):
                    continue
                if dst.exists():
                    raise FileExistsError(f"{dst} already exists")
                src.rename(dst)
                moved.append((src, dst))
    except OSError:
        if not strict:
            return moved
        _move_files_back(moved)
        raise
    return moved


def _move_files_back(moved):
    for src, dst in reversed(moved):  # best-effort rollback so disk matches the DB again
        try:
            dst.rename(src)
        except OSError:
            pass


def _cleanup_empty_job_dir(job_number, category):
    """Best-effort - removes the thumbs folder and then the category folder
    for a job if a delete/rename just emptied them out. Never raises: a
    folder that isn't actually empty (still has another session's photos)
    just fails its rmdir and is left alone, which is the correct outcome."""
    thumbs = job_dir(job_number, category) / "thumbs"
    try:
        thumbs.rmdir()
    except OSError:
        pass
    try:
        job_dir(job_number, category).rmdir()
    except OSError:
        pass
    try:
        (UPLOAD_DIR / job_number).rmdir()  # only succeeds if no other category folder is left either
    except OSError:
        pass


@app.route("/")
@auth.login_required
def index():
    return _render_index(
        submitted=request.args.get("submitted", type=int),
        submitted_job=request.args.get("job"),
        submitted_kit=request.args.get("kit"),
    )


def _render_login(stage, next_url, **extra):
    """`existing_names` only matters on the 'name' stage (that's the only
    one with the dropdown) - computed here so every early-return in login()
    below doesn't have to remember to pass it itself."""
    if stage == "name":
        extra.setdefault("existing_names", db.list_active_user_names())
    return render_template("login.html", stage=stage, next=next_url, **extra)


@app.route("/login", methods=["GET", "POST"])
def login():
    """One route, one template covering the whole name+PIN flow - login,
    self-signup, and choosing a PIN (first time or after an admin reset) are
    all the same handshake, just at different points, so they share a single
    state machine instead of three separate pages. `stage` tells the
    template which fields to show; `name` (and, once known, which stage)
    is threaded through as a hidden field rather than kept in session, so a
    page refresh/back-button mid-flow just re-asks instead of getting stuck
    on stale server-side state."""
    next_url = request.values.get("next") or "/"
    if request.method == "GET":
        return _render_login("name", next_url)

    name = (request.form.get("name") or "").strip()
    if not name:
        return _render_login("name", next_url, error="Enter your name.")

    if request.form.get("action") == "signup":
        if db.find_user_by_name(name):
            return _render_login("name", next_url, error=f'"{name}" is already taken - try another name.')
        try:
            # active=0: a self-signup needs an admin's approval before it can
            # choose a PIN (and therefore before it can log in) - unlike an
            # admin creating the account directly, nobody has vetted this one yet.
            db.create_user(name, role="standard", pin_hash=None, active=0)
        except sqlite3.IntegrityError:
            return _render_login("name", next_url, error=f'"{name}" is already taken - try another name.')
        return _render_login("pending_approval", next_url, name=name)

    user = db.find_user_by_name(name)
    if user is None:
        return _render_login("confirm_signup", next_url, name=name)
    if not user["active"]:
        return _render_login(
            "name", next_url,
            error="Your account is waiting for admin approval - check back soon.",
        )
    if user["locked_at"]:
        return _render_login(
            "name", next_url, error="This account is locked - ask an admin to reset your PIN.",
        )

    if user["pin_hash"] is None:
        pin = (request.form.get("pin") or "").strip()
        pin_confirm = request.form.get("pin_confirm")
        if pin_confirm is None:  # just arrived at this stage - name submitted, PIN not yet
            return _render_login("choose_pin", next_url, name=name)
        if not PIN_RE.match(pin):
            return _render_login("choose_pin", next_url, name=name, error="PIN must be exactly 4 digits.")
        if pin != pin_confirm:
            return _render_login("choose_pin", next_url, name=name, error="PINs don't match.")
        auth.login_user(auth.set_pin_and_sync(user["id"], pin))
        return redirect(next_url)

    pin = (request.form.get("pin") or "").strip()
    if not pin:  # just arrived at this stage - name submitted, PIN not yet
        return _render_login("enter_pin", next_url, name=name)
    if not PIN_RE.match(pin) or not auth.verify_pin(pin, user["pin_hash"]):
        db.record_failed_login(user["id"], auth.max_login_attempts())
        return _render_login("enter_pin", next_url, name=name, error="Incorrect PIN.")

    db.clear_failed_attempts(user["id"])
    auth.login_user(user)
    return redirect(next_url)


@app.route("/logout", methods=["POST"])
def logout():
    # Shared shift phones: "log out and hand the phone over" mid-kit is a
    # normal flow, so any New Store Kit pack this person still has reserved is
    # released here - otherwise it would block Final Submit for everyone until
    # someone waited out the idle takeover. Never lets a DB hiccup stop the
    # logout itself.
    if g.user is not None:
        try:
            db.release_all_packs_held_by(g.user["name"])
        except sqlite3.Error as exc:
            print(f"[logout] could not release kit packs for {g.user['name']}: {exc}")
    auth.logout_user()
    return redirect("/login")


@app.route("/change-pin", methods=["GET", "POST"])
@auth.login_required
def change_pin():
    """Self-service PIN change for whoever's already logged in - the only
    way an admin (who has no one else to reset THEIR pin) can ever get a
    new one without editing the database directly."""
    if request.method == "GET":
        return render_template("change_pin.html", error=None)

    pin = (request.form.get("pin") or "").strip()
    pin_confirm = (request.form.get("pin_confirm") or "").strip()
    if not PIN_RE.match(pin):
        return render_template("change_pin.html", error="PIN must be exactly 4 digits.")
    if pin != pin_confirm:
        return render_template("change_pin.html", error="PINs don't match.")

    auth.set_pin_and_sync(g.user["id"], pin)
    return redirect("/")


@app.route("/admin")
@auth.admin_required
def admin_page():
    sessions_by_employee = {}
    for row in db.list_all_open_sessions():
        sessions_by_employee.setdefault(row["employee_name"], []).append(row)
    return render_template(
        "admin.html", users=db.list_users(), sessions_by_employee=sessions_by_employee,
        categories=CATEGORIES, consignment_logging_category=CONSIGNMENT_LOGGING_CATEGORY,
    )


@app.route("/admin/users", methods=["POST"])
@auth.admin_required
def admin_create_user():
    name = (request.form.get("name") or "").strip()
    role = "admin" if request.form.get("role") == "admin" else "standard"
    if not name:
        flash("Enter a name.")
    elif db.find_user_by_name(name):
        flash(f'"{name}" is already taken - try another name.')
    else:
        try:
            db.create_user(name, role=role, pin_hash=None)
        except sqlite3.IntegrityError:
            flash(f'"{name}" is already taken - try another name.')
    return redirect("/admin")


@app.route("/admin/users/<int:user_id>/reset-pin", methods=["POST"])
@auth.admin_required
def admin_reset_pin(user_id):
    db.reset_pin(user_id)
    return redirect("/admin")


@app.route("/admin/users/<int:user_id>/approve", methods=["POST"])
@auth.admin_required
def admin_approve_user(user_id):
    """Lets a self-signup account (active=0, no PIN yet) proceed to choose
    one and log in - see db.create_user's active=0 default for self-signup."""
    db.set_user_active(user_id, True)
    return redirect("/admin")


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@auth.admin_required
def admin_delete_user(user_id):
    if user_id != g.user["id"]:  # never let an admin delete their own account out from under themselves
        db.delete_user(user_id)
    return redirect("/admin")


@app.route("/admin/users/<int:user_id>/role", methods=["POST"])
@auth.admin_required
def admin_toggle_role(user_id):
    user = db.get_user(user_id)
    if user:
        new_role = "standard" if user["role"] == "admin" else "admin"
        db.set_user_role(user_id, new_role)
    return redirect("/admin")


@app.route("/start", methods=["POST"])
@auth.login_required
def start():
    raw_job = request.form.get("job_number", "")
    category = (request.form.get("category") or "").strip()
    # What was typed, handed back to the form on any error re-render so a
    # typo in one field doesn't wipe the other two.
    typed = dict(
        form_job_number=raw_job.strip(),
        form_category=category,
        form_kit_name=(request.form.get("kit_name") or "").strip(),
    )

    job_number = sanitize_for_filename(raw_job)
    if not job_number:
        return _render_index(error="Enter a valid Job Number.", **typed)
    if not JOB_NUMBER_RE.match(job_number):
        return _render_index(
            error='Job Number must be "J" followed by exactly 6 digits - e.g. J457008.', **typed
        )
    # One spelling per job: "j456789" and "J456789" would otherwise be two
    # different folders, Drive folders, kits and autocomplete entries.
    job_number = job_number.upper()

    employee_name = g.user["name"]

    if category not in CATEGORIES:
        return _render_index(error="Select a Photo Type.", **typed)

    if category == NEW_STORE_KITS_CATEGORY:
        # Kits are shared sessions with their own join/create rules - none of
        # the per-person resume / keep-logs / Drive rehydrate logic below applies.
        return _start_kit(job_number, typed)

    job_dir(job_number, category).mkdir(parents=True, exist_ok=True)
    db.ensure_job(job_number)

    force_new = request.form.get("force_new") == "1"
    existing = None if force_new else db.find_open_session(job_number, employee_name, category)
    if existing:
        # Don't silently resume - the keep_logs choice on THIS submission would
        # otherwise be dropped on the floor in favor of whatever the old session
        # was created with. Let the user pick instead of guessing for them.
        return _render_index(
            existing_session=existing,
            existing_photo_count=len(db.uploads_for_session(existing["id"])),
            pending_job_number=job_number,
            pending_category=category,
            pending_keep_logs=request.form.get("keep_logs") == "on",
        )

    keep_logs = category == CONSIGNMENT_LOGGING_CATEGORY and request.form.get("keep_logs") == "on"

    resumed_count = 0
    if keep_logs and not db.has_consignments(job_number, category):
        resumed_count = _rehydrate_from_drive(job_number, category)

    session_id = uuid.uuid4().hex
    db.create_session(session_id, job_number, employee_name, category, keep_logs=keep_logs)

    suffix = f"?resumed={resumed_count}" if resumed_count else ""
    return redirect(f"/session/{session_id}{suffix}")


def _sheet_name_taken_text(job_number, kit_name, holder):
    """Why a kit name can't be used yet (db.kit_holding_sheet_name)."""
    other = holder["kit_name"] if holder is not None else "another kit"
    return (
        f'The Drive Pack Log "{job_number} - {kit_name} - Pack Log" still belongs to kit "{other}" '
        f'(renamed from "{kit_name}") until "{other}" is submitted again - pick another name, '
        f'or open "{other}".'
    )


def _start_kit(job_number, typed):
    """/start for New Store Kits. A kit is identified by job + kit name
    (case/space-insensitive), and starting one that already exists simply
    JOINS it - several people packing the same kit at once is the point, so
    there's no "continue or start new?" question like a personal batch gets.
    Naming a kit that was already SUBMITTED reopens it (db.reopen_kit) and
    lands on it, so a finished kit can be corrected or added to and then
    submitted again - what's already in Drive stays there.

    The one guard: if this job already has kits (open or submitted) and the
    typed name matches none of them, it's far more likely a typo ("Store12"
    for "Store 12") than a genuinely new kit, so the page asks "join /
    reopen one of these, or create the new one?" instead - the create button
    re-posts with force_new_kit=1 (same pattern as force_new above).

    Arriving at a kit with nothing in it yet (no packs, no photos - a
    brand-new kit) creates Pack 1 reserved for this person, so the Item
    number field is ready the moment the page opens. A kit that already has
    content - packs, or only kit photos - puts nobody into a pack: an empty
    reserved Pack 1 there would just hold up Final Submit for everyone
    (+ New Pack is one tap away)."""
    kit_name, kit_norm = kits.normalize_kit_name(request.form.get("kit_name"))
    if not kit_name:
        return _render_index(error="Enter the New Store Kit name.", **typed)
    if len(kit_name) > KIT_NAME_MAX_LEN:
        return _render_index(
            error=f"The New Store Kit name is too long - keep it to {KIT_NAME_MAX_LEN} characters.", **typed
        )

    if request.form.get("force_new_kit") != "1" and db.find_kit(job_number, kit_norm) is None:
        job_kits = db.list_kits_for_job(job_number)
        if job_kits:
            return _render_index(
                kit_choice={
                    "jobNumber": job_number,
                    "kitName": kit_name,
                    "category": NEW_STORE_KITS_CATEGORY,
                    "kits": [
                        {
                            "kitName": k["kit_name"],
                            "status": "submitted" if k["finalized_at"] else "open",
                            "submittedAt": _kit_submitted_at(k),
                        }
                        for k in job_kits
                    ],
                    # Rev-3 name for the open ones, kept for older templates.
                    "openKits": [k["kit_name"] for k in job_kits if not k["finalized_at"]],
                },
                **typed,
            )

    if db.find_kit(job_number, kit_norm) is None:
        holder = db.kit_holding_sheet_name(job_number, kit_norm)
        if holder is not None:
            return _render_index(error=_sheet_name_taken_text(job_number, kit_name, holder), **typed)

    db.ensure_job(job_number)
    job_dir(job_number, NEW_STORE_KITS_CATEGORY).mkdir(parents=True, exist_ok=True)
    kit, _existing = db.find_or_create_kit(job_number, kit_name, kit_norm, g.user["name"])
    if kit["finalized_at"]:
        # False only if a colleague reopened it a moment ago - open either way.
        db.reopen_kit(kit["session_id"], g.user["name"])
        return redirect(f"/session/{kit['session_id']}?reopened=1")

    if not db.packs_for_kit(kit["session_id"]) and not db.uploads_for_session(kit["session_id"]):
        db.create_pack(kit["session_id"], g.user["name"])
    return redirect(f"/session/{kit['session_id']}")


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
@auth.login_required
def session_page(session_id):
    sess = db.get_session(session_id)
    if not sess:
        abort(404)
    if sess["category"] == NEW_STORE_KITS_CATEGORY:
        return _kit_page(sess)
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
        kit_mode=False,
        kit=None,
        kit_state=None,
        kit_poll_ms=_kit_poll_ms(),
        can_delete=True,  # the delete route itself still checks ownership, as before
        reopened_notice=False,
    )


def _kit_page(sess):
    """The upload page in New Store Kit mode - the same template, rendered
    with the kit's state for its first paint; static/kit.js takes over from
    there (polling /api/kit/state)."""
    kit = db.get_kit(sess["id"])
    kit_state = _kit_state(sess)
    if kit is None or kit_state is None:
        abort(404)
    return render_template(
        "upload.html",
        session=sess,
        category_label=CATEGORIES.get(sess["category"], sess["category"]),
        photos=db.uploads_for_session(sess["id"]),
        finalized=bool(kit["finalized_at"]),
        consignment_logging=False,
        sections_json=[],
        consignment_values=[],
        resumed_count=None,
        kit_mode=True,
        kit=kit,
        kit_state=kit_state,
        kit_poll_ms=_kit_poll_ms(),
        can_delete=_can_delete_kit(kit),
        # ?reopened=1: just reopened from the start page or the finished
        # kit's own page - the page says what that means for Drive.
        reopened_notice=request.args.get("reopened") == "1",
    )


def _own_session_or_403(sess):
    if sess["employee_name"] != g.user["name"] and g.user["role"] != "admin":
        abort(403)


@app.route("/session/<session_id>/delete", methods=["POST"])
@auth.login_required
def delete_session(session_id):
    """Plain form fallback: deletes then redirects (with a flash) to `next`
    (defaults to "/"). The list pages (Active Sessions, admin's User
    Sessions) submit this via fetch instead - see topbar.js - so the row
    just disappears in place rather than reloading the whole page; `ajax=1`
    marks that case and gets a bare JSON result with no flash/redirect,
    since there's no page load left for a flash to appear on."""
    is_ajax = request.form.get("ajax") == "1"
    next_url = request.form.get("next") or "/"
    sess = db.get_session(session_id)
    if not sess:
        if is_ajax:
            return jsonify(ok=False, error="Session not found."), 404
        abort(404)
    if sess["category"] == NEW_STORE_KITS_CATEGORY:
        return _delete_kit(sess, is_ajax, next_url)
    _own_session_or_403(sess)
    if sess["finalized_at"]:
        message = "This batch was already submitted - it can't be deleted."
        if is_ajax:
            return jsonify(ok=False, error=message), 400
        flash(message)
        return redirect(next_url)

    job_number, category = sess["job_number"], sess["category"]
    touched_consignments = set()
    for u in db.uploads_for_session(session_id):
        (job_dir(job_number, category) / u["filename"]).unlink(missing_ok=True)
        (thumb_dir(job_number, category) / u["thumb_filename"]).unlink(missing_ok=True)
        if u["consignment_id"]:
            touched_consignments.add(u["consignment_id"])
        db.delete_upload(u["id"])
    for consignment_id in touched_consignments:
        db.delete_consignment_if_orphaned(consignment_id)
    db.delete_session(session_id)
    _cleanup_empty_job_dir(job_number, category)

    if is_ajax:
        return jsonify(ok=True)
    flash(f"Session for {job_number} deleted.", "success")
    return redirect(next_url)


_WAS_SUBMITTED_MESSAGE = "This kit was submitted before - it can't be deleted (its photos are in Drive)."


def _delete_kit(sess, is_ajax, next_url):
    """Deleting a New Store Kit removes it for EVERYONE (every pack, item
    and photo from every collaborator), so only the person who started it or
    an admin may - and only while it has never been submitted: a reopened
    kit's photos are already in Drive and its Pack Log Sheet exists, so it
    can be changed but not deleted. The DB side is one transaction that also
    refuses a kit that's just been submitted (see db.delete_kit); files are removed only
    after that has committed, best-effort - on Windows an unlink fails while
    another thread is streaming that thumbnail to a collaborator's phone, and
    local_cleanup's empty-folder prune tidies up whatever is left."""
    def fail(message, status, code):
        if is_ajax:
            return jsonify(ok=False, error=message, code=code), status
        flash(message)
        return redirect(next_url)

    kit = db.get_kit(sess["id"])
    if kit is None:
        if is_ajax:
            return jsonify(ok=False, error="Kit not found.", code="kit_missing"), 404
        abort(404)
    if not _is_kit_owner(kit):
        return fail(
            f"Only {kit['created_by']} (who started this kit) or an admin can delete it.", 403, "not_allowed"
        )
    if kit["finalized_at"]:
        return fail("This kit was already submitted - it can't be deleted.", 400, "finalized")

    status, files = db.delete_kit(sess["id"])
    if status == "missing":
        if is_ajax:
            return jsonify(ok=False, error="Kit not found.", code="kit_missing"), 404
        abort(404)
    if status == "finalized":
        return fail("This kit was already submitted - it can't be deleted.", 400, "finalized")
    if status == "was_submitted":
        return fail(_WAS_SUBMITTED_MESSAGE, 403, "not_allowed")

    job_number, category = sess["job_number"], sess["category"]
    for filename, thumb_filename in files:
        _unlink_photo_files(job_number, category, filename, thumb_filename)
    _cleanup_empty_job_dir(job_number, category)

    if is_ajax:
        return jsonify(ok=True)
    flash(f'Kit "{kit["kit_name"]}" for {job_number} deleted.', "success")
    return redirect(next_url)


@app.route("/session/<session_id>/release-my-packs", methods=["POST"])
@auth.login_required
def release_my_packs(session_id):
    """The start page's [Release] on a kit card ("You still have Pack 3
    open") - the fallback for a pack left reserved by a closed tab or a
    navigation the kit page couldn't see, which would otherwise block Final
    Submit for everyone until the idle takeover."""
    db.release_packs_held_by(session_id, g.user["name"])
    return redirect("/")


@app.route("/session/<session_id>/reopen", methods=["POST"])
@auth.login_required
def reopen_kit_route(session_id):
    """"Reopen to make changes" on a submitted kit's (read-only) page. Any
    logged-in user may - the same as typing the kit's job number and name
    on the start page. Nothing already in Drive is touched; the Pack Log
    Sheet is rewritten at the next Final Submit (see db.reopen_kit)."""
    sess = db.get_session(session_id)
    if not sess or sess["category"] != NEW_STORE_KITS_CATEGORY:
        abort(404)
    kit = db.get_kit(session_id)
    if kit is None:
        abort(404)
    if db.reopen_kit(session_id, g.user["name"]):
        flash(f'Reopened "{kit["kit_name"]}" for {kit["job_number"]}.', "success")
    return redirect(f"/session/{session_id}?reopened=1")


@app.route("/session/<session_id>/edit-job-number", methods=["POST"])
@auth.login_required
def edit_session_job_number(session_id):
    sess = db.get_session(session_id)
    if not sess:
        abort(404)
    if sess["category"] == NEW_STORE_KITS_CATEGORY:
        # A kit's job number is changed from its own edit pane (the ✏️ next to
        # the job number - POST /api/kit/rename with job_number), which keeps
        # every phone on the kit in step; this form is for personal batches.
        flash("Change a New Store Kit's job number with the ✏️ next to it on the kit page.")
        return redirect(f"/session/{session_id}")
    _own_session_or_403(sess)
    if sess["finalized_at"]:
        flash("This batch was already submitted - its job number can't be changed.")
        return redirect(f"/session/{session_id}")

    new_job_number = sanitize_for_filename(request.form.get("job_number", ""))
    if not JOB_NUMBER_RE.match(new_job_number):
        flash('Job Number must be "J" followed by exactly 6 digits - e.g. J457008.')
        return redirect(f"/session/{session_id}")
    new_job_number = new_job_number.upper()  # same single spelling as /start

    old_job_number, category = sess["job_number"], sess["category"]
    if new_job_number == old_job_number:
        return redirect(f"/session/{session_id}")

    uploads = db.uploads_for_session(session_id)
    try:
        _move_upload_files(uploads, old_job_number, new_job_number, category)
    except OSError as exc:
        flash(f"Could not move this job's photos to {new_job_number}: {exc}")
        return redirect(f"/session/{session_id}")

    db.rename_session_job(session_id, new_job_number, category)
    _cleanup_empty_job_dir(old_job_number, category)

    flash(f"Job number updated to {new_job_number}.", "success")
    return redirect(f"/session/{session_id}")


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
@auth.login_required
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
@auth.login_required
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
@auth.login_required
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
@auth.login_required
def api_upload():
    session_id = request.form.get("session_id", "")
    sess = db.get_session(session_id)
    if not sess:
        if request.form.get("pack_id") is not None or request.form.get("general") == "1":
            return _kit_missing()  # only kit.js sends pack_id / general - tell it the kit is gone
        return jsonify(ok=False, error="Session not found - reopen the job."), 404
    if sess["category"] == NEW_STORE_KITS_CATEGORY:
        return _kit_upload(sess)
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

    # Filename is always employee-timestamp, regardless of whether this photo
    # is tagged to a consignment - logging is metadata (see the uploads table's
    # consignment_id), never encoded into the filename itself.
    unique_name = (
        f"{sanitize_for_filename(sess['employee_name'])}-{db.now_filename_stamp()}.{_upload_ext(file)}"
    )
    thumb_name = _save_photo_files(job_number, category, unique_name, file.read())

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


def _upload_ext(file):
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else "jpg"
    return ext if ext in ALLOWED_EXT else "jpg"


def _save_photo_files(job_number, category, unique_name, raw_bytes):
    """Writes the full-res original, then a thumbnail for the on-page
    gallery. Returns the thumbnail's filename - or the original's own name
    if no thumbnail could be made, in which case the gallery just falls back
    to the full image."""
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
    return thumb_name


def _unlink_photo_files(job_number, category, filename, thumb_filename):
    """Best-effort removal of a photo's full-res file and thumbnail. Never
    raises: Windows refuses to unlink a file another thread has open (e.g.
    while it's being streamed to someone's phone), and a leftover file is
    harmless - the DB row that pointed at it is already gone."""
    for path in (
        job_dir(job_number, category) / filename,
        job_dir(job_number, category) / "thumbs" / thumb_filename,
    ):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            print(f"[delete] could not remove {path}: {exc}")


def _kit_upload(sess):
    """/api/upload for a New Store Kit photo. The hold on the pack is checked
    (and the heartbeat refreshed, which blocks any idle takeover) BEFORE a
    single byte is written, and checked again atomically by the INSERT - if
    the pack was lost, or the kit submitted/deleted, during the ~1s the file
    write takes, the files just written are removed again so nothing is left
    that no pack owns. The file is named after, and credited to, whoever is
    logged in (a kit's session belongs to its creator, but everyone adds to it)."""
    session_id = sess["id"]
    employee_name = g.user["name"]
    if db.get_kit(session_id) is None:
        return _kit_missing()

    pack_id = request.form.get("pack_id", type=int)
    general = request.form.get("general") == "1"
    if pack_id is None and not general:
        # Explicit either way: a phone that lost track of its pack must not
        # silently file a pack photo under "no pack".
        return _kit_error(sess, "invalid", "Open a pack before adding photos.", 400)
    file = request.files.get("file")
    if not file or not file.filename:
        return _kit_error(sess, "invalid", "No file received.", 400)

    if pack_id is None:
        return _kit_general_upload(sess, file)

    if not db.heartbeat_pack(pack_id, session_id, employee_name):
        return _kit_hold_failure(sess, pack_id)

    job_number, category = sess["job_number"], sess["category"]
    unique_name = f"{sanitize_for_filename(employee_name)}-{db.now_filename_stamp()}.{_upload_ext(file)}"
    thumb_name = _save_photo_files(job_number, category, unique_name, file.read())

    upload_id = db.add_kit_upload(
        session_id, job_number, employee_name, category, unique_name, thumb_name, pack_id
    )
    if upload_id is None:
        _unlink_photo_files(job_number, category, unique_name, thumb_name)
        return _kit_hold_failure(sess, pack_id)

    return _kit_ok(
        sess, holding_pack_id=pack_id,
        id=upload_id,
        thumbUrl=f"/media/{job_number}/{category}/thumbs/{thumb_name}",
        fullUrl=f"/media/{job_number}/{category}/{unique_name}",
    )


def _kit_general_upload(sess, file):
    """A kit photo that belongs to NO pack (the kit page's "Kit photos"
    section - the whole kit, a pallet, a delivery note). No pack hold is
    needed, so several people can add these at once; the INSERT only checks
    the kit is still open, and the files just written are removed again if
    it was submitted or deleted meanwhile."""
    session_id = sess["id"]
    employee_name = g.user["name"]
    job_number, category = sess["job_number"], sess["category"]
    unique_name = f"{sanitize_for_filename(employee_name)}-{db.now_filename_stamp()}.{_upload_ext(file)}"
    thumb_name = _save_photo_files(job_number, category, unique_name, file.read())

    upload_id = db.add_kit_general_upload(session_id, job_number, employee_name, category, unique_name, thumb_name)
    if upload_id is None:
        _unlink_photo_files(job_number, category, unique_name, thumb_name)
        kit = db.get_kit(session_id)
        if kit is None:
            return _kit_missing()
        return _kit_finalized_error(sess)
    return _kit_ok(
        sess,
        id=upload_id,
        thumbUrl=f"/media/{job_number}/{category}/thumbs/{thumb_name}",
        fullUrl=f"/media/{job_number}/{category}/{unique_name}",
    )


@app.route("/api/delete", methods=["POST"])
@auth.login_required
def api_delete():
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "")
    upload_id = data.get("upload_id")

    sess = db.get_session(session_id)
    if not sess:
        return jsonify(ok=False, error="Session not found."), 404
    if sess["category"] == NEW_STORE_KITS_CATEGORY:
        return _kit_delete_photo(sess, upload_id)
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


def _kit_delete_photo(sess, upload_id):
    """/api/delete for a kit photo - only by whoever holds its pack, like
    every other change to a pack, and never a photo that went to Drive with
    an earlier submission of a reopened kit ("photo_locked"). Deleting one
    that's already gone (a double tap) is a success, not an error."""
    if db.get_kit(sess["id"]) is None:
        return _kit_missing()
    upload_id = _int_or_none(upload_id)
    if upload_id is None:
        return _kit_error(sess, "invalid", "Photo not found.", 400)

    target = db.get_upload(upload_id)
    if target is not None and target["session_id"] == sess["id"] and target["pack_id"] is None:
        return _kit_delete_general_photo(sess, upload_id)

    status, row = db.delete_kit_upload(upload_id, sess["id"], g.user["name"])
    if status == "deleted":
        _unlink_photo_files(row["job_number"], row["category"], row["filename"], row["thumb_filename"])
        return _kit_ok(sess)
    if status == "gone":
        return _kit_ok(sess)
    if status == "finalized":
        return _kit_finalized_error(sess)
    if status == "locked":
        return _kit_error(
            sess, "photo_locked", "Photos already sent to Drive can't be removed.", 409,
            holding_pack_id=row["pack_id"] if row else None,
        )
    return _kit_error(
        sess, "not_holding", "Only the person editing this pack can remove its photos.", 409,
        holding_pack_id=row["pack_id"] if row else None,
    )


def _kit_delete_general_photo(sess, upload_id):
    """A no-pack kit photo: its uploader or an admin may remove it while the
    kit is open (there's no pack hold to go by); never once submitted."""
    status, row = db.delete_kit_general_upload(
        upload_id, sess["id"], g.user["name"], is_admin=g.user["role"] == "admin"
    )
    if status == "deleted":
        _unlink_photo_files(row["job_number"], row["category"], row["filename"], row["thumb_filename"])
        return _kit_ok(sess)
    if status == "gone":
        return _kit_ok(sess)
    if status == "finalized":
        return _kit_finalized_error(sess)
    if status == "locked":
        return _kit_error(sess, "photo_locked", "Photos already sent to Drive can't be removed.", 409)
    if status == "in_pack":  # it moved into a pack meanwhile - can't happen today, but answer sanely
        return _kit_delete_photo(sess, upload_id)
    who = row["employee_name"] if row else "whoever took it"
    return _kit_error(sess, "not_allowed", f"Only {who} (who took it) or an admin can remove this photo.", 403)


@app.route("/api/finalize", methods=["POST"])
@auth.login_required
def api_finalize():
    session_id = (request.get_json(silent=True) or {}).get("session_id", "")
    sess = db.get_session(session_id)
    if not sess:
        return jsonify(ok=False, error="Session not found."), 404
    if sess["category"] == NEW_STORE_KITS_CATEGORY:
        # A kit is submitted by its collaborators together, with checks this
        # plain Submit doesn't make - see /api/kit/finalize.
        return jsonify(ok=False, error="Use Final Submit for New Store Kits.", code="invalid"), 400
    count = db.finalize_session(session_id)
    return jsonify(ok=True, count=count)


# --- New Store Kits: shared state + JSON API (consumed by static/kit.js) ----
#
# Every endpoint takes a JSON body with session_id, acts as the logged-in
# user (g.user - never a name from the request), and answers
#   {"ok": true, "state": <KitState>, ...}   or
#   {"ok": false, "error": "<message for people>", "code": "<for kit.js>", "state": <KitState>}
# kit.js branches on `code`, never on the HTTP status. Only "kit_missing"
# (the whole kit is gone) omits state; everything else sends the latest
# state so the page can redraw itself straight from the answer.

def _kit_poll_ms():
    return int(KIT_POLL_INTERVAL_SEC * 1000)


def _is_kit_owner(kit):
    """The kit's creator, or an admin."""
    return kit["created_by"] == g.user["name"] or g.user["role"] == "admin"


def _can_delete_kit(kit):
    """Deleting a kit deletes it for everyone, so only its creator or an
    admin may - and only a kit that has never been submitted (a submitted,
    or reopened, kit's photos are in Drive; see db.delete_kit)."""
    return _is_kit_owner(kit) and not kit["finalized_at"] and not kit["submit_count"]


def _idle_minutes(pack, now_ts):
    """Whole minutes since the holder's last heartbeat, or None if the pack is free."""
    if not pack["reserved_by"]:
        return None
    last = pack["reserved_ts"] if pack["reserved_ts"] is not None else now_ts
    return max(0, now_ts - last) // 60


def _kit_working(packs, now_ts):
    """Everyone holding a pack right now, by pack number - idle or not (an
    idle holder still blocks Final Submit; the takeover is how that's fixed)."""
    return [
        {"name": p["reserved_by"], "packNumber": p["pack_number"], "idleMinutes": _idle_minutes(p, now_ts)}
        for p in packs if p["reserved_by"]
    ]


def _working_text(working):
    """"A (Pack 3), B (Pack 4, idle 12 min)". The idle note only appears
    from 2 minutes on, so the few seconds between two polls never read as
    "idle 0 min"."""
    parts = []
    for w in working:
        idle = f", idle {w['idleMinutes']} min" if (w["idleMinutes"] or 0) >= 2 else ""
        parts.append(f"{w['name']} (Pack {w['packNumber']}{idle})")
    return ", ".join(parts)


def _kit_photo_json(u):
    return {
        "id": u["id"],
        "thumbUrl": f"/media/{u['job_number']}/{u['category']}/thumbs/{u['thumb_filename']}",
        "fullUrl": f"/media/{u['job_number']}/{u['category']}/{u['filename']}",
        # Sent to Drive with an earlier submission of a reopened kit - it
        # can't be removed any more (see db.delete_kit_upload).
        "locked": u["status"] == "finalized",
        # False once local_cleanup has deleted this machine's copy (days
        # after it reached Drive) - thumbUrl/fullUrl then redirect to the
        # Drive file's page, which an <img> can't show, so the page draws an
        # "In Drive" tile instead (like the gallery) and links it there.
        "localCopy": u["local_deleted_at"] is None,
    }


def _kit_state(sess, holding_pack_id=None):
    """Everything the kit page draws, as seen by the current user - see the
    KitState shape in the design notes (camelCase keys, as kit.js reads them).
    Returns None if the kit no longer exists.

    The kit row - and with it `rev` - is read FIRST, before the packs, items
    and photos: those can then only be as new as that rev or newer, never
    older. kit.js drops any state whose rev is below the last one it applied,
    so a state can never roll the page back to data older than its own rev.

    holding_pack_id is the pack the phone believes it's editing; if the
    viewer doesn't hold it any more (taken over, or it was saved/deleted
    elsewhere) `lostPack` says so, so the page can leave the editor and
    explain why instead of silently failing the next add."""
    session_id = sess["id"]
    kit = db.get_kit(session_id)
    if kit is None:
        return None
    packs = db.packs_for_kit(session_id)
    items = db.items_for_kit(session_id)
    uploads = db.uploads_for_session(session_id)

    viewer = g.user["name"]
    finalized = bool(kit["finalized_at"])
    now_ts = int(time.time())

    items_by_pack = {}
    for it in items:
        items_by_pack.setdefault(it["pack_id"], []).append(it)
    photos_by_pack = {}
    for u in uploads:
        photos_by_pack.setdefault(u["pack_id"], []).append(u)
    packs_by_id = {p["id"]: p for p in packs}

    held = None
    for p in packs:  # ascending, so a stray double hold resolves to the newest (as db.held_pack)
        if p["reserved_by"] == viewer:
            held = p

    pack_json = []
    empty_packs = []
    for p in packs:
        pack_items = items_by_pack.get(p["id"], [])
        pack_photos = photos_by_pack.get(p["id"], [])
        holder = p["reserved_by"]
        idle = _idle_minutes(p, now_ts)
        is_empty = not pack_items and not pack_photos
        if is_empty:
            empty_packs.append(p["pack_number"])
        pack_json.append({
            "id": p["id"],
            "number": p["pack_number"],
            "reservedBy": holder,
            "isMine": holder == viewer,
            "idleMinutes": idle,
            "canTakeOver": bool(
                holder and holder != viewer and not finalized
                and p["reserved_ts"] is not None and now_ts - p["reserved_ts"] >= PACK_IDLE_TAKEOVER_SEC
            ),
            "contributors": db.split_list(p["contributors"]),
            "items": [{"id": it["id"], "label": kits.item_label(it["series"], it["idx"])} for it in pack_items],
            "lines": kits.pack_lines(pack_items),
            "groups": kits.pack_groups(pack_items),
            "photos": [_kit_photo_json(u) for u in reversed(pack_photos)],
            "photoCount": len(pack_photos),
            "firstEdit": _format_started_at(p["first_edit_at"]) or None,
            "lastEdit": _format_started_at(p["last_edit_at"]) or None,
            "isEmpty": is_empty,
            "printedAt": _format_started_at(p["last_printed_at"]) or None,
            "printedBy": p["last_printed_by"],
            "printCount": p["print_count"] or 0,
        })
    pack_json.reverse()  # newest pack first, like the consignment cards

    working = _kit_working(packs, now_ts)
    # "Packed so far" counts SAVED packs only - an open pack is still being
    # filled, so counting it would report numbers as packed that may yet move.
    saved_items = [
        it for it in items
        if it["pack_id"] in packs_by_id and packs_by_id[it["pack_id"]]["reserved_by"] is None
    ]
    # Photos that belong to no pack (pack_id NULL - the "Kit photos" section)
    # are keyed under None above; their uploaders are collaborators too.
    general = photos_by_pack.get(None, [])
    general_by = []
    for u in general:
        if u["employee_name"] not in general_by:
            general_by.append(u["employee_name"])
    collaborators = db.merge_collaborators(kit["created_by"], packs, general_by)
    is_collaborator = g.user["role"] == "admin" or viewer in collaborators
    has_content = bool(items) or bool(uploads)
    can_finalize = not finalized and not working and has_content and is_collaborator

    blocked_reason = None
    if not finalized and not can_finalize:
        if working:
            blocked_reason = f"Still packing: {_working_text(working)}."
        elif not has_content:
            blocked_reason = "Add items or photos first."
        else:
            blocked_reason = "Only people who worked on this kit can submit it."

    lost_pack = None
    if holding_pack_id is not None:
        p = packs_by_id.get(holding_pack_id)
        if p is None:
            lost_pack = {"id": holding_pack_id, "number": None, "takenBy": None}
        elif p["reserved_by"] != viewer:
            lost_pack = {"id": p["id"], "number": p["pack_number"], "takenBy": p["reserved_by"]}

    return {
        "rev": kit["rev"],
        "jobNumber": kit["job_number"],
        "kitName": kit["kit_name"],
        "createdBy": kit["created_by"],
        "finalized": finalized,
        "finalizedBy": kit["finalized_by"],
        "finalizedAt": _format_started_at(kit["finalized_at"]) or None,
        "heldPackId": held["id"] if held else None,
        "lostPack": lost_pack,
        "packs": pack_json,
        "series": kits.series_buttons(items),
        "report": [
            {"series": r["series"], "text": r["text"], "complete": r["complete"]}
            for r in kits.series_report(saved_items, full=False)
        ],
        "openPacks": [{"number": w["packNumber"], "by": w["name"]} for w in working],
        "working": working,
        "isCollaborator": is_collaborator,
        "canFinalize": can_finalize,
        "finalizeBlockedReason": blocked_reason,
        "emptyPacks": empty_packs,
        "photoCount": len(uploads),
        # Kit photos that belong to no pack, newest first. "removable": the
        # viewer took it (or is an admin), it isn't submitted and the kit is
        # open - the same rule the server applies on delete.
        "generalPhotos": [
            dict(
                _kit_photo_json(u),
                by=u["employee_name"],
                removable=(
                    not finalized and u["status"] != "finalized"
                    and (u["employee_name"] == viewer or g.user["role"] == "admin")
                ),
            )
            for u in reversed(general)
        ],
        "packCount": len(packs),
        "nextPackNumber": (packs[-1]["pack_number"] + 1) if packs else 1,
        "idleTakeoverMinutes": int(PACK_IDLE_TAKEOVER_SEC // 60),
        "printerConfigured": bool(load_label_config()["printerName"]),
        # A submitted kit that was reopened and not submitted again yet: its
        # Drive Sheet still shows the last submission until the next one.
        "reopened": bool(kit["reopen_count"]) and not finalized,
        "reopenedBy": kit["reopened_by"],
        "reopenedAt": _format_started_at(kit["reopened_at"]) or None,
        "lastSubmittedAt": _format_started_at(kit["last_submitted_at"]) or None,
        "submitCount": kit["submit_count"] or 0,
        "canDelete": _can_delete_kit(kit),
    }


def _int_or_none(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value.strip()):
        return int(value.strip())
    return None


def _kit_request():
    """(json_body, kit session row or None). force=True: the page's pagehide
    release goes out via navigator.sendBeacon, whose body the browser may not
    label as JSON."""
    data = request.get_json(silent=True, force=True)
    if not isinstance(data, dict):
        data = {}
    sess = db.get_session(str(data.get("session_id") or ""))
    if sess is None or sess["category"] != NEW_STORE_KITS_CATEGORY:
        return data, None
    return data, sess


def _kit_missing():
    return jsonify(ok=False, error="This kit was deleted.", code="kit_missing"), 404


def _kit_ok(sess, holding_pack_id=None, **extra):
    state = _kit_state(sess, holding_pack_id)
    if state is None:
        return _kit_missing()
    return jsonify(ok=True, state=state, **extra)


def _kit_error(sess, code, message, status, holding_pack_id=None, **extra):
    state = _kit_state(sess, holding_pack_id)
    if state is None:
        return _kit_missing()
    return jsonify(ok=False, error=message, code=code, state=state, **extra), status


def _kit_finalized_error(sess, message=None):
    kit = db.get_kit(sess["id"])
    if kit is None:
        return _kit_missing()
    if message is None:
        by = kit["finalized_by"]
        message = f"This kit was already submitted by {by}." if by else "This kit was already submitted."
    return _kit_error(sess, "finalized", message, 409)


def _kit_hold_failure(sess, pack_id):
    """The answer when a pack change was refused because the user doesn't
    (or no longer) hold the pack - classified after the fact, for the
    message only (the refusal itself was atomic)."""
    kit = db.get_kit(sess["id"])
    if kit is None:
        return _kit_missing()
    if kit["finalized_at"]:
        return _kit_finalized_error(sess)
    pack = db.get_pack(pack_id) if pack_id is not None else None
    if pack is None or pack["session_id"] != sess["id"]:
        message = "That pack no longer exists."
    elif pack["reserved_by"] and pack["reserved_by"] != g.user["name"]:
        message = f"Pack {pack['pack_number']} is now being edited by {pack['reserved_by']}."
    else:
        message = f"Pack {pack['pack_number']} isn't open for you any more - tap it to edit it again."
    return _kit_error(sess, "not_holding", message, 409, holding_pack_id=pack_id)


@app.route("/api/kit/state", methods=["POST"])
@auth.login_required
def api_kit_state():
    """The kit page's poll (every KIT_POLL_INTERVAL_SEC while visible) - and
    the heartbeat that keeps the viewer's pack reservation from looking
    idle. Works for submitted kits too (read-only view)."""
    data, sess = _kit_request()
    if sess is None:
        return _kit_missing()
    holding = _int_or_none(data.get("holding_pack_id"))
    if holding is not None:
        db.heartbeat_pack(holding, sess["id"], g.user["name"])
    return _kit_ok(sess, holding_pack_id=holding)


@app.route("/api/kit/pack/new", methods=["POST"])
@auth.login_required
def api_kit_pack_new():
    """"+ New Pack" - moves the user out of any pack they hold into a new
    one numbered after the highest so far."""
    data, sess = _kit_request()
    if sess is None:
        return _kit_missing()
    kit = db.get_kit(sess["id"])
    if kit is None:
        return _kit_missing()
    if kit["finalized_at"]:
        return _kit_finalized_error(sess)
    if db.create_pack(sess["id"], g.user["name"]) is None:
        return _kit_hold_failure(sess, None)  # submitted or deleted meanwhile
    return _kit_ok(sess)


@app.route("/api/kit/pack/reserve", methods=["POST"])
@auth.login_required
def api_kit_pack_reserve():
    """Open a pack for editing (releasing the one the user held). "idle"
    means the holder's heartbeat is older than PACK_IDLE_TAKEOVER_SEC - the
    page asks "take it over?" and retries with force: true."""
    data, sess = _kit_request()
    if sess is None:
        return _kit_missing()
    pack_id = _int_or_none(data.get("pack_id"))
    if pack_id is None:
        return _kit_error(sess, "invalid", "Pick a pack to edit.", 400)
    force = data.get("force") in (True, "1", "true")

    status, pack = db.reserve_pack(
        pack_id, sess["id"], g.user["name"], PACK_IDLE_TAKEOVER_SEC, force=force
    )
    if status == "ok":
        return _kit_ok(sess)
    if status == "missing":
        if db.get_kit(sess["id"]) is None:
            return _kit_missing()
        return _kit_error(sess, "pack_missing", "That pack no longer exists.", 409)
    if status == "finalized":
        return _kit_finalized_error(sess)

    holder = pack["reserved_by"] or "someone else"
    idle_minutes = _idle_minutes(pack, int(time.time())) if pack["reserved_by"] else None
    if status == "idle":
        return _kit_error(
            sess, "idle", f"{holder} has been idle for {idle_minutes} min on Pack {pack['pack_number']}.", 409,
            holder=holder, idleMinutes=idle_minutes,
        )
    return _kit_error(
        sess, "taken", f"Pack {pack['pack_number']} is being edited by {holder}.", 409,
        holder=holder, idleMinutes=idle_minutes,
    )


@app.route("/api/kit/pack/release", methods=["POST"])
@auth.login_required
def api_kit_pack_release():
    """Save Pack, and leaving the page (Home/Cancel via a keepalive fetch,
    browser Back via a pagehide beacon). Releases EVERY pack the user holds
    in this kit, whatever pack_id says - idempotent, and it heals any stray
    double hold. A kit that's gone is fine too: there's nothing left to hold."""
    data, sess = _kit_request()
    if sess is None or db.get_kit(sess["id"]) is None:
        return jsonify(ok=True)
    db.release_packs_held_by(sess["id"], g.user["name"])
    state = _kit_state(sess)
    if state is None:
        return jsonify(ok=True)
    return jsonify(ok=True, state=state)


@app.route("/api/kit/item/add", methods=["POST"])
@auth.login_required
def api_kit_item_add():
    data, sess = _kit_request()
    if sess is None:
        return _kit_missing()
    pack_id = _int_or_none(data.get("pack_id"))
    if pack_id is None:
        return _kit_error(sess, "invalid", "Open a pack before adding items.", 400)
    try:
        series, series_norm, idx = kits.parse_item(data.get("series"), data.get("idx"))
    except ValueError as exc:
        return _kit_error(sess, "invalid", str(exc), 400, holding_pack_id=pack_id)
    label = kits.item_label(series, idx)

    result = db.add_pack_item(pack_id, sess["id"], series, series_norm, idx, g.user["name"])
    if result == "added":
        return _kit_ok(sess, holding_pack_id=pack_id, added=True, label=label, message=f"Added {label}")
    if result == "duplicate":
        pack = db.get_pack(pack_id)
        where = f"Pack {pack['pack_number']}" if pack else "this pack"
        return _kit_ok(
            sess, holding_pack_id=pack_id, added=False, label=label,
            message=f"{label} is already in {where}.",
        )
    return _kit_hold_failure(sess, pack_id)


@app.route("/api/kit/item/remove", methods=["POST"])
@auth.login_required
def api_kit_item_remove():
    data, sess = _kit_request()
    if sess is None:
        return _kit_missing()
    pack_id = _int_or_none(data.get("pack_id"))
    item_id = _int_or_none(data.get("item_id"))
    if pack_id is None or item_id is None:
        return _kit_error(sess, "invalid", "Item not found.", 400)
    result = db.remove_pack_item(item_id, pack_id, sess["id"], g.user["name"])
    if result in ("removed", "gone"):  # gone = a double-tapped × - already done
        return _kit_ok(sess, holding_pack_id=pack_id)
    return _kit_hold_failure(sess, pack_id)


@app.route("/api/kit/finalize", methods=["POST"])
@auth.login_required
def api_kit_finalize():
    """Final Submit, after "Is this packed completely?" -> Yes. `rev` is the
    kit version the dialog was showing; if anything changed since (another
    pack saved, an item added), the submit is refused with "changed" and the
    dialog asks again over the fresh report, so nobody confirms contents
    they never saw."""
    data, sess = _kit_request()
    if sess is None:
        return _kit_missing()
    if data.get("confirm") != "yes":
        return _kit_error(sess, "invalid", 'Tap "Yes" to confirm the kit is packed completely.', 400)
    rev = _int_or_none(data.get("rev"))
    if rev is None:
        return _kit_error(sess, "invalid", "Reload the page and try again.", 400)
    kit = db.get_kit(sess["id"])
    if kit is None:
        return _kit_missing()
    if g.user["role"] != "admin" and g.user["name"] not in db.kit_collaborators(sess["id"]):
        return _kit_error(sess, "not_collaborator", "Only people who worked on this kit can submit it.", 403)

    ok, result = db.finalize_kit(sess["id"], g.user["name"], rev)
    if ok:
        return jsonify(
            ok=True, count=result,
            redirect=f"/?submitted={result}&job={quote(kit['job_number'], safe='')}"
                     f"&kit={quote(kit['kit_name'], safe='')}",
        )
    if result == "missing":
        return _kit_missing()
    if result == "finalized":
        kit = db.get_kit(sess["id"])
        by = kit["finalized_by"] if kit else None
        return _kit_finalized_error(sess, f"Already submitted by {by}." if by else None)
    if result in ("reserved", "empty"):
        state = _kit_state(sess)
        if state is None:
            return _kit_missing()
        message = state["finalizeBlockedReason"] or (
            "Someone is still editing a pack." if result == "reserved" else "Add items to a pack first."
        )
        return jsonify(ok=False, error=message, code=result, state=state), 409
    return _kit_error(
        sess, "changed",
        "Something changed while you were confirming - check the report and confirm again.", 409,
    )


# --- New Store Kits: pack labels (labels.py) -----------------------------------

def _label_selection(session_id, pack_ids):
    """Which of the requested packs get a label: ([(pack_row, items), ...] in
    pack order, [pack numbers skipped as empty], [requested ids that aren't
    packs of this kit]). An empty pack - no items and no photos - has
    nothing to put on a box, so it's skipped rather than printed blank."""
    packs = {p["id"]: p for p in db.packs_for_kit(session_id)}
    items_by_pack = {}
    for it in db.items_for_kit(session_id):
        items_by_pack.setdefault(it["pack_id"], []).append(it)
    photo_packs = {u["pack_id"] for u in db.uploads_for_session(session_id)}

    seen = set()
    selected, unknown = [], []
    for pid in pack_ids:
        if pid in seen:
            continue
        seen.add(pid)
        if pid in packs:
            selected.append(packs[pid])
        else:
            unknown.append(pid)
    selected.sort(key=lambda p: p["pack_number"])
    printable = [
        (p, items_by_pack.get(p["id"], [])) for p in selected
        if items_by_pack.get(p["id"]) or p["id"] in photo_packs
    ]
    printable_ids = {p["id"] for p, _ in printable}
    skipped = [p["pack_number"] for p in selected if p["id"] not in printable_ids]
    return printable, skipped, unknown


def _label_pages(kit, printable, cfg):
    """Every label for these packs, in order (a pack whose items don't fit
    on one label gets several). Raises labels.LabelPrintError if the
    configured label is too small to lay out."""
    pages = []
    for pack, items in printable:
        pages.extend(labels.layout(labels.label_content(kit["kit_name"], pack["pack_number"], items), cfg))
    return pages


def _packs_text(numbers):
    """[1, 2, 4] -> "Packs 1, 2 & 4"; [3] -> "Pack 3"."""
    word = "Pack" if len(numbers) == 1 else "Packs"
    return f"{word} {kits.join_and(str(n) for n in numbers)}"


@app.route("/api/kit/rename", methods=["POST"])
@auth.login_required
def api_kit_rename():
    """Renames an open kit (the ✏️ next to its name on the kit page). Anyone
    on the kit may - it's shared, like adding a pack. A submitted kit is
    reopened first. The new name shows on every phone at its next poll (rev
    bump) and on labels printed from now on; the Drive Sheet follows on the
    next Final Submit, renamed rather than duplicated (see
    db.sheet_kit_name / drive_sync._log_kit)."""
    data, sess = _kit_request()
    if sess is None:
        return _kit_missing()
    name, norm = kits.normalize_kit_name(data.get("kit_name") if isinstance(data.get("kit_name"), str) else "")
    if not name:
        return _kit_error(sess, "invalid", "Enter the New Store Kit name.", 400)
    if len(name) > KIT_NAME_MAX_LEN:
        return _kit_error(
            sess, "invalid", f"The New Store Kit name is too long - keep it to {KIT_NAME_MAX_LEN} characters.", 400
        )
    raw_job = data.get("job_number")
    new_job = sess["job_number"]
    if isinstance(raw_job, str) and raw_job.strip():
        new_job = sanitize_for_filename(raw_job.strip()).upper()
        if not JOB_NUMBER_RE.match(new_job):
            return _kit_error(sess, "invalid", 'Job Number must be "J" followed by exactly 6 digits - e.g. J457008.', 400)
    if new_job != sess["job_number"]:
        return _kit_change_job(sess, new_job, name, norm)
    status = db.rename_kit(sess["id"], name, norm)
    if status == "missing":
        return _kit_missing()
    if status == "finalized":
        return _kit_finalized_error(sess, "This kit was submitted - reopen it to change its name.")
    if status == "taken":
        return _kit_error(sess, "name_taken", f'{sess["job_number"]} already has a kit called "{name}".', 409)
    if status == "sheet_taken":
        holder = db.kit_holding_sheet_name(sess["job_number"], norm, exclude_session_id=sess["id"])
        return _kit_error(sess, "name_taken", _sheet_name_taken_text(sess["job_number"], name, holder), 409)
    message = "Kit name unchanged." if status == "same" else f'Kit renamed to "{name}".'
    return _kit_ok(sess, message=message)


def _kit_change_job(sess, new_job, name, norm):
    """Moves an open kit to another job number (and renames it, if `name`
    changed too) - the kit page's edit pane. Everyone on the kit sees the new
    job number at their next poll. The photo files move on disk first, then
    the database in one transaction (db.edit_kit); if that refuses, the files
    go back. Drive follows on the next Final Submit: its Pack Log, labels PDF
    and any photos an earlier submission put in "<old job>/Packing Photos"
    move to the new job's folders - their links (by file id) stay the same."""
    kit = db.get_kit(sess["id"])
    if kit is None:
        return _kit_missing()
    if sess["finalized_at"]:
        return _kit_finalized_error(sess, "This kit was submitted - reopen it to change its job number.")
    other = db.find_kit(new_job, norm)
    if other is not None and other["session_id"] != sess["id"]:
        return _kit_error(sess, "name_taken", f'{new_job} already has a kit called "{name}".', 409)
    holder = db.kit_holding_sheet_name(new_job, norm, exclude_session_id=sess["id"])
    if holder is not None:
        return _kit_error(sess, "name_taken", _sheet_name_taken_text(new_job, name, holder), 409)

    old_job, category = sess["job_number"], NEW_STORE_KITS_CATEGORY
    db.ensure_job(new_job)
    try:
        moved = _move_upload_files(db.uploads_for_session(sess["id"]), old_job, new_job, category)
    except OSError as exc:
        return _kit_error(sess, "move_failed", f"Couldn't move this kit's photos to {new_job}: {exc}", 409)

    status = db.edit_kit(sess["id"], new_job, name, norm)
    if status != "changed":
        _move_files_back(moved)
        if status == "missing":
            return _kit_missing()
        if status == "finalized":
            return _kit_finalized_error(sess, "This kit was submitted - reopen it to change its job number.")
        if status == "sheet_taken":
            holder = db.kit_holding_sheet_name(new_job, norm, exclude_session_id=sess["id"])
            return _kit_error(sess, "name_taken", _sheet_name_taken_text(new_job, name, holder), 409)
        if status == "taken":
            return _kit_error(sess, "name_taken", f'{new_job} already has a kit called "{name}".', 409)
        return _kit_ok(sess, message="Nothing to change.")

    # A photo that finished uploading into the old folder while this ran.
    _move_upload_files(db.uploads_for_session(sess["id"]), old_job, new_job, category, strict=False)
    _cleanup_empty_job_dir(old_job, category)
    sess = db.get_session(sess["id"])
    message = f"Kit moved to {new_job}" + (f' and renamed to "{name}"' if name != kit["kit_name"] else "") + "."
    if (kit["submit_count"] or 0) > 0:
        message += " Its Drive files move there at the next Final Submit."
    return _kit_ok(sess, message=message)


@app.route("/api/kit/labels/print", methods=["POST"])
@auth.login_required
def api_kit_labels_print():
    """Prints the selected packs' labels on the server's label printer, as
    ONE spooler job (so a batch comes out together, in pack order). Anyone
    logged in may print, for submitted kits too - a box can need a fresh
    label at any time - and a pack someone else is still packing prints as
    it is right now (the page asks first). Without a printer configured
    (LABEL_PRINTER_NAME in .env) the answer is printer_not_configured with a
    previewUrl, so the page can open the labels as a printable web page."""
    data, sess = _kit_request()
    if sess is None:
        return _kit_missing()
    kit = db.get_kit(sess["id"])
    if kit is None:
        return _kit_missing()
    raw_ids = data.get("pack_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        return _kit_error(sess, "invalid", "Select the packs to print first.", 400)
    pack_ids, bad_ids = [], []
    for raw in raw_ids:
        pid = _int_or_none(raw)
        (pack_ids if pid is not None else bad_ids).append(pid if pid is not None else raw)

    printable, skipped, unknown = _label_selection(sess["id"], pack_ids)
    unknown = unknown + bad_ids
    if not printable:
        message = (
            "Nothing to print - the selected packs are empty." if skipped
            else "Nothing to print - the selected packs no longer exist."
        )
        return _kit_error(sess, "invalid", message, 400, skipped=skipped, unknown=unknown)

    cfg = load_label_config()
    if not cfg["printerName"]:
        ids = ",".join(str(p["id"]) for p, _ in printable)
        return _kit_error(
            sess, "printer_not_configured",
            "No label printer is set up on the server yet - opening a preview instead.", 409,
            previewUrl=f"/session/{sess['id']}/labels?packs={ids}",
        )

    try:
        pages = _label_pages(kit, printable, cfg)
        job_id = labels.send_raw(
            labels.to_zpl(pages, cfg).encode("utf-8"), cfg["printerName"],
            doc_name=f"{kit['kit_name']} - Pack labels",
        )
    except labels.LabelPrintError as exc:
        return _kit_error(sess, "print_failed", str(exc), 502)
    except Exception as exc:  # noqa: BLE001 - never a 500 over a printer
        print(f"[labels] printing {kit['job_number']} / {kit['kit_name']} failed: {exc!r}")
        return _kit_error(sess, "print_failed", f"Printing failed: {exc}", 502)

    printed = [p["pack_number"] for p, _ in printable]
    db.mark_packs_printed(sess["id"], [p["id"] for p, _ in printable], g.user["name"])
    message = f"Printed {len(pages)} label{'' if len(pages) == 1 else 's'} ({_packs_text(printed)})."
    if skipped:
        message += f" Skipped empty {_packs_text(skipped)}."
    if unknown:
        message += f" {len(unknown)} selected pack(s) no longer exist."
    return _kit_ok(
        sess, printed=printed, skipped=skipped, unknown=unknown, labelCount=len(pages), jobId=job_id,
        message=message,
    )


@app.route("/session/<session_id>/labels")
@auth.login_required
def labels_preview(session_id):
    """The labels as a web page - what Print opens while no label printer is
    configured, and a way to check a layout on screen. ?packs=<id,id,...>
    (default: every non-empty pack). Printing this page from the browser
    onto the label stock gives the same labels; the raw ZPL is at the
    bottom for checking what the printer would get."""
    sess = db.get_session(session_id)
    if not sess or sess["category"] != NEW_STORE_KITS_CATEGORY:
        abort(404)
    kit = db.get_kit(session_id)
    if kit is None:
        abort(404)
    raw = (request.args.get("packs") or "").strip()
    if raw:
        pack_ids = [int(x) for x in raw.split(",") if re.fullmatch(r"[0-9]+", x.strip())]
    else:
        pack_ids = [p["id"] for p in db.packs_for_kit(session_id)]
    printable, skipped, unknown = _label_selection(session_id, pack_ids)

    cfg = load_label_config()
    error = None
    try:
        pages = _label_pages(kit, printable, cfg)
    except labels.LabelPrintError as exc:
        pages, error = [], str(exc)
    return render_template(
        "labels_preview.html",
        session=sess,
        kit=kit,
        cfg=cfg,
        size_text=labels.size_text(cfg),
        printer_configured=bool(cfg["printerName"]),
        printer_name=cfg["printerName"],
        pack_numbers=[p["pack_number"] for p, _ in printable],
        skipped=skipped,
        unknown=unknown,
        label_count=len(pages),
        label_html=labels.to_html(pages, cfg) if pages else "",
        zpl=labels.to_zpl(pages, cfg) if pages else "",
        error=error,
        back_url=f"/session/{session_id}",
    )


@app.route("/gallery/<job_number>")
@auth.login_required
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
    except (FileNotFoundError, NotFound):
        # send_from_directory reports a missing file as werkzeug's NotFound,
        # not FileNotFoundError - catching only the latter meant a photo whose
        # local copy local_cleanup had already removed just 404'd instead of
        # falling back to its Drive copy.
        pass
    # No thumbnail could be made for this photo (an image Pillow can't read -
    # api_upload then records the full-size file's own name as its thumbnail):
    # the full image stands in for it, as api_upload intends.
    try:
        return send_from_directory(job_dir(job_number, category), filename)
    except (FileNotFoundError, NotFound):
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
    except (FileNotFoundError, NotFound):  # see media_thumb
        row = db.get_upload_by_filename(job_number, category, filename)
        if row and row["drive_file_id"]:
            return redirect(_drive_view_url(row["drive_file_id"]))
        abort(404)


if __name__ == "__main__":
    db.init_db()
    auth.ensure_bootstrap_admin()
    if load_drive_config():
        print("[startup] Drive sync configured - background relay starting.")
    else:
        print(
            "[startup] Drive sync NOT configured yet (.env missing or incomplete - see "
            ".env.example). Photos will save locally only until you deploy "
            "apps-script/DriveUploader.gs and fill that file in."
        )
    for line in labels.startup_lines():
        print(line)
    drive_sync.start_background_sync()
    local_cleanup.start_background_cleanup()
    app.run(host=HOST, port=PORT, threaded=True)
