"""
SQLite storage for the Campaign Photo Portal.
One connection per request (Flask app_context), WAL mode so uploads from
several phones at once don't block each other.

Transactions (read this before adding a function that writes): Python's
sqlite3 silently opens a transaction before ANY INSERT/UPDATE/DELETE - even
one that matches no rows - and that transaction holds SQLite's single write
lock until it's committed or rolled back. Connections here are per-thread and
reused by waitress's worker threads, so a function that returns without
committing leaves the lock held by an idle worker and every other writer
(uploads, heartbeats, the Drive sync thread) waits out the 30s busy timeout
and fails with "database is locked". The older functions below commit
unconditionally; every New Store Kit function instead wraps its whole body
in `with conn:` (commit on success, rollback on exception - on EVERY path,
including "nothing matched"), and multi-step kit operations are built from
non-committing `_helper(conn, ...)` functions so they commit exactly once.
"""
import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from config import DB_PATH, EMPLOYEES_FILE, NEW_STORE_KITS_CATEGORY, SESSION_RESUME_WINDOW_HOURS

_local = threading.local()


def get_conn():
    conn = getattr(_local, "conn", None)
    if conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            job_number   TEXT PRIMARY KEY,
            created_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id            TEXT PRIMARY KEY,
            job_number    TEXT NOT NULL,
            employee_name TEXT NOT NULL,
            started_at    TEXT NOT NULL,
            finalized_at  TEXT
        );

        CREATE TABLE IF NOT EXISTS uploads (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id     TEXT NOT NULL,
            job_number     TEXT NOT NULL,
            employee_name  TEXT NOT NULL,
            filename       TEXT NOT NULL,
            thumb_filename TEXT NOT NULL,
            uploaded_at    TEXT NOT NULL,
            status         TEXT NOT NULL DEFAULT 'staged',   -- staged | finalized
            drive_status   TEXT NOT NULL DEFAULT 'pending',  -- pending | synced | error
            drive_file_id  TEXT,
            drive_error    TEXT,
            retry_after    TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_uploads_job ON uploads(job_number);
        CREATE INDEX IF NOT EXISTS idx_uploads_session ON uploads(session_id);
        CREATE INDEX IF NOT EXISTS idx_uploads_drive_pending ON uploads(drive_status);

        -- One row per Consignment #/Store name a "keep logs" session has scanned,
        -- scoped to a job+category. Mirrored to a Google Sheet in that job's Drive
        -- folder; item_ids/contributors are comma-separated, deduped, append-only
        -- lists (see split_list/_join_list below). photo_count is display-only
        -- (shown as "N photos already logged") - it has no bearing on filenames,
        -- which are always employee-timestamp regardless of logging.
        CREATE TABLE IF NOT EXISTS consignments (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            job_number    TEXT NOT NULL,
            category      TEXT NOT NULL,
            key_type      TEXT NOT NULL,           -- 'consignment' | 'store'
            key_value     TEXT NOT NULL,           -- as scanned/typed, for display
            key_norm      TEXT NOT NULL,           -- trimmed+casefolded, for lookups
            item_ids      TEXT NOT NULL DEFAULT '',
            contributors  TEXT NOT NULL DEFAULT '',
            photo_count   INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_consignments_key
            ON consignments(job_number, category, key_norm);

        -- One row per (job_number, category) once its Sheet has had its
        -- columns auto-fit - see job_fully_synced/mark_sheet_resized below.
        -- Guards the resize from firing more than once per job, since it's
        -- otherwise a no-op check run only when a job just finished syncing.
        CREATE TABLE IF NOT EXISTS sheet_resized (
            job_number  TEXT NOT NULL,
            category    TEXT NOT NULL,
            resized_at  TEXT NOT NULL,
            PRIMARY KEY (job_number, category)
        );

        -- Login accounts. pin_hash IS NULL means "must choose a PIN" - true
        -- for a brand-new account (self-signup or admin-created) and, again,
        -- right after an admin resets someone's forgotten PIN - both cases
        -- land on the same "choose your PIN" screen. name_norm is the
        -- trimmed+casefolded form used for case-insensitive uniqueness,
        -- mirroring consignments.key_norm above. active doubles as
        -- "approved by an admin" - a self-signup account starts at 0 and
        -- can't choose a PIN (so can't log in) until an admin approves it;
        -- an admin creating a user directly sets it to 1 immediately, since
        -- that IS the approval. There's no "disable an existing account"
        -- path anymore - see delete_user for that.
        CREATE TABLE IF NOT EXISTS users (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL,
            name_norm       TEXT NOT NULL,
            pin_hash        TEXT,
            role            TEXT NOT NULL DEFAULT 'standard',
            failed_attempts INTEGER NOT NULL DEFAULT 0,
            locked_at       TEXT,
            active          INTEGER NOT NULL DEFAULT 1,
            created_at      TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_users_name_norm ON users(name_norm);

        -- New Store Kits (config.NEW_STORE_KITS_CATEGORY). A kit is ONE shared
        -- sessions row that every collaborator works inside, plus this row
        -- keyed by that session id. Identified within a job by kit_norm (the
        -- casefolded, space-collapsed name - see kits.normalize_kit_name), so
        -- "Store 12" and "store  12" typed on two phones are the same kit.
        -- rev is bumped by every kit change (see _bump_rev): phones use it to
        -- ignore an out-of-order older state, and Final Submit only goes
        -- through if nothing changed since the confirmation dialog was shown.
        -- sheet_* track the kit's Pack Log Sheet (drive_sync.py), written once
        -- per kit rather than per upload like the consignment log.
        CREATE TABLE IF NOT EXISTS kits (
            session_id         TEXT PRIMARY KEY,
            job_number         TEXT NOT NULL,
            kit_name           TEXT NOT NULL,
            kit_norm           TEXT NOT NULL,
            created_by         TEXT NOT NULL,
            created_at         TEXT NOT NULL,
            rev                INTEGER NOT NULL DEFAULT 0,
            finalized_by       TEXT,
            sheet_status       TEXT NOT NULL DEFAULT 'not_applicable',  -- not_applicable | pending | synced | error
            sheet_error        TEXT,
            sheet_retry_after  TEXT,
            sheet_synced_at    TEXT,
            sheet_synced_count INTEGER NOT NULL DEFAULT 0   -- synced photos included in the last Sheet write
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_kits_key ON kits(job_number, kit_norm);

        -- Numbered packs (physical boxes) inside a kit. pack_number is MAX+1
        -- and never renumbered - boxes may already be labelled "Pack 3".
        -- reserved_by is whoever is editing the pack right now (NULL = saved/
        -- free); it's sticky until they move out of the pack - see
        -- reserve_pack. reserved_ts is their last heartbeat as unix epoch
        -- seconds rather than ISO text, so the AEDT/AEST changeover can't
        -- break the "idle for 10 minutes" comparison. contributors is a
        -- comma-joined, deduped list like consignments.contributors.
        CREATE TABLE IF NOT EXISTS kit_packs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id    TEXT NOT NULL,
            pack_number   INTEGER NOT NULL,
            reserved_by   TEXT,
            reserved_ts   INTEGER,
            contributors  TEXT NOT NULL DEFAULT '',
            created_by    TEXT NOT NULL,
            created_at    TEXT NOT NULL,
            first_edit_at TEXT,
            last_edit_at  TEXT
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_kit_packs_number ON kit_packs(session_id, pack_number);

        -- One row per item number entered into a pack (see kits.parse_item):
        -- series is the display form ("J456789" or a free-text description),
        -- series_norm what duplicates are judged on, idx the normalized index
        -- ('' = entered without one). The same item may appear in two packs,
        -- but only once per pack (the unique index below).
        CREATE TABLE IF NOT EXISTS kit_pack_items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            pack_id     INTEGER NOT NULL,
            session_id  TEXT NOT NULL,
            series      TEXT NOT NULL,
            series_norm TEXT NOT NULL,
            idx         TEXT NOT NULL DEFAULT '',
            added_by    TEXT NOT NULL,
            added_at    TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_kit_pack_items_key ON kit_pack_items(pack_id, series_norm, idx);
        CREATE INDEX IF NOT EXISTS idx_kit_pack_items_session ON kit_pack_items(session_id);
        """
    )
    conn.commit()
    _migrate(conn)
    if sqlite3.sqlite_version_info < (3, 35, 0):
        # The kit functions below use UPDATE/DELETE ... RETURNING (SQLite 3.35,
        # 2021 - bundled with every current Python) to release packs in one
        # statement. An older Python would fail every kit action instead.
        print(
            f"[startup] WARNING: SQLite {sqlite3.sqlite_version} is too old for New Store Kits "
            "(needs 3.35+) - upgrade Python."
        )


def _add_column_if_missing(conn, table, column, ddl):
    """True if the column had to be added (i.e. this database predates it)."""
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
        return True
    return False


def _migrate(conn):
    # category: existing rows predate the packing/dispatch split, default them to 'packing'.
    _add_column_if_missing(conn, "sessions", "category", "category TEXT NOT NULL DEFAULT 'packing'")
    _add_column_if_missing(conn, "uploads", "category", "category TEXT NOT NULL DEFAULT 'packing'")
    # local cleanup tracking
    _add_column_if_missing(conn, "uploads", "drive_synced_at", "drive_synced_at TEXT")
    _add_column_if_missing(conn, "uploads", "local_deleted_at", "local_deleted_at TEXT")
    # "keep logs" (consignment/item/Sheet logging) opt-in, per session
    _add_column_if_missing(conn, "sessions", "keep_logs", "keep_logs INTEGER NOT NULL DEFAULT 0")
    # consignment/store logging (see config.CONSIGNMENT_LOGGING_CATEGORY)
    _add_column_if_missing(conn, "uploads", "consignment_id", "consignment_id INTEGER")
    _add_column_if_missing(
        conn, "uploads", "sheet_status", "sheet_status TEXT NOT NULL DEFAULT 'not_applicable'"
    )
    _add_column_if_missing(conn, "uploads", "sheet_synced_at", "sheet_synced_at TEXT")
    _add_column_if_missing(conn, "uploads", "sheet_error", "sheet_error TEXT")
    _add_column_if_missing(conn, "uploads", "sheet_retry_after", "sheet_retry_after TEXT")
    # New Store Kits: which pack a kit photo belongs to (NULL for every other
    # session). Its index can only be created once the column exists, so it
    # lives here rather than in init_db's CREATE script.
    _add_column_if_missing(conn, "uploads", "pack_id", "pack_id INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_uploads_pack ON uploads(pack_id)")
    # Pack labels (labels.py): when a pack's label was last printed, by whom,
    # and how many times - shown on its card, so nobody prints a box twice
    # without meaning to. kit_packs may already exist from before labels.
    _add_column_if_missing(conn, "kit_packs", "last_printed_at", "last_printed_at TEXT")
    _add_column_if_missing(conn, "kit_packs", "last_printed_by", "last_printed_by TEXT")
    _add_column_if_missing(
        conn, "kit_packs", "print_count", "print_count INTEGER NOT NULL DEFAULT 0"
    )
    # Reopening a submitted kit (see reopen_kit): who reopened it last, when,
    # and how often; how many times it has been Final Submitted and when the
    # latest one was (finalized_at is cleared by a reopen, so it can't say).
    _add_column_if_missing(conn, "kits", "reopened_at", "reopened_at TEXT")
    _add_column_if_missing(conn, "kits", "reopened_by", "reopened_by TEXT")
    _add_column_if_missing(conn, "kits", "reopen_count", "reopen_count INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "kits", "submit_count", "submit_count INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "kits", "last_submitted_at", "last_submitted_at TEXT")
    # The kit name its Pack Log Sheet was last WRITTEN under - the Sheet is
    # named "<Job> - <Kit> - Pack Log", so a kit renamed after a submission
    # (see rename_kit) tells Apps Script to rename that file rather than
    # start a second Sheet for the same kit.
    _add_column_if_missing(conn, "kits", "sheet_kit_name", "sheet_kit_name TEXT")
    # ...and the job number it was written under (the Sheet lives in
    # "<Job>/" and its name starts with the job number). NULL = the kit's
    # own job_number (every kit written before a kit could change job).
    _add_column_if_missing(conn, "kits", "sheet_job_number", "sheet_job_number TEXT")
    # The kit's labels PDF in Drive ("<Job> - <Kit> - Labels.pdf", next to the
    # Pack Log) and which submission it was made for: every Final Submit
    # makes a fresh one, and the Drive file id is how the previous one is
    # found and moved to the trash - even after a rename changed its name.
    _add_column_if_missing(conn, "kits", "labels_file_id", "labels_file_id TEXT")
    _add_column_if_missing(conn, "kits", "labels_submit_count", "labels_submit_count INTEGER")
    # A kit submitted before submit_count existed has 0 in it - but it WAS
    # submitted (its photos are in Drive), and "never submitted" is exactly
    # what lets a kit be deleted. finalize_kit always sets both together, so
    # a submitted kit with a 0 count can only be one of those; safe to rerun.
    conn.execute(
        """UPDATE kits
              SET submit_count = 1,
                  last_submitted_at = COALESCE(last_submitted_at,
                                               (SELECT finalized_at FROM sessions WHERE id = kits.session_id))
            WHERE submit_count = 0
              AND EXISTS (SELECT 1 FROM sessions WHERE id = kits.session_id AND finalized_at IS NOT NULL)"""
    )
    conn.commit()
    _import_legacy_employees(conn)


def _import_legacy_employees(conn):
    """One-time migration, first run only: the old free-text name list
    (data/employees.json, pre-login) had no accounts at all, just display
    names - importing them as standard users with pin_hash NULL means
    existing staff keep their name and just set a PIN on next login instead
    of losing their spot in the list. Only runs while the users table is
    still empty, so it can never re-import (or re-add someone already
    removed) on a later restart."""
    if not EMPLOYEES_FILE.exists():
        return
    if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        return
    try:
        with open(EMPLOYEES_FILE, "r", encoding="utf-8") as f:
            names = json.load(f)
    except (OSError, ValueError):
        return
    now = now_iso()
    for name in names:
        name = (name or "").strip()
        if not name:
            continue
        conn.execute(
            """INSERT OR IGNORE INTO users (name, name_norm, role, created_at)
               VALUES (?, ?, 'standard', ?)""",
            (name, name.casefold(), now),
        )
    conn.commit()


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def now_filename_stamp():
    """Microsecond-precision local timestamp for building unique filenames -
    now_iso() only keeps second precision, which isn't fine-grained enough to
    guarantee two photos landing in the same second never collide."""
    return datetime.now().strftime("%Y%m%dT%H%M%S%f")


def ensure_job(job_number):
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO jobs (job_number, created_at) VALUES (?, ?)",
        (job_number, now_iso()),
    )
    conn.commit()


def create_session(session_id, job_number, employee_name, category, keep_logs=False):
    conn = get_conn()
    conn.execute(
        """INSERT INTO sessions (id, job_number, employee_name, started_at, category, keep_logs)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (session_id, job_number, employee_name, now_iso(), category, int(bool(keep_logs))),
    )
    conn.commit()


def get_session(session_id):
    conn = get_conn()
    return conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()


def find_open_session(job_number, employee_name, category):
    """The "resume where you left off" lookup for /start - an existing,
    not-yet-finalized session for the same job+employee+category, as long as
    it was started within SESSION_RESUME_WINDOW_HOURS. That window matters:
    without it, re-entering the same job number weeks later (a genuinely new,
    separate batch) would silently resume a long-abandoned one instead of
    starting fresh. Uploads made to a resumed session were never lost in the
    first place (each photo is written to disk and committed to the DB the
    instant it's picked, before this lookup is even involved) - this just
    finds the right session_id to land back on, so the existing photos and
    consignment logs are visible again and Submit can still finalize them."""
    conn = get_conn()
    cutoff = (
        datetime.now(timezone.utc).astimezone() - timedelta(hours=SESSION_RESUME_WINDOW_HOURS)
    ).isoformat(timespec="seconds")
    return conn.execute(
        """SELECT * FROM sessions
           WHERE job_number = ? AND employee_name = ? AND category = ?
             AND finalized_at IS NULL AND started_at >= ?
           ORDER BY started_at DESC LIMIT 1""",
        (job_number, employee_name, category, cutoff),
    ).fetchone()


def list_open_sessions(employee_name):
    """Every not-yet-finalized session for this employee, regardless of how
    long ago it started (unlike find_open_session's resume window) - this
    powers the Active Sessions list on the job-number page, which exists
    precisely so a long-abandoned or duplicate session doesn't just vanish
    unresumable - it can still be reopened or deleted from here.

    New Store Kit sessions are left out: a kit belongs to everyone working
    on it, not just the person who happened to start it, so the page lists
    those separately for every user (list_open_kits)."""
    conn = get_conn()
    return conn.execute(
        """SELECT s.*, (SELECT COUNT(*) FROM uploads u WHERE u.session_id = s.id) AS photo_count
           FROM sessions s
           WHERE s.employee_name = ? AND s.finalized_at IS NULL AND s.category != ?
           ORDER BY s.started_at DESC""",
        (employee_name, NEW_STORE_KITS_CATEGORY),
    ).fetchall()


def list_all_open_sessions():
    """Every unfinalized session across every employee, ordered employee
    first (alphabetically) then oldest-started first within each employee -
    powers the admin page's view of who has folders sitting around that were
    never submitted, so admin can nudge the employee to finish them or
    finalize them directly. app.py just groups these already-ordered rows by
    employee_name; a plain dict preserves that order. kit_name is set (and
    employee_name is the kit's creator) for New Store Kit sessions, NULL for
    everything else."""
    conn = get_conn()
    return conn.execute(
        """SELECT s.*, (SELECT COUNT(*) FROM uploads u WHERE u.session_id = s.id) AS photo_count,
                  k.kit_name AS kit_name, k.submit_count AS submit_count
           FROM sessions s
           LEFT JOIN kits k ON k.session_id = s.id
           WHERE s.finalized_at IS NULL
           ORDER BY s.employee_name COLLATE NOCASE ASC, s.started_at ASC"""
    ).fetchall()


def delete_session(session_id):
    conn = get_conn()
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()


def add_upload(
    session_id, job_number, employee_name, category, filename, thumb_filename, consignment_id=None
):
    conn = get_conn()
    sheet_status = "pending" if consignment_id else "not_applicable"
    cur = conn.execute(
        """INSERT INTO uploads
           (session_id, job_number, employee_name, category, filename, thumb_filename, uploaded_at,
            consignment_id, sheet_status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            session_id, job_number, employee_name, category, filename, thumb_filename, now_iso(),
            consignment_id, sheet_status,
        ),
    )
    conn.commit()
    return cur.lastrowid


def finalize_session(session_id):
    conn = get_conn()
    conn.execute(
        "UPDATE sessions SET finalized_at = ? WHERE id = ?", (now_iso(), session_id)
    )
    conn.execute(
        "UPDATE uploads SET status = 'finalized' WHERE session_id = ?", (session_id,)
    )
    conn.commit()
    return conn.execute(
        "SELECT COUNT(*) AS n FROM uploads WHERE session_id = ?", (session_id,)
    ).fetchone()["n"]


def get_upload(upload_id):
    conn = get_conn()
    return conn.execute("SELECT * FROM uploads WHERE id = ?", (upload_id,)).fetchone()


def delete_upload(upload_id):
    conn = get_conn()
    conn.execute("DELETE FROM uploads WHERE id = ?", (upload_id,))
    conn.commit()


def get_upload_by_filename(job_number, category, filename):
    conn = get_conn()
    return conn.execute(
        """SELECT * FROM uploads WHERE job_number = ? AND category = ? AND filename = ?""",
        (job_number, category, filename),
    ).fetchone()


def get_upload_by_thumb_filename(job_number, category, thumb_filename):
    conn = get_conn()
    return conn.execute(
        """SELECT * FROM uploads WHERE job_number = ? AND category = ? AND thumb_filename = ?""",
        (job_number, category, thumb_filename),
    ).fetchone()


def uploads_for_job(job_number):
    """Every photo ever uploaded for a job, across all sessions/employees -
    used by the supervisor gallery, which is meant to show everything.
    pack_number/kit_name are set for New Store Kit photos (NULL otherwise)
    so the gallery can tag them "Store 12 · Pack 3"."""
    conn = get_conn()
    return conn.execute(
        """SELECT u.*, c.key_value AS consignment_value, c.item_ids AS consignment_item_ids,
                  p.pack_number AS pack_number, k.kit_name AS kit_name
           FROM uploads u
           LEFT JOIN consignments c ON c.id = u.consignment_id
           LEFT JOIN kit_packs p ON p.id = u.pack_id
           LEFT JOIN kits k ON k.session_id = u.session_id
           WHERE u.job_number = ?
           ORDER BY u.uploaded_at ASC, u.id ASC""",
        (job_number,),
    ).fetchall()


def uploads_for_session(session_id):
    """Only this session's own photos - used by the live upload page, so a
    worker doesn't see (or risk deleting) photos from someone else's earlier
    or concurrent session on the same job. (A New Store Kit is one shared
    session, so there this is every collaborator's photo for the kit -
    pack_number/kit_name say which pack each belongs to.)"""
    conn = get_conn()
    return conn.execute(
        """SELECT u.*, c.key_value AS consignment_value, c.item_ids AS consignment_item_ids,
                  p.pack_number AS pack_number, k.kit_name AS kit_name
           FROM uploads u
           LEFT JOIN consignments c ON c.id = u.consignment_id
           LEFT JOIN kit_packs p ON p.id = u.pack_id
           LEFT JOIN kits k ON k.session_id = u.session_id
           WHERE u.session_id = ?
           ORDER BY u.uploaded_at ASC, u.id ASC""",
        (session_id,),
    ).fetchall()


def pending_drive_uploads_grouped(limit=200):
    """Finalized-but-not-yet-synced uploads, grouped by (job_number,
    category) - a single Apps Script call can only ever hold files bound for
    the same Drive subfolder, so that's as far as grouping happens here.
    Deliberately NOT sliced into fixed-size batches: how many photos safely
    fit in one call depends on their actual file sizes (a batch of 20 real
    5MB photos is a very different request than 20 tiny test fixtures), so
    that slicing happens in drive_sync.py, which can check real file sizes
    on disk - see _split_into_batches. `limit` bounds how many total rows
    are considered per call, so a large backlog can't make one pass through
    the loop take unbounded time - the remainder is picked up on later ticks.
    Kit photos carry pack_number/kit_name (NULL otherwise) so drive_sync can
    note "Store 12 · Pack 3" on the Drive file; the grouping is unchanged."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT u.*, p.pack_number AS pack_number, k.kit_name AS kit_name
           FROM uploads u
           LEFT JOIN kit_packs p ON p.id = u.pack_id
           LEFT JOIN kits k ON k.session_id = u.session_id
           WHERE u.status = 'finalized'
             AND u.drive_status IN ('pending', 'error')
             AND (u.retry_after IS NULL OR u.retry_after <= ?)
           ORDER BY u.id ASC LIMIT ?""",
        (now_iso(), limit),
    ).fetchall()

    by_job_category = {}
    for row in rows:
        by_job_category.setdefault((row["job_number"], row["category"]), []).append(row)
    return by_job_category


def mark_drive_synced(upload_id, drive_file_id):
    conn = get_conn()
    conn.execute(
        """UPDATE uploads
           SET drive_status = 'synced', drive_file_id = ?, drive_error = NULL,
               drive_synced_at = ?
           WHERE id = ?""",
        (drive_file_id, now_iso(), upload_id),
    )
    conn.commit()


def mark_drive_error(upload_id, error_text, retry_after_iso):
    conn = get_conn()
    conn.execute(
        """UPDATE uploads SET drive_status = 'error', drive_error = ?, retry_after = ?
           WHERE id = ?""",
        (error_text, retry_after_iso, upload_id),
    )
    conn.commit()


def uploads_ready_for_local_cleanup(cutoff_iso, limit=50):
    """A photo's local copy is only eligible once the photo ITSELF is synced
    (drive_status) AND, for anything tagged to a consignment, its link has
    actually landed in that job's Sheet too (sheet_status) - not just synced
    to Drive with the Sheet update still pending/erroring. 'not_applicable'
    covers photos with no consignment - nothing to wait for there."""
    conn = get_conn()
    return conn.execute(
        """SELECT * FROM uploads
           WHERE drive_status = 'synced'
             AND sheet_status IN ('synced', 'not_applicable')
             AND local_deleted_at IS NULL
             AND drive_synced_at IS NOT NULL
             AND drive_synced_at <= ?
           ORDER BY id ASC LIMIT ?""",
        (cutoff_iso, limit),
    ).fetchall()


def job_fully_synced(job_number, category):
    """True once every upload for this job+category has reached Drive AND,
    for anything tagged to a consignment, its Sheet row too (same condition
    as uploads_ready_for_local_cleanup, minus the local-cleanup age buffer -
    this is about "has it all reached Drive/Sheet", not "is it safe to
    delete the local copy yet"). False for a job with no uploads at all, so
    this only ever fires right after a job actually had something synced."""
    conn = get_conn()
    row = conn.execute(
        """SELECT COUNT(*) AS n FROM uploads
           WHERE job_number = ? AND category = ?
             AND NOT (drive_status = 'synced' AND sheet_status IN ('synced', 'not_applicable'))""",
        (job_number, category),
    ).fetchone()
    if row["n"] > 0:
        return False
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM uploads WHERE job_number = ? AND category = ?",
        (job_number, category),
    ).fetchone()
    return total["n"] > 0


def is_sheet_resized(job_number, category):
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM sheet_resized WHERE job_number = ? AND category = ?",
        (job_number, category),
    ).fetchone()
    return row is not None


def mark_sheet_resized(job_number, category):
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO sheet_resized (job_number, category, resized_at) VALUES (?, ?, ?)",
        (job_number, category, now_iso()),
    )
    conn.commit()


def mark_local_cleaned(upload_id):
    conn = get_conn()
    conn.execute(
        "UPDATE uploads SET local_deleted_at = ? WHERE id = ?", (now_iso(), upload_id)
    )
    conn.commit()


# --- Consignments (Consignment #/Store name proof logging) ---------------

def split_list(value):
    return [x for x in value.split(",") if x] if value else []


def _join_list(items):
    return ",".join(items)


def has_consignments(job_number, category):
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM consignments WHERE job_number = ? AND category = ? LIMIT 1",
        (job_number, category),
    ).fetchone()
    return row is not None


def consignment_values_for_job(job_number, category, limit=300):
    """Every distinct Consignment/Store value already scanned for this job,
    most-recently-touched first - powers the upload page's autocomplete so a
    value someone else already entered (on any session/device) shows up as
    soon as a later scan starts typing it."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT key_value FROM consignments
           WHERE job_number = ? AND category = ?
           ORDER BY updated_at DESC LIMIT ?""",
        (job_number, category, limit),
    ).fetchall()
    return [row["key_value"] for row in rows]


def find_consignment(job_number, category, key_norm):
    conn = get_conn()
    return conn.execute(
        "SELECT * FROM consignments WHERE job_number = ? AND category = ? AND key_norm = ?",
        (job_number, category, key_norm),
    ).fetchone()


def get_consignment(consignment_id):
    conn = get_conn()
    return conn.execute(
        "SELECT * FROM consignments WHERE id = ?", (consignment_id,)
    ).fetchone()


def create_consignment(job_number, category, key_type, key_value, key_norm, employee_name):
    conn = get_conn()
    now = now_iso()
    cur = conn.execute(
        """INSERT INTO consignments
           (job_number, category, key_type, key_value, key_norm, contributors, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (job_number, category, key_type, key_value, key_norm, employee_name, now, now),
    )
    conn.commit()
    return cur.lastrowid


def find_or_create_consignment(job_number, category, key_type, key_value, key_norm, employee_name):
    """The find-then-create check is racy under real concurrency: two
    sessions scanning the same brand-new consignment number at the same
    instant can both pass the "not found" check before either inserts. The
    UNIQUE index (job_number, category, key_norm) then lets only one INSERT
    win - this catches the loser's IntegrityError and falls back to reading
    the row the winner just committed, instead of surfacing a 500 error.
    Returns (row, existing: bool)."""
    row = find_consignment(job_number, category, key_norm)
    if row:
        touch_consignment_contributor(row["id"], employee_name)
        return get_consignment(row["id"]), True

    conn = get_conn()
    try:
        consignment_id = create_consignment(
            job_number, category, key_type, key_value, key_norm, employee_name
        )
        return get_consignment(consignment_id), False
    except sqlite3.IntegrityError:
        conn.rollback()
        row = find_consignment(job_number, category, key_norm)
        if row is None:
            raise  # a different cause - don't hide a real error behind this fallback
        touch_consignment_contributor(row["id"], employee_name)
        return get_consignment(row["id"]), True


def touch_consignment_contributor(consignment_id, employee_name):
    conn = get_conn()
    row = conn.execute(
        "SELECT contributors FROM consignments WHERE id = ?", (consignment_id,)
    ).fetchone()
    if row is None:
        return
    contributors = split_list(row["contributors"])
    if employee_name not in contributors:
        contributors.append(employee_name)
    conn.execute(
        "UPDATE consignments SET contributors = ?, updated_at = ? WHERE id = ?",
        (_join_list(contributors), now_iso(), consignment_id),
    )
    conn.commit()


def _count_groups(raw_items):
    """[a, b, a, a] -> [(a, 3), (b, 1)], first-scan order. A repeat isn't a
    duplicate to collapse away - it's the same physical item scanned again,
    tracked as a count (see group_item_ids)."""
    counts = {}
    order = []
    for v in raw_items:
        if v not in counts:
            counts[v] = 0
            order.append(v)
        counts[v] += 1
    return [(v, counts[v]) for v in order]


def group_item_ids(raw_value):
    """Raw comma-joined item_ids column value -> count-annotated groups,
    e.g. "xxxxxx" scanned 3 times becomes {"value": "xxxxxx", "count": 3,
    "label": "xxxxxx-3"}. The count always shows, even at 1 ("xxxxxx-1"),
    so the label format never shifts as a count changes. `label` is what's
    shown/synced to the Sheet; `value`/`count` are what the +/- chip
    controls act on."""
    return [
        {"value": v, "count": c, "label": f"{v}-{c}"}
        for v, c in _count_groups(split_list(raw_value))
    ]


def item_id_labels(raw_value):
    return [g["label"] for g in group_item_ids(raw_value)]


def add_consignment_item_id(consignment_id, item_id):
    """Appends a raw scan - NOT deduped against existing entries. Scanning
    the same Item ID again is how its count goes up (see group_item_ids)."""
    conn = get_conn()
    row = conn.execute(
        "SELECT item_ids FROM consignments WHERE id = ?", (consignment_id,)
    ).fetchone()
    raw_items = split_list(row["item_ids"]) if row else []
    raw_items.append(item_id)
    new_value = _join_list(raw_items)
    conn.execute(
        "UPDATE consignments SET item_ids = ?, updated_at = ? WHERE id = ?",
        (new_value, now_iso(), consignment_id),
    )
    conn.commit()
    return group_item_ids(new_value)


def decrement_consignment_item_id(consignment_id, item_id):
    """Removes exactly one occurrence of `item_id` (the raw scanned value,
    not a "-N" display label) - count N drops to N-1, or the entry disappears
    entirely once N reaches 0. Backs both the sole delete button on a
    count-1 chip and the "-" step button on a count>1 chip."""
    conn = get_conn()
    row = conn.execute(
        "SELECT item_ids FROM consignments WHERE id = ?", (consignment_id,)
    ).fetchone()
    raw_items = split_list(row["item_ids"]) if row else []
    if item_id in raw_items:
        raw_items.remove(item_id)  # removes a single occurrence, not every one
    new_value = _join_list(raw_items)
    conn.execute(
        "UPDATE consignments SET item_ids = ?, updated_at = ? WHERE id = ?",
        (new_value, now_iso(), consignment_id),
    )
    conn.commit()
    return group_item_ids(new_value)


def increment_photo_count(consignment_id):
    """Bumps the display-only running total ("N photos already logged") for a
    consignment. Filenames don't depend on this - they're always
    employee-timestamp - so this has no uniqueness requirement, just needs to
    move forward by exactly one per photo."""
    conn = get_conn()
    conn.execute(
        "UPDATE consignments SET photo_count = photo_count + 1, updated_at = ? WHERE id = ?",
        (now_iso(), consignment_id),
    )
    conn.commit()


def decrement_photo_count(consignment_id):
    """Mirrors increment_photo_count - called when a not-yet-finalized photo
    tagged to a consignment is deleted, so the displayed count doesn't
    over-report. Floors at 0 to stay safe against any bookkeeping drift."""
    conn = get_conn()
    conn.execute(
        """UPDATE consignments SET photo_count = MAX(0, photo_count - 1), updated_at = ?
           WHERE id = ?""",
        (now_iso(), consignment_id),
    )
    conn.commit()


def delete_consignment_if_orphaned(consignment_id):
    """Called after a session's uploads referencing this consignment are
    gone - removes the consignment record too if nothing else still points
    at it. A consignment shared with another session (rare - the same
    key scanned under the same job+category from two sessions) survives
    untouched."""
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM uploads WHERE consignment_id = ? LIMIT 1", (consignment_id,)
    ).fetchone()
    if row is None:
        conn.execute("DELETE FROM consignments WHERE id = ?", (consignment_id,))
        conn.commit()


def rename_session_job(session_id, new_job_number, category):
    """Moves one session to a corrected job number - called only after the
    physical files have already been moved on disk (see app.py), and only
    for a session confirmed unfinalized (Drive/Sheets never referenced the
    old job number yet, so nothing there needs correcting).

    For each consignment this session touched: if the corrected job number
    has no record for that same key yet, the consignment just moves with it
    (the common case - the wrong job number was wrong for everyone). If one
    already exists there (someone logged the same consignment/store under
    the correct number already), this session's contribution is merged into
    it instead - the unique index on (job_number, category, key_norm)
    forbids two rows for the same key, and ALL uploads pointing at the old
    consignment (not just this session's) are repointed at the surviving
    one, since the old row is being deleted."""
    conn = get_conn()
    ensure_job(new_job_number)

    consignment_ids = [
        row["consignment_id"] for row in conn.execute(
            """SELECT DISTINCT consignment_id FROM uploads
               WHERE session_id = ? AND consignment_id IS NOT NULL""",
            (session_id,),
        ).fetchall()
    ]

    for old_id in consignment_ids:
        old_row = get_consignment(old_id)
        if old_row is None:
            continue
        target = find_consignment(new_job_number, category, old_row["key_norm"])
        if target is None:
            conn.execute(
                "UPDATE consignments SET job_number = ?, updated_at = ? WHERE id = ?",
                (new_job_number, now_iso(), old_id),
            )
            continue

        conn.execute(
            "UPDATE uploads SET consignment_id = ? WHERE consignment_id = ?",
            (target["id"], old_id),
        )
        merged_items = split_list(target["item_ids"]) + split_list(old_row["item_ids"])
        merged_contributors = split_list(target["contributors"])
        for name in split_list(old_row["contributors"]):
            if name not in merged_contributors:
                merged_contributors.append(name)
        conn.execute(
            """UPDATE consignments
               SET item_ids = ?, contributors = ?, photo_count = photo_count + ?, updated_at = ?
               WHERE id = ?""",
            (
                _join_list(merged_items), _join_list(merged_contributors),
                old_row["photo_count"], now_iso(), target["id"],
            ),
        )
        conn.execute("DELETE FROM consignments WHERE id = ?", (old_id,))

    conn.execute("UPDATE uploads SET job_number = ? WHERE session_id = ?", (new_job_number, session_id))
    conn.execute("UPDATE sessions SET job_number = ? WHERE id = ?", (new_job_number, session_id))
    conn.commit()


def synced_uploads_for_consignment(consignment_id):
    conn = get_conn()
    return conn.execute(
        """SELECT * FROM uploads WHERE consignment_id = ? AND drive_status = 'synced'
           ORDER BY id ASC""",
        (consignment_id,),
    ).fetchall()


def rehydrate_consignment(
    job_number, category, key_value, item_ids, contributors, photo_count,
    created_at=None, updated_at=None,
):
    """Recreates a local consignment record from a job's Google Sheet log,
    for when this job's local data was cleaned up (or never existed on this
    machine) and someone resumes it later. `photo_count` seeds the running
    total shown to the user - the actual photo links themselves stay tracked
    in the sheet, not locally.

    Two sessions can both start on the same never-before-seen-locally job at
    close enough to the same instant that both pass the "not found" check
    before either inserts (see find_or_create_consignment for the same
    pattern) - the UNIQUE index then lets only one INSERT win, and this
    catches the loser's IntegrityError rather than surfacing a 500 error."""
    key_norm = key_value.strip().casefold()
    existing = find_consignment(job_number, category, key_norm)
    if existing:
        return existing["id"]

    conn = get_conn()
    now = now_iso()
    try:
        cur = conn.execute(
            """INSERT INTO consignments
               (job_number, category, key_type, key_value, key_norm, item_ids, contributors,
                photo_count, created_at, updated_at)
               VALUES (?, ?, 'consignment', ?, ?, ?, ?, ?, ?, ?)""",
            (
                job_number, category, key_value, key_norm,
                _join_list(item_ids), _join_list(contributors),
                photo_count, created_at or now, updated_at or now,
            ),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        conn.rollback()
        existing = find_consignment(job_number, category, key_norm)
        if existing is None:
            raise
        return existing["id"]


def pending_sheet_log_batches(consignment_cap, limit=200):
    """Groups uploads waiting on a Sheet update into batches of up to
    `consignment_cap` consignments each, one Apps Script call per batch -
    grouped first by job (a call updates one job's Sheet), then packed up to
    the cap. Combining multiple consignments matters for the same reason as
    upload batching: a job with many small consignments (e.g. 100
    consignments x 1 photo each) would otherwise still need 100 Sheet-update
    calls if each call covered only one consignment.
    Returns a list of (job_number, {consignment_id: [upload_id, ...]})."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM uploads
           WHERE consignment_id IS NOT NULL
             AND drive_status = 'synced'
             AND sheet_status IN ('pending', 'error')
             AND (sheet_retry_after IS NULL OR sheet_retry_after <= ?)
           ORDER BY id ASC LIMIT ?""",
        (now_iso(), limit),
    ).fetchall()

    by_job = {}
    for row in rows:
        job_groups = by_job.setdefault(row["job_number"], {})
        job_groups.setdefault(row["consignment_id"], []).append(row["id"])

    batches = []
    for job_number, consignment_groups in by_job.items():
        items = list(consignment_groups.items())
        for i in range(0, len(items), consignment_cap):
            batches.append((job_number, dict(items[i:i + consignment_cap])))
    return batches


def mark_sheet_synced(upload_id):
    conn = get_conn()
    conn.execute(
        """UPDATE uploads SET sheet_status = 'synced', sheet_synced_at = ?, sheet_error = NULL
           WHERE id = ?""",
        (now_iso(), upload_id),
    )
    conn.commit()


def mark_sheet_error(upload_id, error_text, retry_after_iso):
    conn = get_conn()
    conn.execute(
        """UPDATE uploads SET sheet_status = 'error', sheet_error = ?, sheet_retry_after = ?
           WHERE id = ?""",
        (error_text, retry_after_iso, upload_id),
    )
    conn.commit()


def recent_job_numbers(limit=300):
    """Job numbers anyone has started a session for, most recently used
    first - the start page's job-number autocomplete. Upper-cased and grouped
    so a legacy "j456789" session and a "J456789" one are one suggestion, and
    only well-formed numbers are offered (GLOB is case-sensitive, hence the
    UPPER on both sides)."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT UPPER(job_number) AS jn, MAX(started_at) AS last
           FROM sessions
           WHERE UPPER(job_number) GLOB 'J[0-9][0-9][0-9][0-9][0-9][0-9]'
           GROUP BY UPPER(job_number)
           ORDER BY last DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [row["jn"] for row in rows]


# --- New Store Kits ---------------------------------------------------------
#
# A kit is one shared session (category NEW_STORE_KITS_CATEGORY) plus a
# `kits` row; its numbered packs are `kit_packs`, the item numbers typed into
# each pack `kit_pack_items`, and its photos ordinary `uploads` rows with
# pack_id set. Up to ~4 people work one kit at once, so the rule that matters
# is "a pack is only ever edited by the one person holding it": every write
# below re-checks that hold INSIDE the same statement (or at least the same
# transaction) that makes the change, never with a separate read beforehand -
# a read-then-write lets a takeover, a Save or a Final Submit from another
# phone land in between. See this module's docstring for why every function
# here wraps its body in `with conn:`.

_KIT_SELECT = """SELECT k.*, s.finalized_at AS finalized_at, s.started_at AS started_at
                 FROM kits k JOIN sessions s ON s.id = k.session_id"""

# A write is only allowed while the kit's session is still unsubmitted -
# appended to the WHERE of every kit write (with the session id bound once more).
_KIT_OPEN = "EXISTS (SELECT 1 FROM sessions WHERE id = ? AND finalized_at IS NULL)"


def _now_ts():
    """Pack heartbeats are unix epoch seconds (see kit_packs.reserved_ts)."""
    return int(time.time())


def get_kit(session_id):
    conn = get_conn()
    return conn.execute(_KIT_SELECT + " WHERE k.session_id = ?", (session_id,)).fetchone()


def find_kit(job_number, kit_norm):
    conn = get_conn()
    return conn.execute(
        _KIT_SELECT + " WHERE k.job_number = ? AND k.kit_norm = ?", (job_number, kit_norm)
    ).fetchone()


def kit_holding_sheet_name(job_number, kit_norm, exclude_session_id=None):
    """The OTHER kit of this job whose Drive Pack Log Sheet still carries
    this name, else None. A kit renamed after it was submitted keeps its old
    Sheet name ("<Job> - Alpha - Pack Log") until its next Final Submit
    renames the file (sheet_kit_name), and the Apps Script finds Sheets by
    name - so a NEW kit called "Alpha" in that window would take over (and
    overwrite) the renamed kit's Sheet. Until that rename lands, the old
    name stays reserved. The kit's own old name is never a clash with
    itself (renaming back is fine). Same normalisation as
    kits.normalize_kit_name."""
    conn = get_conn()
    # A Sheet sits in the job it was last WRITTEN under (sheet_job_number) -
    # a kit moved to another job keeps its Sheet in the old job's folder,
    # under the old name, until its next Final Submit moves it.
    rows = conn.execute(
        """SELECT * FROM kits
           WHERE sheet_kit_name IS NOT NULL AND COALESCE(sheet_job_number, job_number) = ?
             AND (sheet_kit_name != kit_name OR COALESCE(sheet_job_number, job_number) != job_number)""",
        (job_number,),
    ).fetchall()
    for row in rows:
        if exclude_session_id is not None and row["session_id"] == exclude_session_id:
            continue
        if " ".join(row["sheet_kit_name"].split()).casefold() == kit_norm:
            return row
    return None


def find_or_create_kit(job_number, kit_name, kit_norm, employee_name):
    """The kit named `kit_name` for this job - joined if it already exists
    (finalized or not: the caller decides what a finalized one means), else
    created along with its session, both in ONE transaction so there can
    never be a session without its kit row or vice versa. Two people starting
    the same brand-new kit at the same instant can both miss find_kit; the
    UNIQUE (job_number, kit_norm) index then lets only one INSERT win, and the
    loser's rollback undoes its session row too before it joins the winner's
    kit instead (same pattern as find_or_create_consignment).
    Returns (kit_row, existing: bool)."""
    row = find_kit(job_number, kit_norm)
    if row:
        return row, True

    conn = get_conn()
    session_id = uuid.uuid4().hex
    now = now_iso()
    try:
        with conn:
            conn.execute(
                """INSERT INTO sessions (id, job_number, employee_name, started_at, category, keep_logs)
                   VALUES (?, ?, ?, ?, ?, 0)""",
                (session_id, job_number, employee_name, now, NEW_STORE_KITS_CATEGORY),
            )
            conn.execute(
                """INSERT INTO kits (session_id, job_number, kit_name, kit_norm, created_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, job_number, kit_name, kit_norm, employee_name, now),
            )
    except sqlite3.IntegrityError:
        row = find_kit(job_number, kit_norm)
        if row is None:
            raise  # a different cause - don't hide a real error behind this fallback
        return row, True
    return get_kit(session_id), False


def list_open_kits():
    """Every unsubmitted kit, newest first - shown to EVERY user on the start
    page (a kit is shared, so anyone might need to join it). A reopened kit
    counts as new again from the moment it was reopened, so it sits at the
    top next to the kits people are working on now, not buried under them
    by its original start date."""
    conn = get_conn()
    return conn.execute(
        """SELECT k.*, s.started_at AS started_at, s.finalized_at AS finalized_at,
                  (SELECT COUNT(*) FROM uploads u WHERE u.session_id = k.session_id) AS photo_count,
                  (SELECT COUNT(*) FROM kit_packs p WHERE p.session_id = k.session_id) AS pack_count
           FROM kits k JOIN sessions s ON s.id = k.session_id
           WHERE s.finalized_at IS NULL
           ORDER BY COALESCE(k.reopened_at, s.started_at) DESC, k.rowid DESC"""
    ).fetchall()


def open_kit_names_for_job(job_number):
    """Names of this job's unsubmitted kits, oldest first - used by /start to
    ask "join one of these, or really create a new kit?" when a typed name
    matches none of them (most likely a typo of an existing one)."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT k.kit_name FROM kits k JOIN sessions s ON s.id = k.session_id
           WHERE k.job_number = ? AND s.finalized_at IS NULL
           ORDER BY s.started_at ASC, k.rowid ASC""",
        (job_number,),
    ).fetchall()
    return [row["kit_name"] for row in rows]


def list_kits_for_job(job_number):
    """Every kit of this job - open AND submitted - oldest first, each with
    finalized_at (NULL = open). /start uses it to ask "join / reopen one of
    these, or really create a new kit?" when a typed name matches none of
    them: a submitted kit is as likely to be the one meant as an open one
    (someone coming back to fix a finished kit - see reopen_kit)."""
    conn = get_conn()
    return conn.execute(
        _KIT_SELECT + """ WHERE k.job_number = ?
                          ORDER BY s.started_at ASC, k.rowid ASC""",
        (job_number,),
    ).fetchall()


def list_kit_names(limit=1000):
    """(job, kit name, open/submitted) for the start page's kit-name
    suggestions - every kit, not just open ones, so a finished kit can be
    found and reopened by typing its job number. Oldest first within a job;
    capped at the `limit` most recently started kits so years of history
    can't bloat the page (an older kit is still reached by typing its name)."""
    conn = get_conn()
    return conn.execute(
        """SELECT * FROM (
               SELECT k.job_number AS job_number, k.kit_name AS kit_name, k.rowid AS kit_rowid,
                      k.last_submitted_at AS last_submitted_at,
                      s.finalized_at AS finalized_at, s.started_at AS started_at
               FROM kits k JOIN sessions s ON s.id = k.session_id
               ORDER BY s.started_at DESC, k.rowid DESC LIMIT ?)
           ORDER BY job_number ASC, started_at ASC, kit_rowid ASC""",
        (limit,),
    ).fetchall()


def merge_collaborators(created_by, packs, photo_uploaders=()):
    """Creator first, then everyone who has edited any pack, in pack order
    and first-seen order within a pack, then anyone who only added kit
    photos that belong to no pack (general_photo_uploaders) - each name
    once. Shared by kit_collaborators and app.py (which already has the pack
    rows in hand)."""
    names = [created_by] if created_by else []
    for pack in sorted(packs, key=lambda p: p["pack_number"]):
        for name in split_list(pack["contributors"]):
            if name not in names:
                names.append(name)
    for name in photo_uploaders:
        if name and name not in names:
            names.append(name)
    return names


def kit_collaborators(session_id):
    """Who counts as having worked on this kit - the people allowed to push
    Final Submit (besides admins). Starting the kit counts; merely opening
    or reserving a pack doesn't (contributors only grows through _touch_pack,
    i.e. an actual item or photo change); adding a no-pack kit photo does."""
    kit = get_kit(session_id)
    if kit is None:
        return []
    return merge_collaborators(kit["created_by"], packs_for_kit(session_id), general_photo_uploaders(session_id))


def delete_kit(session_id):
    """Deletes a whole never-submitted kit - every collaborator's packs,
    items and photo rows - in ONE transaction. Returns (status, files):
      - ("deleted", [(filename, thumb_filename), ...]) - the caller removes
        those files from disk, only AFTER this has committed (on Windows an
        unlink fails while another thread is streaming that file to someone's
        phone, and a half-deleted kit must never be the result);
      - ("missing", None) - no such kit;
      - ("finalized", None) - it's submitted right now;
      - ("was_submitted", None) - it's open again (reopened), but it WAS
        submitted before: its photos are in Drive and its Pack Log Sheet
        exists, so deleting it here would leave those behind while the
        portal forgot about them. Checked on the kit's submit count AND on
        any photo already marked submitted, belt and braces.
    Every condition is part of the one DELETE, so a collaborator's Final
    Submit landing a moment earlier can never be undone by this. Any upload
    or item-add still in flight from another phone fails its own hold check
    afterwards (its pack no longer exists) and cleans up after itself."""
    conn = get_conn()
    with conn:
        cur = conn.execute(
            """DELETE FROM sessions
               WHERE id = ? AND finalized_at IS NULL
                 AND EXISTS (SELECT 1 FROM kits WHERE session_id = ? AND submit_count = 0)
                 AND NOT EXISTS (SELECT 1 FROM uploads WHERE session_id = ? AND status = 'finalized')""",
            (session_id, session_id, session_id),
        )
        if cur.rowcount == 0:
            sess = conn.execute("SELECT finalized_at FROM sessions WHERE id = ?", (session_id,)).fetchone()
            kit = conn.execute("SELECT 1 FROM kits WHERE session_id = ?", (session_id,)).fetchone()
            if sess is None or kit is None:
                return "missing", None
            if sess["finalized_at"]:
                return "finalized", None
            return "was_submitted", None
        files = [
            (row["filename"], row["thumb_filename"])
            for row in conn.execute(
                "SELECT filename, thumb_filename FROM uploads WHERE session_id = ?", (session_id,)
            ).fetchall()
        ]
        conn.execute("DELETE FROM uploads WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM kit_pack_items WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM kit_packs WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM kits WHERE session_id = ?", (session_id,))
    return "deleted", files


def finalize_kit(session_id, finalized_by, expected_rev):
    """Final Submit. Every precondition is part of the one UPDATE that marks
    the session submitted, so none of them can change between checking and
    submitting: still unsubmitted, nothing changed since the "Is this packed
    completely?" dialog was drawn (kits.rev == expected_rev - otherwise the
    person said Yes to a report that's already out of date), no pack still
    reserved by anyone (idle or not), and at least one item or photo in the
    kit. On success the kit's photos flip to 'finalized' (Drive sync picks
    them up) and its Pack Log Sheet is queued. A reopened kit (see
    reopen_kit) is submitted again exactly the same way: its submit count
    goes up and its Sheet is queued for a full rewrite of the kit as it is
    NOW. Returns (True, photo_count) - every photo of the kit, old and new -
    or (False, reason) with reason one of "missing" | "finalized" |
    "reserved" | "empty" | "changed", classified inside the same transaction
    so the reason matches the state that actually blocked it. (Whether the
    person is allowed to submit at all - a collaborator or admin - is
    app.py's check, made before this.)"""
    conn = get_conn()
    now = now_iso()
    with conn:
        cur = conn.execute(
            """UPDATE sessions SET finalized_at = ?
               WHERE id = ? AND finalized_at IS NULL
                 AND (SELECT rev FROM kits WHERE session_id = ?) = ?
                 AND NOT EXISTS (SELECT 1 FROM kit_packs WHERE session_id = ? AND reserved_by IS NOT NULL)
                 AND (EXISTS (SELECT 1 FROM kit_pack_items WHERE session_id = ?)
                      OR EXISTS (SELECT 1 FROM uploads WHERE session_id = ?))""",
            (now, session_id, session_id, expected_rev, session_id, session_id, session_id),
        )
        if cur.rowcount == 1:
            # The Sheet bookkeeping is per SUBMISSION: a kit reopened and
            # submitted again starts drive_sync's wait / partial / give-up
            # rules from scratch (its clock is the new finalized_at), and is
            # always rewritten - even when only an item number changed and no
            # new photo has to reach Drive first.
            conn.execute(
                """UPDATE kits
                      SET finalized_by = ?, submit_count = submit_count + 1, last_submitted_at = ?,
                          sheet_status = 'pending', sheet_error = NULL, sheet_retry_after = NULL,
                          sheet_synced_at = NULL, sheet_synced_count = 0
                    WHERE session_id = ?""",
                (finalized_by, now, session_id),
            )
            _bump_rev(conn, session_id)
            # Only photos added since the last submission change here; ones
            # submitted before stay exactly as they are (and stay drive-
            # synced, so they're never uploaded twice).
            conn.execute(
                "UPDATE uploads SET status = 'finalized' WHERE session_id = ? AND status != 'finalized'",
                (session_id,),
            )
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM uploads WHERE session_id = ?", (session_id,)
            ).fetchone()["n"]
            return True, count

        sess = conn.execute("SELECT finalized_at FROM sessions WHERE id = ?", (session_id,)).fetchone()
        kit = conn.execute("SELECT rev FROM kits WHERE session_id = ?", (session_id,)).fetchone()
        if sess is None or kit is None:
            return False, "missing"
        if sess["finalized_at"]:
            return False, "finalized"
        if conn.execute(
            "SELECT 1 FROM kit_packs WHERE session_id = ? AND reserved_by IS NOT NULL LIMIT 1",
            (session_id,),
        ).fetchone():
            return False, "reserved"
        has_content = conn.execute(
            """SELECT EXISTS (SELECT 1 FROM kit_pack_items WHERE session_id = ?)
                      OR EXISTS (SELECT 1 FROM uploads WHERE session_id = ?) AS has_content""",
            (session_id, session_id),
        ).fetchone()["has_content"]
        if not has_content:
            return False, "empty"
        return False, "changed"


def reopen_kit(session_id, employee_name):
    """Turns a submitted kit back into an open one, so it can be changed and
    submitted again - someone typing the job number + name of a finished
    kit on the start page, or tapping "Reopen to make changes" on its page.
    True if it was submitted and now isn't; False if it's gone or already
    open (e.g. a colleague reopened it a moment earlier - open either way).

    Nothing already submitted is undone: its photos keep status 'finalized'
    (they're in Drive, or on their way - which is also what makes them
    undeletable from now on, see delete_kit_upload), and the Pack Log Sheet
    keeps showing the last submission until the next Final Submit rewrites
    it. A Sheet write still pending from that last submission simply waits,
    since pending_kit_sheets only lists submitted kits. Any stale pack
    reservation is cleared so every pack starts out free - none can exist
    on a submitted kit through the app, but a leftover must never block the
    next Final Submit. ONE transaction, and the reopen itself is the
    conditional UPDATE, so two people reopening at once count once."""
    conn = get_conn()
    with conn:
        cur = conn.execute(
            """UPDATE sessions SET finalized_at = NULL
               WHERE id = ? AND finalized_at IS NOT NULL
                 AND EXISTS (SELECT 1 FROM kits WHERE session_id = sessions.id)""",
            (session_id,),
        )
        if cur.rowcount != 1:
            return False
        conn.execute(
            """UPDATE kits SET reopened_at = ?, reopened_by = ?, reopen_count = reopen_count + 1
               WHERE session_id = ?""",
            (now_iso(), employee_name, session_id),
        )
        conn.execute(
            """UPDATE kit_packs SET reserved_by = NULL, reserved_ts = NULL
               WHERE session_id = ? AND reserved_by IS NOT NULL""",
            (session_id,),
        )
        _bump_rev(conn, session_id)
    return True


def rename_kit(session_id, kit_name, kit_norm):
    """Renames an OPEN kit (a submitted one is reopened first - reopen_kit).
    Returns "renamed" | "same" (nothing to change) | "taken" (another kit of
    the same job already has that name - kits are identified by job + name,
    so two can't share one) | "sheet_taken" (another kit's Drive Sheet still
    carries that name - see kit_holding_sheet_name) | "finalized" | "missing". A change of
    capitalisation only ("store 12" -> "Store 12") is a rename of the same
    kit, not a clash with itself. The UNIQUE (job_number, kit_norm) index is
    the real guard against two people renaming two kits to the same name at
    once; the loser's IntegrityError becomes "taken". The Pack Log Sheet
    follows on the next Final Submit (see sheet_kit_name)."""
    conn = get_conn()
    job = conn.execute("SELECT job_number FROM kits WHERE session_id = ?", (session_id,)).fetchone()
    if job is not None and kit_holding_sheet_name(job["job_number"], kit_norm, exclude_session_id=session_id):
        return "sheet_taken"
    try:
        with conn:
            cur = conn.execute(
                """UPDATE kits SET kit_name = ?, kit_norm = ?
                   WHERE session_id = ? AND kit_name != ?
                     AND EXISTS (SELECT 1 FROM sessions WHERE id = kits.session_id AND finalized_at IS NULL)""",
                (kit_name, kit_norm, session_id, kit_name),
            )
            if cur.rowcount == 1:
                _bump_rev(conn, session_id)
                return "renamed"
            row = conn.execute(
                """SELECT k.kit_name, s.finalized_at FROM kits k JOIN sessions s ON s.id = k.session_id
                   WHERE k.session_id = ?""",
                (session_id,),
            ).fetchone()
            if row is None:
                return "missing"
            if row["finalized_at"]:
                return "finalized"
            return "same"
    except sqlite3.IntegrityError:
        return "taken"


def edit_kit(session_id, job_number, kit_name, kit_norm):
    """Moves an OPEN kit to another job number - and renames it, if the name
    changed too - in ONE transaction: the kit, its session and every one of
    its photos' rows. app.py has already moved the photo files on disk.
    Returns "changed" | "same" | "taken" (the new job already has a kit of
    that name - the UNIQUE (job_number, kit_norm) index is the real guard)
    | "sheet_taken" (another kit's Drive Sheet still carries that job +
    name, see kit_holding_sheet_name) | "finalized" | "missing".

    Drive isn't touched here: the kit's photos only ever reach Drive through
    a Final Submit, and the next one moves whatever an earlier submission
    left in the old job's folder (sheet_job_number, remembered here the
    first time the kit leaves the job its Sheet was written under). Drive
    links are by file id, so they stay valid when the files move."""
    conn = get_conn()
    if kit_holding_sheet_name(job_number, kit_norm, exclude_session_id=session_id):
        return "sheet_taken"
    try:
        with conn:
            cur = conn.execute(
                """UPDATE kits SET job_number = ?, kit_name = ?, kit_norm = ?,
                          sheet_job_number = CASE WHEN sheet_kit_name IS NOT NULL
                                                  THEN COALESCE(sheet_job_number, job_number) END
                   WHERE session_id = ? AND (job_number != ? OR kit_name != ?)
                     AND EXISTS (SELECT 1 FROM sessions WHERE id = kits.session_id AND finalized_at IS NULL)""",
                (job_number, kit_name, kit_norm, session_id, job_number, kit_name),
            )
            if cur.rowcount == 1:
                conn.execute("UPDATE sessions SET job_number = ? WHERE id = ?", (job_number, session_id))
                conn.execute("UPDATE uploads SET job_number = ? WHERE session_id = ?", (job_number, session_id))
                _bump_rev(conn, session_id)
                return "changed"
            row = conn.execute(
                """SELECT s.finalized_at FROM kits k JOIN sessions s ON s.id = k.session_id
                   WHERE k.session_id = ?""",
                (session_id,),
            ).fetchone()
            if row is None:
                return "missing"
            if row["finalized_at"]:
                return "finalized"
            return "same"
    except sqlite3.IntegrityError:
        return "taken"


def mark_packs_printed(session_id, pack_ids, employee_name):
    """Stamps "last printed at / by" and bumps the print count on the packs
    whose labels were just sent to the printer - only packs of this kit (a
    stray id from another kit is ignored), submitted kits included (a box
    can need a fresh label at any time). Printing isn't an edit, so the
    packs' first/last edit and contributors are untouched; it does bump rev,
    since every phone's pack cards show the "printed" note. Returns how many
    packs were marked."""
    pack_ids = [int(pid) for pid in pack_ids]
    if not pack_ids:
        return 0
    conn = get_conn()
    placeholders = ",".join("?" * len(pack_ids))
    with conn:
        cur = conn.execute(
            f"""UPDATE kit_packs
                   SET last_printed_at = ?, last_printed_by = ?, print_count = print_count + 1
                 WHERE session_id = ? AND id IN ({placeholders})""",
            [now_iso(), employee_name, session_id, *pack_ids],
        )
        marked = cur.rowcount
        if marked:
            _bump_rev(conn, session_id)
    return marked


def packs_for_kit(session_id):
    conn = get_conn()
    return conn.execute(
        "SELECT * FROM kit_packs WHERE session_id = ? ORDER BY pack_number ASC", (session_id,)
    ).fetchall()


def get_pack(pack_id):
    conn = get_conn()
    return conn.execute("SELECT * FROM kit_packs WHERE id = ?", (pack_id,)).fetchone()


def held_pack(session_id, employee_name):
    """The pack this person is editing in this kit, or None. One person
    holds at most one pack per kit (reserve/create release the others in the
    same transaction); the ORDER BY just makes a stray double hold - which
    release_packs_held_by heals - resolve predictably to the newest pack."""
    conn = get_conn()
    return conn.execute(
        """SELECT * FROM kit_packs WHERE session_id = ? AND reserved_by = ?
           ORDER BY pack_number DESC LIMIT 1""",
        (session_id, employee_name),
    ).fetchone()


def held_packs_by_user(employee_name):
    """{session_id: pack_number} for every unsubmitted kit where this person
    still holds a pack - the start page's "You still have Pack 3 open"
    reminder with its [Release] button (a pack left reserved by someone who
    closed the tab blocks Final Submit for everyone)."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT p.session_id AS session_id, MAX(p.pack_number) AS pack_number
           FROM kit_packs p JOIN sessions s ON s.id = p.session_id
           WHERE p.reserved_by = ? AND s.finalized_at IS NULL
           GROUP BY p.session_id""",
        (employee_name,),
    ).fetchall()
    return {row["session_id"]: row["pack_number"] for row in rows}


def _bump_rev(conn, session_id):
    """Non-committing - part of whatever kit change the caller is making.
    Heartbeats deliberately don't bump it (nothing a phone draws changed)."""
    conn.execute("UPDATE kits SET rev = rev + 1 WHERE session_id = ?", (session_id,))


def _heartbeat(conn, pack_id, session_id, employee_name):
    """Non-committing. Refreshes the holder's heartbeat and, in the same
    statement, answers "does this person still hold this pack in an
    unsubmitted kit?" - rowcount 1 means yes. Being a write, it also takes
    the write lock, so whatever the caller does next in this transaction
    sees a pack nobody else can change underneath it."""
    cur = conn.execute(
        f"""UPDATE kit_packs SET reserved_ts = ?
            WHERE id = ? AND session_id = ? AND reserved_by = ? AND {_KIT_OPEN}""",
        (_now_ts(), pack_id, session_id, employee_name, session_id),
    )
    return cur.rowcount == 1


def _delete_if_empty_and_last(conn, pack_id):
    """Non-committing. A pack someone opened and then left without adding
    anything is just noise - but only the highest-numbered one is removed,
    since deleting "Pack 3" out of 1..5 would leave a hole (packs are never
    renumbered; boxes may already be labelled). One conditional DELETE, so a
    pack that gained an item or photo, or was reserved again, a moment ago is
    never removed."""
    cur = conn.execute(
        """DELETE FROM kit_packs
           WHERE id = ? AND reserved_by IS NULL
             AND NOT EXISTS (SELECT 1 FROM kit_pack_items i WHERE i.pack_id = kit_packs.id)
             AND NOT EXISTS (SELECT 1 FROM uploads u WHERE u.pack_id = kit_packs.id)
             AND pack_number = (SELECT MAX(p2.pack_number) FROM kit_packs p2
                                WHERE p2.session_id = kit_packs.session_id)""",
        (pack_id,),
    )
    return cur.rowcount


def _release_held(conn, session_id, employee_name, except_pack_id=None):
    """Non-committing. Releases every pack this person holds in the kit
    (except `except_pack_id`, the one they're moving into), then drops any of
    those left empty-and-last. One UPDATE ... RETURNING rather than
    "find them, then release them": as the first write of a transaction it
    waits for any other writer and then sees the latest data, so a
    double-tapped "+ New Pack" can't end up holding two packs (the second
    request releases - and, being empty, deletes - the pack the first just
    created). Highest pack first, so releasing an empty 5 and 6 together
    removes both. Returns how many packs were released."""
    sql = """UPDATE kit_packs SET reserved_by = NULL, reserved_ts = NULL
             WHERE session_id = ? AND reserved_by = ?"""
    params = [session_id, employee_name]
    if except_pack_id is not None:
        sql += " AND id != ?"
        params.append(except_pack_id)
    released = conn.execute(sql + " RETURNING id, pack_number", params).fetchall()
    for row in sorted(released, key=lambda r: r["pack_number"], reverse=True):
        _delete_if_empty_and_last(conn, row["id"])
    return len(released)


def _touch_pack(conn, pack_id, employee_name):
    """Non-committing - called right after a hold-checked item/photo change
    in the same transaction: adds the person to the pack's contributors,
    stamps first/last edit (the Sheet's "First Edit"/"Last Edit"), and
    refreshes their heartbeat (actively working is the opposite of idle)."""
    row = conn.execute("SELECT contributors FROM kit_packs WHERE id = ?", (pack_id,)).fetchone()
    if row is None:
        return
    contributors = split_list(row["contributors"])
    if employee_name not in contributors:
        contributors.append(employee_name)
    now = now_iso()
    conn.execute(
        """UPDATE kit_packs
           SET contributors = ?, first_edit_at = COALESCE(first_edit_at, ?), last_edit_at = ?,
               reserved_ts = ?
           WHERE id = ?""",
        (_join_list(contributors), now, now, _now_ts(), pack_id),
    )


def create_pack(session_id, employee_name):
    """"+ New Pack": moves this person out of whatever pack they hold and
    into a brand-new one numbered MAX+1, reserved for them - one transaction,
    so they never hold two packs, and the number is taken while holding the
    write lock. The INSERT ... SELECT (no FROM) inserts nothing at all for a
    submitted or deleted kit (an aggregate over kit_packs in the outer SELECT
    would still produce a row). The retry is only for an IntegrityError on
    the (session_id, pack_number) index - not expected, since the number is
    computed under the write lock, but cheap insurance. Returns the new pack
    row, or None if the kit is submitted or gone."""
    conn = get_conn()
    for attempt in range(5):
        try:
            with conn:
                _release_held(conn, session_id, employee_name)
                cur = conn.execute(
                    """INSERT INTO kit_packs
                       (session_id, pack_number, reserved_by, reserved_ts, contributors, created_by, created_at)
                       SELECT ?, (SELECT COALESCE(MAX(pack_number), 0) + 1 FROM kit_packs WHERE session_id = ?),
                              ?, ?, ?, ?, ?
                        WHERE EXISTS (SELECT 1 FROM sessions WHERE id = ? AND finalized_at IS NULL)""",
                    (
                        session_id, session_id, employee_name, _now_ts(), "", employee_name, now_iso(),
                        session_id,
                    ),
                )
                if cur.rowcount == 0:
                    return None
                pack_id = cur.lastrowid
                _bump_rev(conn, session_id)
            return get_pack(pack_id)
        except sqlite3.IntegrityError:
            if attempt == 4:
                raise
    return None


def reserve_pack(pack_id, session_id, employee_name, idle_sec, force=False):
    """Opens a pack for editing. Allowed when it's free, already this
    person's, or - only with force=True, i.e. after they confirmed "Mike has
    been idle for 14 min... take it over?" - held by someone whose last
    heartbeat is older than idle_sec. All of that is the one UPDATE's WHERE,
    so two people tapping the same free pack can't both get it. On success
    the person's previous pack is released in the same transaction (one pack
    per person). Returns (status, pack_row): "ok" | "taken" | "idle" |
    "finalized" | "missing", with failures classified inside the same
    transaction ("idle" = held by someone idle, so a forced retry would work)."""
    conn = get_conn()
    now_ts = _now_ts()
    idle_cutoff = now_ts - idle_sec
    with conn:
        cur = conn.execute(
            f"""UPDATE kit_packs SET reserved_by = ?, reserved_ts = ?
                WHERE id = ? AND session_id = ?
                  AND (reserved_by IS NULL OR reserved_by = ? OR (? = 1 AND reserved_ts < ?))
                  AND EXISTS (SELECT 1 FROM sessions WHERE id = kit_packs.session_id AND finalized_at IS NULL)""",
            (employee_name, now_ts, pack_id, session_id, employee_name, 1 if force else 0, idle_cutoff),
        )
        if cur.rowcount == 1:
            _release_held(conn, session_id, employee_name, except_pack_id=pack_id)
            _bump_rev(conn, session_id)
            return "ok", conn.execute("SELECT * FROM kit_packs WHERE id = ?", (pack_id,)).fetchone()

        row = conn.execute(
            "SELECT * FROM kit_packs WHERE id = ? AND session_id = ?", (pack_id, session_id)
        ).fetchone()
        if row is None:
            return "missing", None
        sess = conn.execute("SELECT finalized_at FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if sess is None:
            return "missing", None
        if sess["finalized_at"]:
            return "finalized", row
        if row["reserved_ts"] is not None and row["reserved_ts"] < idle_cutoff:
            return "idle", row
        return "taken", row


def release_packs_held_by(session_id, employee_name):
    """Save Pack / leaving the page / the start page's [Release]: frees
    EVERY pack this person holds in the kit (not just the one the phone
    thinks it has - that also heals any stray double hold), dropping ones
    left empty-and-last. Idempotent: releasing nothing is fine and doesn't
    bump rev. Returns how many were released."""
    conn = get_conn()
    with conn:
        released = _release_held(conn, session_id, employee_name)
        if released:
            _bump_rev(conn, session_id)
    return released


def release_all_packs_held_by(employee_name):
    """Log out: frees every pack this person holds in every unsubmitted kit
    - these are shared shift phones, so "log out and hand the phone over"
    mid-kit is normal, and must not leave a pack blocking Final Submit."""
    conn = get_conn()
    with conn:
        released = conn.execute(
            """UPDATE kit_packs SET reserved_by = NULL, reserved_ts = NULL
               WHERE reserved_by = ?
                 AND EXISTS (SELECT 1 FROM sessions WHERE id = kit_packs.session_id AND finalized_at IS NULL)
               RETURNING id, session_id, pack_number""",
            (employee_name,),
        ).fetchall()
        for row in sorted(released, key=lambda r: (r["session_id"], -r["pack_number"])):
            _delete_if_empty_and_last(conn, row["id"])
        for session_id in {row["session_id"] for row in released}:
            _bump_rev(conn, session_id)
    return len(released)


def heartbeat_pack(pack_id, session_id, employee_name):
    """True if this person still holds the pack (and the kit is still open),
    refreshing their heartbeat. The kit page's poll uses it to keep a
    reservation alive; /api/upload uses it as its hold gate BEFORE writing
    any file - and since a fresh heartbeat blocks an idle takeover for
    PACK_IDLE_TAKEOVER_SEC, nobody can take the pack out from under a photo
    that's mid-upload."""
    conn = get_conn()
    with conn:
        return _heartbeat(conn, pack_id, session_id, employee_name)


def items_for_kit(session_id):
    conn = get_conn()
    return conn.execute(
        "SELECT * FROM kit_pack_items WHERE session_id = ? ORDER BY id ASC", (session_id,)
    ).fetchall()


def add_pack_item(pack_id, session_id, series, series_norm, idx, employee_name):
    """Adds one item number to a pack - only if this person holds that pack
    in an unsubmitted kit, checked in the INSERT itself. OR IGNORE + the
    unique (pack_id, series_norm, idx) index turn a repeat into a no-op.
    Returns "added" | "duplicate" (still holding - heartbeat refreshed, the
    item was already there) | "not_holding"."""
    conn = get_conn()
    with conn:
        cur = conn.execute(
            f"""INSERT OR IGNORE INTO kit_pack_items
                (pack_id, session_id, series, series_norm, idx, added_by, added_at)
                SELECT ?, ?, ?, ?, ?, ?, ?
                 WHERE EXISTS (SELECT 1 FROM kit_packs WHERE id = ? AND session_id = ? AND reserved_by = ?)
                   AND {_KIT_OPEN}""",
            (
                pack_id, session_id, series, series_norm, idx, employee_name, now_iso(),
                pack_id, session_id, employee_name, session_id,
            ),
        )
        if cur.rowcount == 1:
            _touch_pack(conn, pack_id, employee_name)
            _bump_rev(conn, session_id)
            return "added"
        if _heartbeat(conn, pack_id, session_id, employee_name):
            return "duplicate"
        return "not_holding"


def remove_pack_item(item_id, pack_id, session_id, employee_name):
    """Removes one item from a pack this person holds. The hold is checked
    first (and, being a write, locks out everyone else for the rest of this
    transaction); an item that's already gone - a double-tapped × - is
    "gone", which callers treat as success. Returns "removed" | "gone" |
    "not_holding"."""
    conn = get_conn()
    with conn:
        if not _heartbeat(conn, pack_id, session_id, employee_name):
            return "not_holding"
        cur = conn.execute(
            "DELETE FROM kit_pack_items WHERE id = ? AND pack_id = ? AND session_id = ?",
            (item_id, pack_id, session_id),
        )
        if cur.rowcount == 0:
            return "gone"
        _touch_pack(conn, pack_id, employee_name)
        _bump_rev(conn, session_id)
        return "removed"


def add_kit_upload(session_id, job_number, employee_name, category, filename, thumb_filename, pack_id):
    """add_upload for a kit photo: the row is only inserted if this person
    still holds the pack in an unsubmitted kit - checked in the INSERT
    itself, since the file write before it takes long enough (~1s) for the
    kit to be submitted or deleted meanwhile. Returns the upload id, or None
    (the caller then deletes the files it just wrote). sheet_status starts
    'pending': a kit photo's local copy is only cleaned up once its link has
    reached the kit's Pack Log Sheet (see mark_kit_sheet_written)."""
    conn = get_conn()
    with conn:
        cur = conn.execute(
            f"""INSERT INTO uploads
                (session_id, job_number, employee_name, category, filename, thumb_filename, uploaded_at,
                 pack_id, sheet_status)
                SELECT ?, ?, ?, ?, ?, ?, ?, ?, 'pending'
                 WHERE EXISTS (SELECT 1 FROM kit_packs WHERE id = ? AND session_id = ? AND reserved_by = ?)
                   AND {_KIT_OPEN}""",
            (
                session_id, job_number, employee_name, category, filename, thumb_filename, now_iso(),
                pack_id, pack_id, session_id, employee_name, session_id,
            ),
        )
        if cur.rowcount != 1:
            return None
        upload_id = cur.lastrowid
        _touch_pack(conn, pack_id, employee_name)
        _bump_rev(conn, session_id)
    return upload_id


def add_kit_general_upload(session_id, job_number, employee_name, category, filename, thumb_filename):
    """A kit photo that belongs to NO pack (pack_id NULL) - the kit page's
    "Kit photos" section, for pictures of the whole kit, a pallet, a
    delivery note... No pack reservation is involved (several people can add
    these at once); the only guard, checked in the INSERT itself like
    add_kit_upload, is that the kit is still unsubmitted. Returns the upload
    id, or None (the caller deletes the files it wrote)."""
    conn = get_conn()
    with conn:
        cur = conn.execute(
            f"""INSERT INTO uploads
                (session_id, job_number, employee_name, category, filename, thumb_filename, uploaded_at,
                 pack_id, sheet_status)
                SELECT ?, ?, ?, ?, ?, ?, ?, NULL, 'pending'
                 WHERE {_KIT_OPEN}""",
            (session_id, job_number, employee_name, category, filename, thumb_filename, now_iso(), session_id),
        )
        if cur.rowcount != 1:
            return None
        upload_id = cur.lastrowid
        _bump_rev(conn, session_id)
    return upload_id


def delete_kit_general_upload(upload_id, session_id, employee_name, is_admin=False):
    """Removes a no-pack kit photo - only its uploader (or an admin) may,
    only while the kit is unsubmitted and the photo itself was never
    submitted; all checked in the DELETE. Returns (status, row):
    "deleted" | "gone" | "finalized" | "locked" | "not_owner" | "in_pack"
    (the id is a PACK photo - those go through delete_kit_upload's hold rule)."""
    conn = get_conn()
    with conn:
        deleted = conn.execute(
            f"""DELETE FROM uploads
                WHERE id = ? AND session_id = ? AND pack_id IS NULL AND status != 'finalized'
                  AND (? = 1 OR employee_name = ?)
                  AND {_KIT_OPEN}
                RETURNING *""",
            (upload_id, session_id, 1 if is_admin else 0, employee_name, session_id),
        ).fetchall()
        if deleted:
            _bump_rev(conn, session_id)
            return "deleted", deleted[0]
        sess = conn.execute("SELECT finalized_at FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if sess is not None and sess["finalized_at"]:
            return "finalized", None
        row = conn.execute(
            "SELECT * FROM uploads WHERE id = ? AND session_id = ?", (upload_id, session_id)
        ).fetchone()
        if row is None:
            return "gone", None
        if row["pack_id"] is not None:
            return "in_pack", row
        if row["status"] == "finalized":
            return "locked", row
        return "not_owner", row


def general_photo_uploaders(session_id):
    """Everyone who added a no-pack kit photo, first upload first - they
    count as having worked on the kit (see merge_collaborators)."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT employee_name, MIN(id) AS first_id FROM uploads
           WHERE session_id = ? AND pack_id IS NULL
           GROUP BY employee_name ORDER BY first_id ASC""",
        (session_id,),
    ).fetchall()
    return [row["employee_name"] for row in rows]


def delete_kit_upload(upload_id, session_id, employee_name):
    """Removes a kit photo's row - only if its pack is held by this person,
    the kit is unsubmitted AND the photo itself was never submitted, all
    checked in the DELETE itself. Returns (status, row): "deleted" (caller
    removes the files) | "gone" (already deleted - a double tap; treated as
    success) | "finalized" (the kit is submitted) | "locked" (the kit was
    reopened, but this photo went to Drive with an earlier submission - it's
    in the job's Drive folder and linked from the Pack Log, so it stays) |
    "not_holding"."""
    conn = get_conn()
    with conn:
        deleted = conn.execute(
            f"""DELETE FROM uploads
                WHERE id = ? AND session_id = ? AND status != 'finalized'
                  AND EXISTS (SELECT 1 FROM kit_packs p
                              WHERE p.id = uploads.pack_id AND p.session_id = uploads.session_id
                                AND p.reserved_by = ?)
                  AND {_KIT_OPEN}
                RETURNING *""",
            (upload_id, session_id, employee_name, session_id),
        ).fetchall()
        if deleted:
            row = deleted[0]
            _touch_pack(conn, row["pack_id"], employee_name)
            _bump_rev(conn, session_id)
            return "deleted", row

        sess = conn.execute("SELECT finalized_at FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if sess is not None and sess["finalized_at"]:
            return "finalized", None
        row = conn.execute(
            "SELECT * FROM uploads WHERE id = ? AND session_id = ?", (upload_id, session_id)
        ).fetchone()
        if row is None:
            return "gone", None
        if row["status"] == "finalized":
            return "locked", row
        return "not_holding", row


# --- Kit Pack Log Sheet bookkeeping (drive_sync.py) ------------------------

def pending_kit_sheets():
    """Submitted kits whose Pack Log Sheet still needs (re)writing and whose
    retry time, if any, has come."""
    conn = get_conn()
    return conn.execute(
        """SELECT k.*, s.finalized_at AS finalized_at, s.started_at AS started_at
           FROM kits k JOIN sessions s ON s.id = k.session_id
           WHERE s.finalized_at IS NOT NULL
             AND k.sheet_status IN ('pending', 'error')
             AND (k.sheet_retry_after IS NULL OR k.sheet_retry_after <= ?)
           ORDER BY s.finalized_at ASC""",
        (now_iso(),),
    ).fetchall()


def kit_upload_sync_counts(session_id):
    """(total, synced) over the kit's submitted photos - the Sheet is only
    "complete" once every one of them has a Drive link to list."""
    conn = get_conn()
    row = conn.execute(
        """SELECT COUNT(*) AS total,
                  COALESCE(SUM(CASE WHEN drive_status = 'synced' THEN 1 ELSE 0 END), 0) AS synced
           FROM uploads WHERE session_id = ? AND status = 'finalized'""",
        (session_id,),
    ).fetchone()
    return row["total"], row["synced"]


def synced_kit_uploads(session_id):
    """The kit's submitted photos that are on Drive (with a file id to link
    to), oldest first, with the pack each belongs to."""
    conn = get_conn()
    return conn.execute(
        """SELECT id, pack_id, drive_file_id, filename, employee_name, uploaded_at
           FROM uploads
           WHERE session_id = ? AND status = 'finalized' AND drive_status = 'synced'
             AND drive_file_id IS NOT NULL AND drive_file_id != ''
           ORDER BY id ASC""",
        (session_id,),
    ).fetchall()


def mark_kit_sheet_written(session_id, upload_ids, synced_count, complete, retry_after_iso=None, note=None,
                           sheet_kit_name=None, sheet_job_number=None):
    """After a successful Pack Log write: exactly the uploads whose links were
    in it are marked sheet-synced (which is what lets local_cleanup remove
    their local copies), and the kit remembers how many synced photos that
    write included (so a partial Sheet is only rewritten once that number
    changes). complete -> the kit's Sheet is done (note, if given, records
    why it was finished early, e.g. "2 photo(s) never reached Drive"); else
    it stays 'pending' until retry_after_iso. sheet_synced_at is set either
    way - it's "a Sheet has been written", partial or not. sheet_kit_name is
    the kit name the Sheet was just written under (the name that was SENT,
    not re-read here - see the sheet_kit_name column); None leaves it as is
    (drive_sync's "nothing changed, check again later" call writes no Sheet)."""
    conn = get_conn()
    now = now_iso()
    with conn:
        if sheet_kit_name is not None:
            conn.execute(
                "UPDATE kits SET sheet_kit_name = ? WHERE session_id = ?", (sheet_kit_name, session_id)
            )
        if sheet_job_number is not None:
            conn.execute(
                "UPDATE kits SET sheet_job_number = ? WHERE session_id = ?", (sheet_job_number, session_id)
            )
        conn.executemany(
            """UPDATE uploads SET sheet_status = 'synced', sheet_synced_at = ?, sheet_error = NULL
               WHERE id = ? AND session_id = ?""",
            [(now, upload_id, session_id) for upload_id in upload_ids],
        )
        if complete:
            conn.execute(
                """UPDATE kits SET sheet_status = 'synced', sheet_synced_at = ?, sheet_synced_count = ?,
                                  sheet_error = ?, sheet_retry_after = NULL
                   WHERE session_id = ?""",
                (now, synced_count, note, session_id),
            )
        else:
            conn.execute(
                """UPDATE kits SET sheet_status = 'pending', sheet_synced_at = ?, sheet_synced_count = ?,
                                  sheet_error = ?, sheet_retry_after = ?
                   WHERE session_id = ?""",
                (now, synced_count, note, retry_after_iso, session_id),
            )


def record_kit_sheet_name(session_id, kit_name, job_number=None):
    """Just the name (and job folder) the kit's Pack Log file now has - for a
    write whose other bookkeeping had to be dropped (see drive_sync._log_kit)."""
    conn = get_conn()
    with conn:
        conn.execute("UPDATE kits SET sheet_kit_name = ? WHERE session_id = ?", (kit_name, session_id))
        if job_number is not None:
            conn.execute("UPDATE kits SET sheet_job_number = ? WHERE session_id = ?", (job_number, session_id))


def record_kit_labels(session_id, file_id, submit_count):
    """The labels PDF Drive now holds for this kit ("" = none, e.g. every
    pack empty) and the submission it belongs to - drive_sync sends a new
    PDF only when a later submission has come along."""
    conn = get_conn()
    with conn:
        conn.execute(
            "UPDATE kits SET labels_file_id = ?, labels_submit_count = ? WHERE session_id = ?",
            (file_id, submit_count, session_id),
        )


def mark_kit_sheet_error(session_id, error_text, retry_after_iso):
    conn = get_conn()
    with conn:
        conn.execute(
            """UPDATE kits SET sheet_status = 'error', sheet_error = ?, sheet_retry_after = ?
               WHERE session_id = ?""",
            (error_text, retry_after_iso, session_id),
        )


# --- Users (login accounts) -----------------------------------------------

def create_user(name, role="standard", pin_hash=None, active=1):
    """Raises sqlite3.IntegrityError on a case-insensitive name collision -
    callers (signup/admin-create) check find_user_by_name first, but the
    UNIQUE index is the real guard against a race between two people signing
    up with the same name at once (same pattern as find_or_create_consignment).

    `active` doubles as "approved by an admin" - an admin creating a user
    directly is implicit approval (active=1, the default), but self-signup
    (see app.py's /login) passes active=0 so the account can't choose a PIN
    (and therefore can't log in) until an admin approves it from /admin."""
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO users (name, name_norm, pin_hash, role, active, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (name, name.strip().casefold(), pin_hash, role, int(bool(active)), now_iso()),
    )
    conn.commit()
    return cur.lastrowid


def delete_user(user_id):
    """Hard delete - employee_name on uploads/sessions/consignments is a
    plain denormalized string, not a foreign key to users.id, so removing
    the account can never orphan or corrupt historical records."""
    conn = get_conn()
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()


def find_user_by_name(name):
    conn = get_conn()
    return conn.execute(
        "SELECT * FROM users WHERE name_norm = ?", (name.strip().casefold(),)
    ).fetchone()


def get_user(user_id):
    conn = get_conn()
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def list_users():
    conn = get_conn()
    return conn.execute("SELECT * FROM users ORDER BY name COLLATE NOCASE ASC").fetchall()


def list_active_user_names():
    """Powers the login page's name dropdown - active accounts only, so a
    disabled one doesn't show up as something you can still pick."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT name FROM users WHERE active = 1 ORDER BY name COLLATE NOCASE ASC"
    ).fetchall()
    return [row["name"] for row in rows]


def set_pin(user_id, pin_hash):
    """Used both for a brand-new account's first PIN and to complete a
    post-reset PIN choice - either way this is what clears the NULL that put
    the account in 'must choose a PIN' state."""
    conn = get_conn()
    conn.execute(
        """UPDATE users SET pin_hash = ?, failed_attempts = 0, locked_at = NULL
           WHERE id = ?""",
        (pin_hash, user_id),
    )
    conn.commit()


def record_failed_login(user_id, max_attempts):
    """Bumps the failed-attempt counter and locks the account once it
    reaches max_attempts. The only way out of a lock is an admin PIN reset
    (reset_pin below) - there's no time-based cooldown, so a lock can't
    silently clear itself."""
    conn = get_conn()
    row = conn.execute("SELECT failed_attempts FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        return
    attempts = row["failed_attempts"] + 1
    locked_at = now_iso() if attempts >= max_attempts else None
    conn.execute(
        "UPDATE users SET failed_attempts = ?, locked_at = ? WHERE id = ?",
        (attempts, locked_at, user_id),
    )
    conn.commit()


def clear_failed_attempts(user_id):
    conn = get_conn()
    conn.execute(
        "UPDATE users SET failed_attempts = 0, locked_at = NULL WHERE id = ?", (user_id,)
    )
    conn.commit()


def reset_pin(user_id):
    """Admin action: puts the account back into 'must choose a PIN' state
    and clears any lockout - the only way a locked account becomes usable
    again."""
    conn = get_conn()
    conn.execute(
        """UPDATE users SET pin_hash = NULL, failed_attempts = 0, locked_at = NULL
           WHERE id = ?""",
        (user_id,),
    )
    conn.commit()


def set_user_active(user_id, active):
    conn = get_conn()
    conn.execute("UPDATE users SET active = ? WHERE id = ?", (int(bool(active)), user_id))
    conn.commit()


def set_user_role(user_id, role):
    conn = get_conn()
    conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
    conn.commit()
