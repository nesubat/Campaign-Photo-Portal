"""
Production entry point - use this instead of `py app.py` for real use.

Flask's own dev server (what app.py runs directly) explicitly warns it
isn't meant to stay up all day serving multiple people - waitress is a
plain WSGI server with no such caveat, and needs no compiler on Windows.

Run:  py serve.py
"""
import socket

import db
import drive_sync
import local_cleanup
from app import app
from config import HOST, PORT, load_drive_config
from waitress import serve


def _detect_phone_address():
    """Best-effort single IPv4 address for phones to browse to.

    Windows Mobile Hotspot always self-assigns 192.168.137.1 to its
    adapter - checked for first, since when the hotspot's on, that's the
    address phones need even though this PC's own internet traffic keeps
    routing out some other way (e.g. Ethernet), so it wouldn't otherwise
    show up as "the" address. Otherwise, ask the OS which address it
    would use to reach the internet - correct for a normal WiFi/LAN or
    this PC joined to a phone's hotspot as a client.
    """
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("192.168.137."):
                return ip
    except socket.gaierror:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        ip = probe.getsockname()[0]
        probe.close()
        return ip
    except OSError:
        return None


if __name__ == "__main__":
    db.init_db()
    if load_drive_config():
        print("[startup] Drive sync configured - background relay starting.")
    else:
        print(
            "[startup] Drive sync NOT configured yet (.env missing or incomplete - see "
            ".env.example). Photos will save locally only until you deploy "
            "apps-script/DriveUploader.gs and fill that file in."
        )
    drive_sync.start_background_sync()
    local_cleanup.start_background_cleanup()

    phone_ip = _detect_phone_address()
    if phone_ip:
        print(f"[startup] Serving (waitress). Phones should browse to: http://{phone_ip}:{PORT}")
    else:
        print(f"[startup] Serving on http://{HOST}:{PORT} (waitress)")
        print(f"    Could not auto-detect this PC's address - run `ipconfig` to find it.")

    serve(app, host=HOST, port=PORT, threads=8)
