"""
SQLite storage for the Campaign Photo Portal.
One connection per request (Flask app_context), WAL mode so uploads from
several phones at once don't block each other.
"""
import sqlite3
import threading
from datetime import datetime, timezone

from config import DB_PATH

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
        """
    )
    conn.commit()
    _migrate(conn)


def _add_column_if_missing(conn, table, column, ddl):
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


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
    used by the supervisor gallery, which is meant to show everything."""
    conn = get_conn()
    return conn.execute(
        """SELECT u.*, c.key_value AS consignment_value, c.item_ids AS consignment_item_ids
           FROM uploads u
           LEFT JOIN consignments c ON c.id = u.consignment_id
           WHERE u.job_number = ?
           ORDER BY u.uploaded_at ASC""",
        (job_number,),
    ).fetchall()


def uploads_for_session(session_id):
    """Only this session's own photos - used by the live upload page, so a
    worker doesn't see (or risk deleting) photos from someone else's earlier
    or concurrent session on the same job."""
    conn = get_conn()
    return conn.execute(
        """SELECT u.*, c.key_value AS consignment_value, c.item_ids AS consignment_item_ids
           FROM uploads u
           LEFT JOIN consignments c ON c.id = u.consignment_id
           WHERE u.session_id = ?
           ORDER BY u.uploaded_at ASC""",
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
    the loop take unbounded time - the remainder is picked up on later ticks."""
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM uploads
           WHERE status = 'finalized'
             AND drive_status IN ('pending', 'error')
             AND (retry_after IS NULL OR retry_after <= ?)
           ORDER BY id ASC LIMIT ?""",
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
