"""
Central configuration for the Campaign Photo Portal.
Change values here rather than hunting through app.py.
"""
import json
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")  # DRIVE_WEBAPP_URL / DRIVE_SHARED_SECRET - see .env.example

DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = BASE_DIR / "uploads"
DB_PATH = DATA_DIR / "portal.db"
EMPLOYEES_FILE = DATA_DIR / "employees.json"

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


def load_employees():
    """Preset name list for the dropdown. Edit data/employees.json to change it."""
    if not EMPLOYEES_FILE.exists():
        return []
    with open(EMPLOYEES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_employees(names):
    EMPLOYEES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(EMPLOYEES_FILE, "w", encoding="utf-8") as f:
        json.dump(names, f, indent=2)
        f.write("\n")


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
