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
    conn.commit()


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def ensure_job(job_number):
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO jobs (job_number, created_at) VALUES (?, ?)",
        (job_number, now_iso()),
    )
    conn.commit()


def create_session(session_id, job_number, employee_name, category):
    conn = get_conn()
    conn.execute(
        """INSERT INTO sessions (id, job_number, employee_name, started_at, category)
           VALUES (?, ?, ?, ?, ?)""",
        (session_id, job_number, employee_name, now_iso(), category),
    )
    conn.commit()


def get_session(session_id):
    conn = get_conn()
    return conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()


def add_upload(session_id, job_number, employee_name, category, filename, thumb_filename):
    conn = get_conn()
    cur = conn.execute(
        """INSERT INTO uploads
           (session_id, job_number, employee_name, category, filename, thumb_filename, uploaded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (session_id, job_number, employee_name, category, filename, thumb_filename, now_iso()),
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
    conn = get_conn()
    return conn.execute(
        """SELECT * FROM uploads WHERE job_number = ?
           ORDER BY uploaded_at ASC""",
        (job_number,),
    ).fetchall()


def pending_drive_uploads(limit=10):
    conn = get_conn()
    return conn.execute(
        """SELECT * FROM uploads
           WHERE status = 'finalized'
             AND drive_status IN ('pending', 'error')
             AND (retry_after IS NULL OR retry_after <= ?)
           ORDER BY id ASC LIMIT ?""",
        (now_iso(), limit),
    ).fetchall()


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
    conn = get_conn()
    return conn.execute(
        """SELECT * FROM uploads
           WHERE drive_status = 'synced'
             AND local_deleted_at IS NULL
             AND drive_synced_at IS NOT NULL
             AND drive_synced_at <= ?
           ORDER BY id ASC LIMIT ?""",
        (cutoff_iso, limit),
    ).fetchall()


def mark_local_cleaned(upload_id):
    conn = get_conn()
    conn.execute(
        "UPDATE uploads SET local_deleted_at = ? WHERE id = ?", (now_iso(), upload_id)
    )
    conn.commit()
