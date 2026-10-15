"""
Auto-start for the portal: a Windows scheduled task that starts serve.py in
the background every time you log in to Windows - no console window to keep
open, and nothing to remember after a reboot.

Manage it with the venv's python, from this folder:

    venv\\Scripts\\python autostart.py install     set up the task and start the server
    venv\\Scripts\\python autostart.py status      is it running? (and the address for phones)
    venv\\Scripts\\python autostart.py restart     restart the server, e.g. after updating the code
    venv\\Scripts\\python autostart.py stop        stop the server and pause auto-start
    venv\\Scripts\\python autostart.py uninstall   stop the server and remove the task

Server output goes to logs\\server.log instead of a console window.

How it works: the task runs `pythonw autostart.py run` at logon and again
every minute after that. `run` exits straight away if something's already
serving on PORT - so a running server is left alone (including one started
by hand with start_server.bat), and a crashed one is back within a minute.
Otherwise it starts the exact same `venv\\Scripts\\python.exe serve.py` that
start_server.bat does, just without a window. It's the same python.exe on
purpose: whatever firewall permission lets phones reach a hand-started
server then covers this one too.

It starts at logon, not at boot: starting before anyone has logged in needs
a task that runs whether you're logged in or not, and creating one of those
needs admin rights on this PC (see README's "Running unattended").
"""
import csv
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from xml.sax.saxutils import escape

from config import BASE_DIR, PORT

TASK_NAME = "Campaign Photo Portal"
RECHECK_MINUTES = 1
VENV_PYTHON = BASE_DIR / "venv" / "Scripts" / "python.exe"
VENV_PYTHONW = BASE_DIR / "venv" / "Scripts" / "pythonw.exe"
LOG_DIR = BASE_DIR / "logs"
LOG_PATH = LOG_DIR / "server.log"
OLD_LOG_PATH = LOG_DIR / "server.log.1"
MAX_LOG_BYTES = 5 * 1024 * 1024  # drive-sync can print an error every few seconds
# while offline, so a server that stays up for weeks needs its log capped.


def _is_serving():
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=2):
            return True
    except OSError:
        return False


class _Log:
    """logs/server.log, one timestamped line at a time. Once it passes
    MAX_LOG_BYTES it's moved to server.log.1 (replacing the previous one)
    and a fresh file started, so at most ~2x MAX_LOG_BYTES is ever kept."""

    def __init__(self):
        LOG_DIR.mkdir(exist_ok=True)
        self._file = open(LOG_PATH, "a", encoding="utf-8")

    def write(self, line):
        self._file.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {line.rstrip()}\n")
        self._file.flush()
        if self._file.tell() > MAX_LOG_BYTES:
            self._file.close()
            try:
                os.replace(LOG_PATH, OLD_LOG_PATH)
            except OSError:
                pass  # e.g. locked by something reading it - just keep appending
            self._file = open(LOG_PATH, "a", encoding="utf-8")


def run():
    """What the scheduled task runs, under pythonw (so no window of its own)."""
    if _is_serving():
        return 0
    log = _Log()
    log.write("[autostart] Starting serve.py in the background.")
    proc = subprocess.Popen(
        [str(VENV_PYTHON), "-u", "serve.py"],
        cwd=BASE_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        # A hidden console rather than none at all: serve.py's netstat/tasklist
        # calls inherit it, instead of each flashing up a window of their own.
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    for line in proc.stdout:
        log.write(line)
    code = proc.wait()
    log.write(f"[autostart] serve.py exited (code {code}).")
    return code


def _task_xml():
    user = escape(f"{os.environ['USERDOMAIN']}\\{os.environ['USERNAME']}")
    # The re-check is a separate time trigger rather than a repeat on the
    # logon one, since a logon trigger's repeats only begin at the next
    # logon - this way it's already watching from the moment of install.
    # Priority 5 = normal: Task Scheduler's default (7) runs the task - and so
    # the server - at below-normal CPU and low disk priority.
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Starts the Campaign Photo Portal (serve.py) in the background at logon, and brings it back within {RECHECK_MINUTES} minute(s) if it stops. Managed by {escape(str(BASE_DIR / "autostart.py"))}.</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
    </LogonTrigger>
    <TimeTrigger>
      <Enabled>true</Enabled>
      <StartBoundary>{datetime.now():%Y-%m-%dT%H:%M:%S}</StartBoundary>
      <Repetition>
        <Interval>PT{RECHECK_MINUTES}M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>5</Priority>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(VENV_PYTHONW))}</Command>
      <Arguments>"{escape(str(BASE_DIR / "autostart.py"))}" run</Arguments>
      <WorkingDirectory>{escape(str(BASE_DIR))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks(*args, check=True):
    result = subprocess.run(["schtasks", *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if check and result.returncode != 0:
        sys.exit(f"schtasks {args[0]} failed: {(result.stderr or result.stdout).strip()}")
    return result


def _task_status():
    """'Ready' (on, not running the server itself right now), 'Running'
    (running the server), 'Disabled' (paused by `stop`), or None if the task
    isn't set up."""
    result = _schtasks("/Query", "/TN", TASK_NAME, "/FO", "CSV", "/NH", check=False)
    if result.returncode != 0:
        return None
    row = next(csv.reader(result.stdout.strip().splitlines()))
    return row[-1]


def _stop_server():
    """Stops whatever python process is serving on PORT - background or hand-
    started - and waits for the port to free up. Returns whether it did."""
    from serve import _find_python_pids_on_port  # pulls in the whole app - fine for a one-off command

    for pid in _find_python_pids_on_port(PORT):
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F", "/T"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    for _ in range(40):
        if not _is_serving():
            break
        time.sleep(0.25)
    else:
        return False
    for _ in range(40):  # let the task's `run` notice and exit, or /Run is ignored (IgnoreNew)
        if _task_status() != "Running":
            break
        time.sleep(0.25)
    return True


def _start():
    _schtasks("/Run", "/TN", TASK_NAME)
    for _ in range(60):  # serve.py takes a few seconds to import everything and bind
        if _is_serving():
            print("Server started in the background.")
            _print_phone_address()
            return
        time.sleep(0.5)
    print(f"Started, but the server isn't answering yet - check {LOG_PATH}")


def _print_phone_address():
    from serve import _detect_phone_address

    ip = _detect_phone_address()
    if ip:
        print(f"Phones should browse to: http://{ip}:{PORT}")


def _require_task():
    if _task_status() is None:
        sys.exit("Auto-start isn't set up yet - run: venv\\Scripts\\python autostart.py install")


def install():
    if not VENV_PYTHONW.exists():
        sys.exit(f"{VENV_PYTHONW} not found - set up the venv first (see README).")
    with tempfile.NamedTemporaryFile("w", encoding="utf-16", suffix=".xml", delete=False) as xml_file:
        xml_file.write(_task_xml())
    try:
        _schtasks("/Create", "/TN", TASK_NAME, "/XML", xml_file.name, "/F")
    finally:
        os.unlink(xml_file.name)
    print(f'Scheduled task "{TASK_NAME}" is set up - the portal now starts by itself when you log in to Windows.')
    if _is_serving():
        print(
            "A server is already running (e.g. from start_server.bat), so it's been left alone.\n"
            "The background one takes over within a minute of that one closing - or right now with:\n"
            "    venv\\Scripts\\python autostart.py restart"
        )
    else:
        _start()


def status():
    state = _task_status()
    for _ in range(12):  # a per-minute check also shows as Running for the second it takes
        if state != "Running":
            break
        time.sleep(0.25)
        state = _task_status()
    print("Auto-start: " + {
        None: "not set up (run: venv\\Scripts\\python autostart.py install)",
        "Running": "on - running the server in the background",
        "Ready": "on",
        "Disabled": "paused by `stop` (run: venv\\Scripts\\python autostart.py restart)",
    }.get(state, state))
    if _is_serving():
        print(f"Server: running on port {PORT}" + ("" if state == "Running" else " (started by hand, not by auto-start)"))
        _print_phone_address()
    else:
        print("Server: not running")
    print(f"Log: {LOG_PATH}")


def restart():
    _require_task()
    _schtasks("/Change", "/TN", TASK_NAME, "/ENABLE")
    if not _stop_server():
        sys.exit(f"Couldn't stop whatever is using port {PORT} - is it something other than the portal?")
    _start()


def stop():
    if _task_status() not in (None, "Disabled"):
        _schtasks("/Change", "/TN", TASK_NAME, "/DISABLE")
    if not _stop_server():
        sys.exit(f"Couldn't stop whatever is using port {PORT} - is it something other than the portal?")
    print("Server stopped, and auto-start paused until: venv\\Scripts\\python autostart.py restart")


def uninstall():
    if _task_status() is not None:
        _schtasks("/Delete", "/TN", TASK_NAME, "/F")
    _stop_server()
    print(f'Server stopped and scheduled task "{TASK_NAME}" removed.')


COMMANDS = {"run": run, "install": install, "status": status, "restart": restart, "stop": stop, "uninstall": uninstall}

if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else None
    if command not in COMMANDS:
        sys.exit(__doc__)
    sys.exit(COMMANDS[command]())
