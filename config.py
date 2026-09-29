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
# as the Drive subfolder name (unless DRIVE_FOLDER_OVERRIDES below says otherwise).
CATEGORIES = {
    "packing": "Packing Photos",
    "dispatch": "Dispatch Photos",
    "new_store_kits": "New Store Kits",
}

# The one category that works as a shared, multi-person "kit" instead of a
# one-person batch - see kits.py and db.py's kits/kit_packs/kit_pack_items
# tables. Picking it on the start form asks for a kit name too, and everyone
# who starts the same job + kit name lands in the SAME session, packing
# numbered packs side by side.
NEW_STORE_KITS_CATEGORY = "new_store_kits"

# Drive folder name to use for a category instead of its CATEGORIES label.
# Kit photos are packing photos as far as the office is concerned - they
# belong in the job's existing "Packing Photos" folder, not a new folder per
# kit or pack (the user's explicit instruction). Locally they still live under
# uploads/<Job>/new_store_kits/ so the two kinds of session never mix on disk;
# only the Drive side is merged.
DRIVE_FOLDER_OVERRIDES = {"new_store_kits": "Packing Photos"}

# Longest New Store Kit name accepted on the start form (after trimming and
# collapsing spaces). It becomes part of a Drive Sheet's name and the Active
# Sessions card title, so a pasted paragraph shouldn't make it through.
KIT_NAME_MAX_LEN = 60

# A pack is reserved for whoever is editing it until they save it or move out
# of it - reservations never expire on their own, since "Mike's phone is in
# his pocket while he tapes the box" is normal, not abandonment. Once the
# holder's last heartbeat (every poll from their open page) is older than
# this, another collaborator MAY take the pack over - only deliberately, after
# a "Mike has been idle for 14 min on Pack 3. Take it over?" confirm. That's
# how a forgotten phone stops blocking Final Submit for everyone else.
PACK_IDLE_TAKEOVER_SEC = 600

# How often (seconds) an open kit page asks the server for the latest kit
# state. Doubles as the holder's heartbeat for the pack they're editing, so it
# must stay well under PACK_IDLE_TAKEOVER_SEC.
KIT_POLL_INTERVAL_SEC = 4

# The kit's Pack Log Sheet lists every pack's photo links, so it's written
# once every submitted kit photo has reached Drive. If some still haven't this
# many minutes after Final Submit (a slow hotspot, one bad file), a partial
# Sheet is written anyway so the office isn't left with nothing...
KIT_SHEET_FORCE_AFTER_MIN = 30

# ...and rewritten at most this often (minutes) while photos keep trickling
# in - only when the number of synced photos has actually changed since the
# last write, so a photo that can never sync doesn't cause a Sheet rewrite
# every few minutes forever...
KIT_SHEET_PARTIAL_RETRY_MIN = 10

# ...until this many hours after Final Submit, when the last write is treated
# as final and the kit records "N photo(s) never reached Drive" instead of
# retrying indefinitely.
KIT_SHEET_GIVE_UP_HOURS = 24

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


# Pack labels (labels.py) - printed on the Zebra ZD420d connected to the
# server machine. The real label stock and the printer's Windows name are
# only known once someone is at that machine, so they live in .env (see
# .env.example) rather than here; these are the fallbacks until then: the
# standard 100 x 150 mm (4 x 6 in) courier label, at the ZD420d's 203 dpi
# (a 300 dpi variant of the printer exists - set LABEL_DPI=300 for that one).
LABEL_DEFAULTS = {"widthMm": 100.0, "heightMm": 150.0, "dpi": 203, "marginMm": 4.0}

# The label font: Arial Black, the TrueType file every Windows install has.
# The Zebra has no Arial Black of its own, so labels.py draws each label with
# this file on the server and sends it to the printer as an image - which is
# also what the preview page shows. LABEL_FONT_FILE in .env points at another
# .ttf; if the file can't be loaded, labels fall back to the printer's own
# built-in font (with a warning) rather than failing to print.
LABEL_FONT_FILE_DEFAULT = r"C:\Windows\Fonts\ariblk.ttf"

# How the label reads on the stock (LABEL_ORIENTATION in .env). LABEL_WIDTH_MM
# is always the stock's width ACROSS the printer (100 mm for the courier
# label), LABEL_HEIGHT_MM its length along the feed; "landscape" lays the
# label out with the long side horizontal - on the 100 x 150 courier label
# that's 150 x 100 mm, turned 90 degrees for the printer; stock that's
# already wider than long (100 x 50) prints as it feeds. If the labels come
# out upside down for the way the boxes are handled, "landscape-flipped"
# turns them the other way; "portrait" = the stock as it feeds.
LABEL_ORIENTATIONS = ("landscape", "landscape-flipped", "portrait")
LABEL_ORIENTATION_DEFAULT = "landscape"

# The widest stock the ZD420d's print head can take: 104 mm (4.09 in), plus
# a little slack for rounding - anything wider was almost certainly typed
# as the label reads (150 x 100) rather than as it feeds (100 x 150).
LABEL_MAX_HEAD_WIDTH_MM = 108.0
# (env var, key, lowest accepted, highest accepted, int?) - a value outside
# its range is almost certainly a typo (e.g. inches typed where mm belong),
# and printing on the wrong size is worse than printing on the default.
_LABEL_NUMBER_SETTINGS = (
    ("LABEL_WIDTH_MM", "widthMm", 10.0, 300.0, False),
    ("LABEL_HEIGHT_MM", "heightMm", 10.0, 1000.0, False),
    ("LABEL_DPI", "dpi", 100, 1200, True),
    ("LABEL_MARGIN_MM", "marginMm", 0.0, 50.0, False),
)

# Bad-value warnings already printed - load_label_config runs on every kit
# page poll (for "printerConfigured"), and one typo in .env shouldn't print
# the same warning every 4 seconds per phone.
_label_warnings_shown = set()


def _mm(value):
    return f"{value:g}"


def _label_warning(message):
    if message not in _label_warnings_shown:
        _label_warnings_shown.add(message)
        print(f"[labels] WARNING: {message}")


def load_label_config():
    """
    Label printer settings, read from .env at call time (like
    load_drive_config), so the defaults above apply until the real stock and
    printer name are filled in on the server:

        {"printerName": "ZDesigner ZD420-203dpi ZPL" | "" (not configured),
         "widthMm": 100.0, "heightMm": 150.0, "dpi": 203, "marginMm": 4.0,
         "fontFile": "C:/Windows/Fonts/ariblk.ttf" (LABEL_FONT_FILE, default Arial Black),
         "orientation": "landscape" | "landscape-flipped" | "portrait" (LABEL_ORIENTATION)}

    Never raises: a number that doesn't parse, or is out of range, falls
    back to its default with a printed warning - a typo in .env must not
    take the portal (or the kit page's poll) down with it. An empty printer
    name means "not configured": Print then opens a preview page instead.
    """
    cfg = {"printerName": (os.environ.get("LABEL_PRINTER_NAME") or "").strip().strip('"').strip()}
    for env_name, key, lowest, highest, as_int in _LABEL_NUMBER_SETTINGS:
        default = LABEL_DEFAULTS[key]
        raw = (os.environ.get(env_name) or "").strip()
        if not raw:
            cfg[key] = default
            continue
        try:
            value = float(raw)
            if as_int:
                if value != int(value):
                    raise ValueError
                value = int(value)
        except ValueError:
            _label_warning(f"{env_name}={raw!r} in .env isn't a number - using {default}.")
            cfg[key] = default
            continue
        if not (lowest <= value <= highest):
            _label_warning(f"{env_name}={raw!r} in .env is out of range ({lowest}-{highest}) - using {default}.")
            cfg[key] = default
            continue
        cfg[key] = value
    # LABEL_WIDTH_MM is across the print head, so it can't be wider than it.
    if cfg["widthMm"] > LABEL_MAX_HEAD_WIDTH_MM:
        if cfg["heightMm"] <= LABEL_MAX_HEAD_WIDTH_MM:
            _label_warning(
                f"LABEL_WIDTH_MM={_mm(cfg['widthMm'])} is wider than the printer's head - it's the stock's width "
                f"ACROSS the printer, so using {_mm(cfg['heightMm'])} x {_mm(cfg['widthMm'])} mm "
                "(swap LABEL_WIDTH_MM and LABEL_HEIGHT_MM in .env)."
            )
            cfg["widthMm"], cfg["heightMm"] = cfg["heightMm"], cfg["widthMm"]
        else:
            _label_warning(
                f"LABEL_WIDTH_MM={_mm(cfg['widthMm'])} is wider than the printer's head "
                f"({_mm(LABEL_MAX_HEAD_WIDTH_MM)} mm max) - using {_mm(LABEL_DEFAULTS['widthMm'])}."
            )
            cfg["widthMm"] = LABEL_DEFAULTS["widthMm"]
    # The margins have to leave something to print on.
    if cfg["marginMm"] * 2 >= min(cfg["widthMm"], cfg["heightMm"]) * 0.8:
        _label_warning(
            f"LABEL_MARGIN_MM={cfg['marginMm']} leaves no room on a {cfg['widthMm']} x {cfg['heightMm']} mm "
            f"label - using {LABEL_DEFAULTS['marginMm']}."
        )
        cfg["marginMm"] = min(LABEL_DEFAULTS["marginMm"], min(cfg["widthMm"], cfg["heightMm"]) * 0.1)
    cfg["fontFile"] = (os.environ.get("LABEL_FONT_FILE") or "").strip().strip('"').strip() or LABEL_FONT_FILE_DEFAULT
    orientation = (os.environ.get("LABEL_ORIENTATION") or "").strip().lower() or LABEL_ORIENTATION_DEFAULT
    if orientation not in LABEL_ORIENTATIONS:
        _label_warning(f"LABEL_ORIENTATION={orientation!r} in .env isn't one of {', '.join(LABEL_ORIENTATIONS)} - "
                       f"using {LABEL_ORIENTATION_DEFAULT}.")
        orientation = LABEL_ORIENTATION_DEFAULT
    cfg["orientation"] = orientation
    return cfg
