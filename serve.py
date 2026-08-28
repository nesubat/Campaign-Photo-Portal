"""
Production entry point - use this instead of `py app.py` for real use.

Flask's own dev server (what app.py runs directly) explicitly warns it
isn't meant to stay up all day serving multiple people - waitress is a
plain WSGI server with no such caveat, and needs no compiler on Windows.

Run:  py serve.py
"""
import db
import drive_sync
import local_cleanup
from app import app
from config import HOST, PORT, load_drive_config
from waitress import serve

if __name__ == "__main__":
    db.init_db()
    if load_drive_config():
        print("[startup] Drive sync configured - background relay starting.")
    else:
        print(
            "[startup] Drive sync NOT configured yet (data/drive_config.json missing "
            "or incomplete). Photos will save locally only until you deploy "
            "apps-script/DriveUploader.gs and fill that file in."
        )
    drive_sync.start_background_sync()
    local_cleanup.start_background_cleanup()
    print(f"[startup] Serving on http://{HOST}:{PORT} (waitress)")
    serve(app, host=HOST, port=PORT, threads=8)
