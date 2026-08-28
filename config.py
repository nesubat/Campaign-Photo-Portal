"""
Central configuration for the Campaign Photo Portal.
Change values here rather than hunting through app.py.
"""
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = BASE_DIR / "uploads"
DB_PATH = DATA_DIR / "portal.db"
EMPLOYEES_FILE = DATA_DIR / "employees.json"
DRIVE_CONFIG_FILE = DATA_DIR / "drive_config.json"

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
    Google Drive relay settings, filled in after you deploy apps-script/DriveUploader.gs.
    Returns None if not configured yet (Drive sync is skipped, local copy still works).
    """
    if not DRIVE_CONFIG_FILE.exists():
        return None
    with open(DRIVE_CONFIG_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if not cfg.get("webAppUrl") or not cfg.get("sharedSecret"):
        return None
    return cfg
