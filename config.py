"""
Central configuration for the Campaign Photo Portal.
Change values here rather than hunting through app.py.
"""
import json
import os
from pathlib import Path

from dotenv import load_dotenv, set_key

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(ENV_PATH)  # DRIVE_WEBAPP_URL / DRIVE_SHARED_SECRET - see .env.example

DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = BASE_DIR / "uploads"
DB_PATH = DATA_DIR / "portal.db"
EMPLOYEES_FILE = DATA_DIR / "employees.json"  # legacy name list - one-time imported into `users`, see db._migrate

# Flask session signing key - required for login to work. Set a long random
# string in .env; sessions become invalid (everyone logged out) if this changes.
SECRET_KEY = os.environ.get("SECRET_KEY", "")

# How long a login lasts before a phone needs to log in again. Long by design
# (a year) - these are shared/shift phones, not personal devices, so "stay
# logged in until someone taps Log out" is the expected behavior, not a
# security compromise. See auth.login_user, which marks the session permanent
# so this actually takes effect (a non-permanent Flask session cookie has no
# expiry at all and gets dropped the moment the phone's browser process ends,
# which is exactly the "logged out again after reopening the app" symptom
# this replaces).
SESSION_LIFETIME_DAYS = 365

# First Admin account, created on startup if it doesn't already exist yet -
# see auth.ensure_bootstrap_admin. ADMIN_PIN doubles as a live mirror of that
# account's current PIN (see update_admin_pin_in_env, called from
# auth.set_pin_and_sync whenever the Admin account's PIN changes) - a
# deliberate "break glass" recovery path in plaintext here, since there's no
# other admin around to reset the sole Admin account if you're the only one.
ADMIN_NAME = os.environ.get("ADMIN_NAME", "").strip()
ADMIN_PIN = os.environ.get("ADMIN_PIN", "").strip()

# Failed PIN attempts before an account locks (only an admin PIN reset clears it).
LOGIN_MAX_ATTEMPTS = 5

# Thumbnail size for the on-page gallery grid (full-res original is always kept).
THUMB_MAX_PX = 480

# How often (seconds) the background worker checks for photos not yet synced to Drive.
DRIVE_SYNC_INTERVAL_SEC = 5

# Photo uploads are batched into one Apps Script call per group of photos,
# rather than one call per photo - grouped by job+category first (a batch
# can only ever hold files bound for the same Drive subfolder), then packed
# up to whichever of these two limits is hit first:
#
# The SIZE cap is the one that actually matters: real phone photos run
# 3.6-8MB each, so a count-only cap can silently balloon into a huge request
# (20 photos x ~6MB average = ~115MB raw, ~155MB once base64-encoded) that
# fails outright with a write-timeout on anything but a fast, stable
# connection - this is exactly what happened in practice, not a hypothetical.
DRIVE_UPLOAD_BATCH_MAX_BYTES = 20 * 1024 * 1024  # ~20MB of raw file data per batch

# The COUNT cap is just a fallback in case photos are unusually small -
# without it, hundreds of tiny files could still pile into one oversized
# request even while staying under the byte cap.
DRIVE_UPLOAD_BATCH_MAX_COUNT = 20

# Sheet updates are batched the same way, by consignment count - the payload
# there is just text (item IDs, names, links), so there's no photo-size risk
# and no separate byte cap is needed.
DRIVE_SHEET_BATCH_CAP = 20

# Timeout for a single batched call. A batch capped at ~20MB raw (~27MB
# base64) needs well under a minute even on a slow (~5 Mbps) connection, so
# this keeps a generous multiple of that as margin, still well under Apps
# Script's own execution limit (6 min on a plain Google account, 30 min on
# Workspace).
DRIVE_BATCH_TIMEOUT_SEC = 240

# Photo category, chosen once per session on the start form. Slug (key) is used for
# local folder names and stored in the DB; label (value) is shown in the UI and used
# as the Drive subfolder name.
CATEGORIES = {
    "packing": "Packing Photos",
    "dispatch": "Dispatch Photos",
}

# Only this category ever offers the "keep logs" option (Consignment #/Store
# name, Item ID, Google Sheet) - see db.py's `consignments` table and
# drive_sync.py. Whether it's actually used for a given session is a
# separate, per-session choice (sessions.keep_logs).
CONSIGNMENT_LOGGING_CATEGORY = "packing"

# How long (hours) a not-yet-submitted session stays auto-resumable - see
# db.find_open_session. Re-entering the same job number + category within
# this window (browser back button, app/server restart, phone locked and
# reopened) lands back on the same in-progress batch instead of starting a
# new one; past it, the old session is treated as abandoned and a fresh one
# starts instead (most likely a genuinely separate batch, e.g. the next day).
SESSION_RESUME_WINDOW_HOURS = 24

# How many days after a photo is confirmed synced to Drive before its local copy
# (full-res + thumbnail) is deleted to free disk space.
LOCAL_CLEANUP_AFTER_DAYS = 2

# How often (seconds) the background worker checks for synced photos old enough to clean up.
LOCAL_CLEANUP_INTERVAL_SEC = 3600

# Server bind settings. 0.0.0.0 so phones on the hotspot/LAN can reach it.
HOST = "0.0.0.0"
PORT = 5000

# waitress worker threads (serve.py only - the dev server in app.py doesn't
# use this). Sized with headroom above "number of simultaneous users": one
# person selecting several photos from their gallery fires all of those
# uploads as concurrent requests, not one at a time, so real concurrent
# request count can exceed the user count by a few times over.
WAITRESS_THREADS = 16


def update_admin_pin_in_env(new_pin):
    """Keeps .env's ADMIN_PIN mirroring the Admin account's live PIN. Only
    called for the ADMIN_NAME account (see auth.set_pin_and_sync) - other
    admins stay hash-only/reset-by-another-admin, same as standard users."""
    if ENV_PATH.exists():
        set_key(str(ENV_PATH), "ADMIN_PIN", new_pin, quote_mode="never")


def load_drive_config():
    """
    Google Drive relay settings, read from .env (see .env.example) once you've
    deployed apps-script/DriveUploader.gs. Returns None if not configured yet
    (Drive sync is skipped, local copy still works).
    """
    web_app_url = (os.environ.get("DRIVE_WEBAPP_URL") or "").strip()
    shared_secret = (os.environ.get("DRIVE_SHARED_SECRET") or "").strip()
    if not web_app_url or not shared_secret:
        return None
    return {"webAppUrl": web_app_url, "sharedSecret": shared_secret}
