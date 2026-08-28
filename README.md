# Campaign Photo Portal

A phone-friendly upload portal for campaign photos. Staff type a Job Number,
pick their name, then take or attach photos - each one uploads the instant
it's picked and shows up in a live gallery. Submit finalizes the batch.
Photos are saved locally on this machine immediately (fast, reliable) and
mirrored to a company Google Drive folder in the background.

## What's here

| File | Purpose |
|---|---|
| `app.py` | Flask app: routes, upload handling, thumbnailing |
| `serve.py` | **Use this to run it day-to-day** (production WSGI server) |
| `db.py` | SQLite schema + helpers (jobs, sessions, uploads) |
| `drive_sync.py` | Background thread that pushes photos to Google Drive |
| `config.py` | All the tunable settings in one place |
| `templates/`, `static/` | The web pages staff and supervisors see |
| `apps-script/DriveUploader.gs` | Deploy this separately to Google Apps Script |
| `data/employees.json` | Preset name dropdown - edit with real staff names |
| `data/drive_config.json.example` | Copy to `drive_config.json` once Drive is set up |

## 1. First-time setup

```powershell
cd "C:\Users\sbasnet\Campaign Photo Portal"
py -m pip install -r requirements.txt
```

Edit `data/employees.json` with your real staff names (the "Other" option on
the form always lets someone type a name that isn't listed yet).

## 2. Run it

```powershell
py serve.py
```

You'll see something like:
```
[startup] Serving on http://0.0.0.0:5000 (waitress)
```

Leave that window open (or run it as a background/scheduled task - see
"Running unattended" below).

## 3. Every session: turn on the hotspot, then connect

1. On this Windows machine: **Settings > Network & Internet > Mobile hotspot**
   -> "Share my Internet connection from: Ethernet" -> turn the toggle on.
2. Note the network name/password shown.
3. On each phone: join that WiFi network.
4. On each phone's browser, go to: `http://192.168.137.1:5000`
   (Windows always assigns itself `192.168.137.1` on the hotspot adapter.)

No VLAN/ACL issues this way - the hotspot is its own private network with
this PC as the only "router" on it.

## 4. Connect it to Google Drive (optional but recommended)

Local saving works immediately with zero setup - Drive sync is additive.
To turn it on:

1. Open `apps-script/DriveUploader.gs` and follow the deploy steps in its
   header comment (uses **your** company Google account - script.google.com,
   no Cloud Console access needed).
2. Copy `data/drive_config.json.example` to `data/drive_config.json` and
   fill in the Web App URL and the shared secret you chose.
3. Restart `serve.py`. You'll see `Drive sync configured` at startup.

Every photo already on disk that hasn't synced yet will pick up
automatically - you don't need to re-upload anything.

## 5. Where everything ends up

Every session starts by picking a **Photo Type** - Packing Photos or Dispatch
Photos (edit `CATEGORIES` in `config.py` to add more) - which keeps the two
kinds of photos separated everywhere downstream:

- **Local copy (always)**: `uploads/<JobNumber>/<packing|dispatch>/photo.jpg`,
  thumbnails in `uploads/<JobNumber>/<packing|dispatch>/thumbs/`.
- **Drive copy (once configured)**: `<JobNumber>/<Packing Photos|Dispatch Photos>/`
  inside the Drive folder set as `PARENT_FOLDER_ID` in
  `apps-script/DriveUploader.gs`.
- **Metadata**: `data/portal.db` (SQLite) - who uploaded what, when, which
  category, and whether it's synced to Drive yet.
- **Supervisor view**: `http://<address>:5000/gallery/<JobNumber>` - read
  only, shows every photo for a job (both categories), its type, and its
  Drive sync status, no need to start a session.

Drive sync only picks up a photo once its batch has been **Submitted** -
photos sit local-only until then.

**Local cleanup**: once a photo is confirmed synced to Drive, its local
copy (full-res + thumbnail) is automatically deleted after
`LOCAL_CLEANUP_AFTER_DAYS` (default 2 days, set in `config.py`) to keep disk
usage down. The gallery keeps a "View on Drive" link for anything cleaned up
locally - nothing is ever deleted before Drive confirms it has the photo.

## 6. Running unattended

`serve.py` needs to keep running on the Windows machine. Options, roughly
in order of effort:

- **Simplest**: leave the PowerShell window open, minimized.
- **Better**: register it as a Windows service with
  [NSSM](https://nssm.cc/) so it survives reboots and stays up without
  anyone logged in:
  ```powershell
  nssm install CampaignPhotoPortal "C:\path\to\py.exe" "C:\Users\sbasnet\Campaign Photo Portal\serve.py"
  nssm set CampaignPhotoPortal AppDirectory "C:\Users\sbasnet\Campaign Photo Portal"
  nssm start CampaignPhotoPortal
  ```

## Known limits worth knowing

- **Hotspot device cap**: Windows Mobile Hotspot supports up to 8
  simultaneous connections. For more than that, you'd need a real WiFi
  access point instead - the app itself has no such limit.
- **Drive storage**: mirrors to whatever Google account owns the Apps
  Script deployment - keep an eye on that account's Drive quota over time.
- **One machine**: this is a single-instance local app (SQLite + local
  disk), matching the "one PC on the network" design - it isn't built to
  run on multiple machines behind a load balancer.
