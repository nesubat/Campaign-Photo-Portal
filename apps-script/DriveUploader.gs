/***********************************************************************
 *  CAMPAIGN PHOTO PORTAL - Drive relay
 *
 *  This is a HEADLESS endpoint only - no HTML page, no UI. It exists
 *  purely so the Windows portal can hand it one photo at a time and get
 *  it saved into "<JobNumber>/" inside the Drive folder identified by
 *  PARENT_FOLDER_ID below. Keeping it UI-free avoids the slow HtmlService
 *  page-load overhead - a plain doPost() responds in about a second once warm.
 *
 *  The portal already keeps its own local copy of every photo and only
 *  treats this as a background sync, so if this endpoint is briefly down
 *  nothing is lost - it just retries automatically.
 *
 *  DEPLOY (one-time):
 *    1. sheets.new isn't needed - go to script.google.com > New project
 *       (use your COMPANY Google account, since that account will own
 *       the Drive folders and storage).
 *    2. Delete the stub code, paste this whole file, save.
 *    3. Set this project's Script Properties (Project Settings > gear icon >
 *       Script Properties > Add property): SHARED_SECRET (a long random
 *       string - treat it like a password, it's the only thing stopping a
 *       stranger who finds the URL from writing junk files into your Drive)
 *       and TARGET_FOLDER_ID (the Drive folder that should hold every
 *       <JobNumber> folder - the long id in that folder's URL:
 *       drive.google.com/drive/folders/<ID>). The account running this
 *       script needs edit access to that folder.
 *    4. Deploy > New deployment > type: Web app.
 *         Execute as:      Me
 *         Who has access:  Anyone
 *       (svc runs as you regardless of who calls it - the "Anyone" only
 *       controls who can reach the URL at all; SHARED_SECRET is the
 *       actual gate.)
 *    5. Copy the Web App URL it gives you.
 *    6. On the Windows machine, copy .env.example -> .env and paste that URL
 *       into DRIVE_WEBAPP_URL, and your SHARED_SECRET value into
 *       DRIVE_SHARED_SECRET. Restart the portal (py serve.py).
 *    7. Test: upload one photo from the portal and check this Drive
 *       account for a new folder matching the job number.
 *
 *  If you ever edit this script after deploying, use
 *  Deploy > Manage deployments > pencil icon > New version - editing
 *  the code alone does NOT update a live Web App URL.
 ***********************************************************************/

const SHARED_SECRET = PropertiesService.getScriptProperties().getProperty('SHARED_SECRET');      // must match .env's DRIVE_SHARED_SECRET
const PARENT_FOLDER_ID = PropertiesService.getScriptProperties().getProperty('TARGET_FOLDER_ID'); // Drive folder that holds every <JobNumber> folder

// Photo Links holds several stacked Drive URLs per cell (WRAP-formatted, one
// per line - see getOrCreateLogSheet_) - a full Drive file link has no
// natural break point, so auto-fit-to-data fights the wrap and balloons row
// height instead of column width. Fixed width instead, picked by hand in the
// Sheets UI (right-click the column header - Resize column - reads/sets the
// exact px value) rather than auto-fit.
const PHOTO_LINKS_COLUMN_WIDTH = 514;

function doPost(e) {
  try {
    const body = JSON.parse(e.postData.contents);

    if (body.secret !== SHARED_SECRET) {
      return jsonOut({ ok: false, error: 'bad secret' });
    }

    if (body.action === 'uploadBatch') return uploadBatch_(body);
    if (body.action === 'logConsignments') return logConsignments_(body);
    if (body.action === 'checkJob') return checkJob_(body);
    if (body.action === 'resizeSheetColumns') return resizeSheetColumns_(body);

    return jsonOut({ ok: false, error: 'unknown or missing action' });
  } catch (err) {
    return jsonOut({ ok: false, error: String(err) });
  }
}

/* Uploads every file in body.files (all for the same jobNumber/category) in
   one execution. Each file is tried independently - one bad file (missing
   data, a decode error, etc.) doesn't stop the rest, and its own failure is
   reported back per-file rather than failing the whole batch. Idempotent by
   filename: if a file with this exact name already exists (e.g. a retry
   after the portal never got the response for an earlier attempt that
   actually succeeded), it's reused rather than duplicated. Filenames are
   globally unique per photo (employee-timestamp, to the microsecond - see
   now_filename_stamp() in the portal's db.py), so a name match is always
   the same photo, never a false positive - PROVIDED the exists-check and the
   create happen as one atomic step, which is why they're wrapped in a lock
   below: without it, two overlapping calls for the same fileName (a second
   server instance, or the portal's own retry racing an earlier attempt that
   timed out on the portal's side but kept running here) could both pass the
   "doesn't exist yet" check before either finished creating it, producing
   two files with the same name - this actually happened once in practice. */
function uploadBatch_(body) {
  if (!body.jobNumber || !body.category || !Array.isArray(body.files) || !body.files.length) {
    return jsonOut({ ok: false, error: 'missing jobNumber/category/files' });
  }

  const folder = getOrCreateJobFolder_(body.jobNumber, body.category);

  const results = body.files.map(function (f) {
    try {
      if (!f.fileName || !f.contentB64) {
        return { fileName: f.fileName || null, ok: false, error: 'missing fileName/contentB64' };
      }

      const bytes = Utilities.base64Decode(f.contentB64);
      const mime = guessMime_(f.fileName);
      const blob = Utilities.newBlob(bytes, mime, f.fileName);

      // Exists-check + create locked as one atomic step - see the note above
      // the function. Kept tight (just this, not the decode above or
      // setDescription below) so one slow file can't stall unrelated
      // concurrent uploads any longer than necessary.
      const lock = LockService.getScriptLock();
      lock.waitLock(15000);
      let file;
      let created = false;
      try {
        const existing = folder.getFilesByName(f.fileName);
        if (existing.hasNext()) {
          file = existing.next();
        } else {
          file = folder.createFile(blob);
          created = true;
        }
      } finally {
        lock.releaseLock();
      }
      if (created) {
        file.setDescription(
          'Uploaded by ' + (f.employeeName || 'unknown') +
          ' at ' + (f.uploadedAt || new Date().toISOString())
        );
      }
      return { fileName: f.fileName, ok: true, fileId: file.getId(), url: file.getUrl() };
    } catch (err) {
      // Logged (not just returned in the JSON) so a per-file failure shows
      // up in this execution's own entry in the Apps Script Executions log -
      // otherwise a failure is only visible indirectly, as the portal
      // quietly backing that one file off and retrying it later.
      Logger.log('uploadBatch_ failed for ' + (f.fileName || '(missing fileName)') + ': ' + err);
      return { fileName: f.fileName || null, ok: false, error: String(err) };
    }
  });

  return jsonOut({ ok: true, results: results });
}

/* Finds "<jobNumber>/<category>" inside PARENT_FOLDER_ID, creating either
   level as needed. Locked so two near-simultaneous uploads for a brand-new
   job number/category can never create duplicate folders. */
function getOrCreateJobFolder_(jobNumber, category) {
  const lock = LockService.getScriptLock();
  lock.waitLock(15000);
  try {
    const parent = DriveApp.getFolderById(PARENT_FOLDER_ID);
    const jobFolder = getOrCreateChild_(parent, String(jobNumber));
    return getOrCreateChild_(jobFolder, String(category));
  } finally {
    lock.releaseLock();
  }
}

function getOrCreateChild_(parent, name) {
  const it = parent.getFoldersByName(name);
  if (it.hasNext()) return it.next();
  return parent.createFolder(name);
}

/* Writes/updates one row PER CONSIGNMENT in body.consignments (all for the
   same jobNumber) in "<jobNumber>/Packing Photos/<jobNumber> - Photo Log" -
   one execution covers every consignment in the batch, not just one, so a
   job with many small consignments (e.g. 100 x 1 photo each) doesn't need
   100 Sheet-update calls. Only reached when a session opted into "keep
   logs" - see keep_logs in the portal's sessions table. Each consignment is
   tried independently and its own result reported, so one bad entry can't
   block the rest. Item IDs, contributors, and photo links are MERGED into
   whatever's already in that row (union, deduped) rather than overwritten -
   so a retry after a partial failure can't duplicate anything, and the
   sheet stays correct even if the caller's own local data is incomplete
   (e.g. cleaned up locally and only partially rehydrated - see checkJob_).
   Locked so two near-simultaneous calls for the same job can't both append
   a fresh row for the same value. */
function logConsignments_(body) {
  if (!body.jobNumber || !Array.isArray(body.consignments) || !body.consignments.length) {
    return jsonOut({ ok: false, error: 'missing jobNumber/consignments' });
  }

  // getOrCreateJobFolder_ takes its own lock internally, so it stays outside
  // this one to avoid nesting two waitLock() calls in the same execution.
  const folder = getOrCreateJobFolder_(body.jobNumber, 'Packing Photos');

  const lock = LockService.getScriptLock();
  lock.waitLock(15000);
  try {
    const sheet = getOrCreateLogSheet_(folder, body.jobNumber);
    const now = new Date(); // fallback only - see firstLogged/lastUpdated below
    // Read once, then keep this in-memory copy in sync as rows are
    // appended/updated below, instead of re-reading the whole sheet for
    // every consignment in the batch.
    let data = sheet.getDataRange().getValues();

    const results = body.consignments.map(function (c) {
      try {
        if (!c.keyType || !c.keyValue) {
          return { keyValue: c.keyValue || null, ok: false, error: 'missing keyType/keyValue' };
        }

        // Use the portal's own locally-recorded scan/edit timestamps, not
        // this execution's clock - Sheet updates are batched and can run
        // minutes after the actual scan (see _log_consignment_batch in the
        // portal's drive_sync.py), so `now` here would misrepresent when the
        // consignment was actually scanned or touched. Falls back to `now`
        // only if an older portal version ever sends a batch without them.
        const firstLogged = c.firstLogged ? new Date(c.firstLogged) : now;
        const lastUpdated = c.lastUpdated ? new Date(c.lastUpdated) : now;

        let rowIndex = -1; // 1-indexed sheet row of an existing match, if any
        for (let i = 1; i < data.length; i++) {
          if (String(data[i][2]).trim().toLowerCase() === String(c.keyValue).trim().toLowerCase()) {
            rowIndex = i + 1;
            break;
          }
        }

        const itemIds = c.itemIds || [];
        const contributors = c.contributors || [];
        const photoLinks = c.photoLinks || [];

        if (rowIndex === -1) {
          // Item IDs and Photo Links are newline-separated (one per line in
          // the cell - see getOrCreateLogSheet_'s wrap formatting);
          // Contributors stays a short comma list, it's just names.
          const newRow = [firstLogged, lastUpdated, c.keyValue, itemIds.join('\n'), contributors.join(', '), photoLinks.join('\n')];
          sheet.appendRow(newRow);
          data.push(newRow);
        } else {
          const existingRow = data[rowIndex - 1];
          const mergedItemIds = mergeLists_(existingRow[3], '\n', itemIds).join('\n');
          const mergedContributors = mergeLists_(existingRow[4], ',', contributors).join(', ');
          const mergedLinks = mergeLists_(existingRow[5], '\n', photoLinks).join('\n');
          // Columns B-F only - column A (First Logged) is left as originally set.
          sheet.getRange(rowIndex, 2, 1, 5).setValues(
            [[lastUpdated, c.keyValue, mergedItemIds, mergedContributors, mergedLinks]]
          );
          data[rowIndex - 1] = [existingRow[0], lastUpdated, c.keyValue, mergedItemIds, mergedContributors, mergedLinks];
        }
        return { keyValue: c.keyValue, ok: true };
      } catch (err) {
        Logger.log('logConsignments_ failed for ' + (c.keyValue || '(missing keyValue)') + ': ' + err);
        return { keyValue: c.keyValue || null, ok: false, error: String(err) };
      }
    });

    return jsonOut({ ok: true, results: results });
  } finally {
    lock.releaseLock();
  }
}

/* Case-insensitively unions an existing delimited cell value with a new list,
   preserving first-seen order and dropping duplicates either side already had. */
function mergeLists_(existingCellValue, sep, incomingList) {
  const existing = splitTrimmed_(existingCellValue, sep);
  const seen = {};
  const result = [];
  existing.concat(incomingList || []).forEach(function (v) {
    const key = v.toLowerCase();
    if (!seen[key]) {
      seen[key] = true;
      result.push(v);
    }
  });
  return result;
}

function splitTrimmed_(value, sep) {
  return String(value || '').split(sep).map(function (s) { return s.trim(); }).filter(String);
}

/* Read-only: tells the portal whether a job it doesn't know about locally
   (its data was cleaned up, or this is a fresh local install) already has a
   Photo Log sheet in Drive - and if so, hands back every consignment row so
   the portal can restore just enough locally to resume it correctly. */
function checkJob_(body) {
  if (!body.jobNumber) {
    return jsonOut({ ok: false, error: 'missing jobNumber' });
  }

  const sheet = findLogSheet_(body.jobNumber);
  if (!sheet) return jsonOut({ ok: true, found: false });

  const data = sheet.getDataRange().getValues();
  const rows = [];
  for (let i = 1; i < data.length; i++) {
    const r = data[i];
    if (!r[2]) continue; // skip any blank row
    rows.push({
      firstLogged: r[0] ? new Date(r[0]).toISOString() : null,
      lastUpdated: r[1] ? new Date(r[1]).toISOString() : null,
      keyValue: String(r[2]),
      itemIds: splitTrimmed_(r[3], '\n'),
      contributors: splitTrimmed_(r[4], ','),
      photoLinks: splitTrimmed_(r[5], '\n'),
    });
  }
  return jsonOut({ ok: true, found: true, rows: rows });
}

/* Read-only lookup of an EXISTING "<jobNumber> - Photo Log" sheet under
   "<jobNumber>/Packing Photos" - never creates anything (unlike
   getOrCreateLogSheet_), so callers that only want to inspect or format a
   sheet that may not exist yet just get null back instead of a fresh
   spreadsheet. */
function findLogSheet_(jobNumber) {
  const parent = DriveApp.getFolderById(PARENT_FOLDER_ID);
  const jobIt = parent.getFoldersByName(String(jobNumber));
  if (!jobIt.hasNext()) return null;

  const packingIt = jobIt.next().getFoldersByName('Packing Photos');
  if (!packingIt.hasNext()) return null;

  const sheetIt = packingIt.next().getFilesByName(jobNumber + ' - Photo Log');
  if (!sheetIt.hasNext()) return null;

  return SpreadsheetApp.open(sheetIt.next()).getSheets()[0];
}

/* Called once per job, after the portal has confirmed every photo and
   consignment for it is fully synced (see job_fully_synced in the portal's
   db.py) - auto-fits the plain single-line columns (First Logged, Last
   Updated, Consignment/Store, Item ID(s), Contributors) to their content.
   Photo Links is deliberately left out of the auto-fit and instead pinned to
   PHOTO_LINKS_COLUMN_WIDTH - seeing getOrCreateLogSheet_'s note, auto-fitting
   a wrapped column of un-breakable URLs balloons row height instead of
   column width. No lock needed - this only reformats already-written
   content, it doesn't race with anything else writing new rows. */
function resizeSheetColumns_(body) {
  if (!body.jobNumber) {
    return jsonOut({ ok: false, error: 'missing jobNumber' });
  }

  const sheet = findLogSheet_(body.jobNumber);
  if (!sheet) return jsonOut({ ok: true, found: false });

  sheet.autoResizeColumns(1, 5); // First Logged, Last Updated, Consignment/Store, Item ID(s), Contributors
  sheet.setColumnWidth(6, PHOTO_LINKS_COLUMN_WIDTH); // Photo Links - fixed, not auto-fit

  return jsonOut({ ok: true, found: true });
}

/* Finds "<jobNumber> - Photo Log" inside the given folder, creating it (with
   a header row) if it doesn't exist yet. SpreadsheetApp.create() always drops
   a new file in Drive's root, so it's moved into the target folder after. */
function getOrCreateLogSheet_(folder, jobNumber) {
  const name = jobNumber + ' - Photo Log';
  let sheet;

  const existing = folder.getFilesByName(name);
  if (existing.hasNext()) {
    sheet = SpreadsheetApp.open(existing.next()).getSheets()[0];
  } else {
    const ss = SpreadsheetApp.create(name);
    const file = DriveApp.getFileById(ss.getId());
    folder.addFile(file);
    DriveApp.getRootFolder().removeFile(file);

    sheet = ss.getSheets()[0];
    sheet.appendRow(
      ['First Logged', 'Last Updated', 'Consignment / Store', 'Item ID(s)', 'Contributors', 'Photo Links']
    );
    sheet.setFrozenRows(1);
    sheet.setColumnWidth(4, 180); // Item ID(s) - wide enough for a short multi-line list
    sheet.setColumnWidth(6, PHOTO_LINKS_COLUMN_WIDTH); // Photo Links
  }

  // Item ID(s) and Photo Links hold one value per line (see logConsignment_) -
  // wrap so each line actually shows as its own row in the cell instead of
  // overflowing or hiding. Applied every call (cheap, idempotent) so it's
  // correct even for a sheet that already existed before this was added.
  sheet.getRange('D2:D').setWrapStrategy(SpreadsheetApp.WrapStrategy.WRAP);
  sheet.getRange('F2:F').setWrapStrategy(SpreadsheetApp.WrapStrategy.WRAP);

  return sheet;
}

function guessMime_(fileName) {
  const ext = fileName.split('.').pop().toLowerCase();
  const map = {
    jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png',
    webp: 'image/webp', heic: 'image/heic', heif: 'image/heif'
  };
  return map[ext] || 'application/octet-stream';
}

function jsonOut(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

/* Optional: quick manual sanity check from the Apps Script editor
   (Run > testGetOrCreate). Creates/looks up a "TEST-JOB" folder. */
function testGetOrCreate() {
  const f = getOrCreateJobFolder_('TEST-JOB', 'Packing Photos');
  Logger.log(f.getUrl());
}

function authTestDeleteMe() {
  const ss = SpreadsheetApp.create('auth-test-delete-me');
  Logger.log(ss.getUrl());
  DriveApp.getFileById(ss.getId()).setTrashed(true);
}

/* Manual diagnostic - paste the folder ID below, then Run > checkDuplicateFilenames
   from the Apps Script editor (View > Logs, or Ctrl+Enter, to see the output).
   No redeploy needed - this isn't reachable via doPost, it only runs when you
   trigger it yourself. Read-only: lists every filename that appears more than
   once in the folder, with each copy's file ID, created date, and view link,
   sorted oldest-first so you can tell which copy was created first. Never
   deletes or modifies anything. */
function checkDuplicateFilenames() {
  const folderId = 'PASTE_FOLDER_ID_HERE'; // the target folder's ID from its Drive URL

  const folder = DriveApp.getFolderById(folderId);
  const byName = {};
  let total = 0;

  const files = folder.getFiles();
  while (files.hasNext()) {
    const file = files.next();
    total++;
    const name = file.getName();
    if (!byName[name]) byName[name] = [];
    byName[name].push({
      id: file.getId(),
      created: file.getDateCreated(),
      url: file.getUrl(),
    });
  }

  const names = Object.keys(byName);
  const duplicateNames = names.filter(function (name) { return byName[name].length > 1; });
  let extraCopies = 0;
  duplicateNames.forEach(function (name) { extraCopies += byName[name].length - 1; });

  Logger.log('Total files in folder: ' + total);
  Logger.log('Unique filenames: ' + names.length);
  Logger.log('Filenames with more than one copy: ' + duplicateNames.length);
  Logger.log('Total extra/duplicate copies (beyond one per name): ' + extraCopies);
  Logger.log('---');

  if (duplicateNames.length === 0) {
    Logger.log('No duplicate filenames found.');
    return;
  }

  duplicateNames.forEach(function (name) {
    const copies = byName[name].slice().sort(function (a, b) { return a.created - b.created; });
    Logger.log(name + ' - ' + copies.length + ' copies:');
    copies.forEach(function (c, i) {
      const label = i === 0 ? 'earliest' : 'later #' + i;
      Logger.log('  [' + label + '] id=' + c.id + ' created=' + c.created.toISOString() + ' ' + c.url);
    });
  });
}
