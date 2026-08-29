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

# Photo category, chosen once per session on the start form. Slug (key) is used for
# local folder names and stored in the DB; label (value) is shown in the UI and used
# as the Drive subfolder name.
CATEGORIES = {
    "packing": "Packing Photos",
    "dispatch": "Dispatch Photos",
}

# How many days after a photo is confirmed synced to Drive before its local copy
# (full-res + thumbnail) is deleted to free disk space.
LOCAL_CLEANUP_AFTER_DAYS = 2

# How often (seconds) the background worker checks for synced photos old enough to clean up.
LOCAL_CLEANUP_INTERVAL_SEC = 3600

# Server bind settings. 0.0.0.0 so phones on the hotspot/LAN can reach it.
HOST = "0.0.0.0"
PORT = 5000


def load_employees():
    """Preset name list for the dropdown. Edit data/employees.json to change it."""
    if not EMPLOYEES_FILE.exists():
        return []
    with open(EMPLOYEES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


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
