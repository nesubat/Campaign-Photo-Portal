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
| `kits.py` | New Store Kit rules: item numbers, pack lines, the "missing numbers" report |
| `labels.py` | Pack labels for the Zebra printer (ZPL) + `py labels.py --list-printers` / `--test-print` |
| `templates/`, `static/` | The web pages staff and supervisors see (`static/kit.js` = the New Store Kit page) |
| `apps-script/DriveUploader.gs` | Deploy this separately to Google Apps Script |
| `data/employees.json` | Preset name dropdown - edit with real staff names |
| `.env.example` | Copy to `.env` once Drive is set up (never committed - see .gitignore) |

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

## 3a. Uploading 50-100 photos at once

Pick as many photos as you like. Every page (Packing, Dispatch, New Store Kits) sends two
at a time and keeps the rest waiting in order, so:

- **Keep the page open (and the screen on) until the status line says it's done.** Over a
  busy hotspot 100 photos take roughly 15-20 minutes; on a good Wi-Fi a few minutes.
- **"Connection problem - N photo(s) waiting, nothing is lost"** means the Wi-Fi dropped or
  the portal PC restarted. Do nothing: it tries again by itself (after 2 s, 5 s, 10 s ... then
  every minute) and carries on where it stopped. **Try now** skips the wait.
- A photo with **⚠ and Retry** was refused by the portal (the tile shows which photo). Tap
  **Retry**, or **×** to leave it out. Submit waits until nothing is uploading or failed.
- **"You've been logged out"**: log in again in a new tab, come back, tap **Try again**.
- Closing the page while photos are waiting asks first. If the page does get closed, photos
  picked from the phone's library can be picked again, but a **Take Photo** shot that hadn't
  uploaded yet is gone - so let a batch finish before closing the browser.

Once a photo shows as a thumbnail it is saved on this PC, and it reaches Google Drive in the
background after Submit, however long Drive takes. See CODE_GUIDE.md section 8 for the load
tests behind these numbers.

## 4. Connect it to Google Drive (optional but recommended)

Local saving works immediately with zero setup - Drive sync is additive.
To turn it on:

1. Open `apps-script/DriveUploader.gs` and follow the deploy steps in its
   header comment (uses **your** company Google account - script.google.com,
   no Cloud Console access needed).
2. Copy `.env.example` to `.env` and fill in `DRIVE_WEBAPP_URL` (the Web App
   URL) and `DRIVE_SHARED_SECRET` (the secret you chose). `.env` is
   gitignored - it holds a real secret, so it must never be committed.
3. Restart `serve.py`. You'll see `Drive sync configured` at startup.

Every photo already on disk that hasn't synced yet will pick up
automatically - you don't need to re-upload anything.

## 5. Where everything ends up

Every session starts by picking a **Photo Type** - Packing Photos, Dispatch
Photos or New Store Kits (edit `CATEGORIES` in `config.py` to add more) - which
keeps the kinds of photos separated everywhere downstream:

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

**New Store Kits** work a little differently (see the next section): locally they
live in `uploads/<JobNumber>/new_store_kits/`, but in Drive the photos go into the
job's normal **`<JobNumber>/Packing Photos/`** folder (each file's description says
which kit and pack it belongs to), and each kit gets its own report sheet directly
in **`<JobNumber>/`**: `<JobNumber> - <Kit name> - Pack Log` (tab *Packs*: one row
per pack with its item numbers, first/last edit, collaborators and photo links, plus
a "No pack" row for kit photos; tab *Summary*: who submitted it and what's missing).
Next to it, `<JobNumber> - <Kit name> - Labels.pdf` holds the kit's pack labels (one
page per pack, exactly as they print). Both are only written after **Final Submit**;
when a reopened kit is submitted again the sheet is rewritten and a new labels PDF
replaces the old one (the old PDF goes to Drive's trash).

## 5a. New Store Kits

On the kit page the ✏️ next to the job number edits both the **job number** and the
**kit name**. Changing the job number moves the kit (and its photos) for everyone on it;
if the kit was submitted before, its Drive sheet, labels and photos move to the new job's
folders at the next Final Submit (their links stay the same).

1. Start page: type the Job Number, pick **New Store Kits**, type the kit name
   (e.g. "Store 12"). Anyone who starts the same job + kit name joins the same kit -
   it shows up for everyone under Active Sessions. A kit that was already submitted is
   **reopened** instead (photos already sent to Drive can't be deleted).
2. On the kit page each person edits one **pack** at a time: type the item number
   (`J456781`) - the cursor jumps to the index field (number keypad) - then add index
   after index (`02`, `05`, ...). The pack shows `J456781-02, 05, 06, 08 & 09`.
   **Save Pack**, then **+ New Pack** for the next box (the number it will get is shown under
   the button). Photos that belong to no pack go in the **Kit photos** section, shown
   whenever you're not editing a pack.
3. **Packed so far**, at the top of the page right under the bar, shows per series the last
   number and what's missing, and who is working on which pack right now.
4. When nobody is still editing a pack (and no photo is still uploading on your phone),
   **Final Submit** appears; answering **Yes** to "Is this packed completely?" sends it to Drive.

### Pack labels (Zebra ZD420d on this machine)

Print buttons are on every pack card, and next to Save Pack (tick packs, or **All**,
then **Print (N)**). Until a printer is set up, Print opens a **preview** of the labels
at their real size instead. To set up the printer, on this machine:

1. `py labels.py --list-printers` - copy the Zebra's exact name.
2. In `.env`: `LABEL_PRINTER_NAME=<that name>`, and the label stock size as it
   feeds through the printer (`LABEL_WIDTH_MM` = across the print head, at most 104 mm,
   `LABEL_HEIGHT_MM`, `LABEL_DPI`, `LABEL_MARGIN_MM`) - the defaults are the standard
   100 x 150 mm courier label at 203 dpi. Labels print **landscape**, long side across
   (`LABEL_ORIENTATION=landscape`; `landscape-flipped` if they come out upside down,
   or `portrait`), every line centred, in **Arial Black** (`LABEL_FONT_FILE`, default
   `C:\Windows\Fonts\ariblk.ttf`). A pack always gets exactly one label - the text
   shrinks to fit.
3. `py labels.py --sample-png sample.png` shows the sample label on screen (no printing),
   then `py labels.py --test-print` prints it.
4. Restart `serve.py` - its startup lines say which printer (and size) Print will use.

**After updating the code:** `apps-script/DriveUploader.gs` must be redeployed
(Deploy > Manage deployments > edit > **New version**) for the kit Pack Log sheets and
labels PDFs - until then kit photos still reach Drive, but their sheets wait (and an older
deployment writes the sheet without the PDF; the console says so).

**Drive errors in the console** (`[drive-sync] ...` / `[kit-sheet] ... failed: ...`) now say
which side of Google answered - `script.google.com (doPost)` or the
`script.googleusercontent.com` result page - how long it took, and what Google's error page
said. Photo uploads and kit sheets get one quick retry a few seconds later before falling back
to the usual retry schedule. For the full story of a failed run, open the script at
script.google.com > **Executions**.

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
