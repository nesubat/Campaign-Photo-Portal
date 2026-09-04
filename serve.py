"""
Production entry point - use this instead of `py app.py` for real use.

Flask's own dev server (what app.py runs directly) explicitly warns it
isn't meant to stay up all day serving multiple people - waitress is a
plain WSGI server with no such caveat, and needs no compiler on Windows.

Run:  py serve.py

On this machine, `py serve.py` is actually a three-process chain - py.exe
(the launcher), the venv's python.exe (itself just a small stub that
re-launches into the real interpreter below it), and the base install's
python.exe, which is the one that actually binds the port and runs
everything. Ctrl+C is supposed to stop all three, but Windows doesn't
always propagate the interrupt cleanly through a launcher -> stub -> real-
interpreter chain like this, so the outer two can exit (returning your
prompt, making it look like the server stopped) while the real interpreter
is left running in the background, still holding the port. _kill_stale_
port_holders() below clears out exactly that leftover before every start,
so a restart can never silently fail to take over and leave you talking to
old code without you knowing.
"""
import os
import socket
import subprocess
import time

import auth
import db
import drive_sync
import local_cleanup
from app import app
from config import HOST, PORT, WAITRESS_THREADS, load_drive_config
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


def _find_python_pids_on_port(port):
    """Windows-only, no extra dependency: PIDs of python.exe/py.exe/pythonw.exe
    processes currently LISTENING on `port`, via netstat + tasklist. Only
    ever matches something actually bound to OUR port (never an unrelated
    python process doing something else), and the tasklist name check means
    it never touches something else entirely that happens to be squatting
    on the port - see the file's own note above about py.exe/venv's launcher
    chain sometimes leaving the real interpreter running after Ctrl+C."""
    try:
        netstat_out = subprocess.check_output(
            ["netstat", "-ano", "-p", "TCP"], text=True, stderr=subprocess.DEVNULL
        )
    except (OSError, subprocess.CalledProcessError):
        return []

    candidate_pids = set()
    for line in netstat_out.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0] != "TCP" or parts[3] != "LISTENING":
            continue
        if parts[1].rsplit(":", 1)[-1] == str(port):
            try:
                candidate_pids.add(int(parts[-1]))
            except ValueError:
                pass

    python_pids = []
    for pid in candidate_pids:
        try:
            tasklist_out = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                text=True, stderr=subprocess.DEVNULL,
            )
            name = tasklist_out.split(",")[0].strip('"').lower()
        except (OSError, subprocess.CalledProcessError, IndexError):
            continue
        if name in ("python.exe", "py.exe", "pythonw.exe"):
            python_pids.append(pid)
    return python_pids


def _kill_stale_port_holders(port):
    """Clears out a leftover python process still bound to `port` (e.g. one
    orphaned by an earlier Ctrl+C not fully propagating through py.exe's
    launcher chain - see serve.py's module docstring) before we try to bind
    it ourselves, so a restart can never silently fail to take over and
    leave you talking to old code."""
    my_pid = os.getpid()
    pids = [p for p in _find_python_pids_on_port(port) if p != my_pid]
    if not pids:
        return

    print(
        f"[startup] Port {port} is still held by a leftover Python process "
        f"(PID {', '.join(map(str, pids))}) - stopping it before starting."
    )
    for pid in pids:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F", "/T"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    for _ in range(20):  # up to ~5s for Windows to actually release the socket
        if not _find_python_pids_on_port(port):
            return
        time.sleep(0.25)
    print(f"[startup] Warning: port {port} may still be in use - startup might fail.")


if __name__ == "__main__":
    _kill_stale_port_holders(PORT)
    db.init_db()
    auth.ensure_bootstrap_admin()
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

    serve(app, host=HOST, port=PORT, threads=WAITRESS_THREADS)
