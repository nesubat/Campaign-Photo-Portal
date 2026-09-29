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
 *  ACTIONS (body.action, all POSTed as JSON with body.secret):
 *    uploadBatch         photos -> "<Job>/<Category>/" (New Store Kit photos
 *                        arrive as category "Packing Photos", so they share
 *                        the job's packing folder; their description names
 *                        the kit and pack - see f.note in uploadBatch_)
 *    logConsignments     "keep logs" rows -> "<Job>/Packing Photos/<Job> - Photo Log"
 *    checkJob            read-only lookup of that Photo Log (resume a job)
 *    resizeSheetColumns  one-off column auto-fit of that Photo Log
 *    logKit              one New Store Kit -> "<Job>/<Job> - <Kit> - Pack Log"
 *                        (tabs "Packs" + "Summary", fully rewritten each call -
 *                        also every time a kit is reopened and re-submitted;
 *                        photos that belong to no pack get a last "No pack" row;
 *                        with body.labelsPdf also "<Job>/<Job> - <Kit> - Labels.pdf",
 *                        the previous submission's PDF moved to the trash;
 *                        with body.previousJobNumber (the kit changed job) its
 *                        Pack Log and body.moveFileIds photos are MOVED from
 *                        the old job's folders - same files, same links)
 *  logKit was added after the first deployment: until this script is
 *  redeployed as a New version (see the bottom of this comment), the live
 *  Web App answers it with 'unknown or missing action' - photos keep syncing
 *  fine through the old version, only the kit Sheets wait, and the portal's
 *  console says "[kit-sheet] Apps Script is out of date - redeploy ...".
 *  The Summary tab's "Times submitted" row came later still: a deployment
 *  from before it writes the Sheet fine, just without that row. So did
 *  renaming (previousKitName): a kit renamed in the portal after its Sheet
 *  was written gets that same Sheet renamed - an older deployment would
 *  start a second Sheet under the new name instead.
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
 *  If you ever edit this script after deploying (e.g. to pick up a new
 *  action like logKit), use Deploy > Manage deployments > pencil icon >
 *  Version: New version > Deploy - editing and saving the code alone does
 *  NOT update a live Web App URL, and a brand-new deployment would give a
 *  NEW URL that .env doesn't know about.
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
    if (body.action === 'logKit') return logKit_(body);

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
        // A retry of a photo already here reuses it - unless it's in the
        // trash, where nobody would ever see it.
        const existing = findLiveFileByName_(folder, f.fileName);
        if (existing) {
          file = existing;
        } else {
          file = folder.createFile(blob);
          created = true;
        }
      } finally {
        lock.releaseLock();
      }
      if (created) {
        // f.note is only sent for New Store Kit photos ("Store 12 · Pack 3") -
        // every kit's photos share the job's Packing Photos folder under
        // their usual employee-timestamp names, so the description is what
        // says which kit/pack a picture belongs to. Only set on creation,
        // like the rest of the description: a retry that reuses an existing
        // file leaves it exactly as it was.
        file.setDescription(
          'Uploaded by ' + (f.employeeName || 'unknown') +
          ' at ' + (f.uploadedAt || new Date().toISOString()) +
          (f.note ? ' · ' + f.note : '')
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
    const jobFolder = getOrCreateJobRootFolderUnlocked_(jobNumber);
    return getOrCreateChild_(jobFolder, String(category));
  } finally {
    lock.releaseLock();
  }
}

/* Finds (or creates) just the "<jobNumber>" folder itself inside
   PARENT_FOLDER_ID - where a New Store Kit's Pack Log sheet lives, one level
   above the Packing Photos folder its pictures go into. Locked for the same
   reason as getOrCreateJobFolder_: a kit Sheet and a first photo upload for
   a brand-new job can arrive at the same moment, and both would otherwise
   create their own "<jobNumber>" folder. */
function getOrCreateJobRootFolder_(jobNumber) {
  const lock = LockService.getScriptLock();
  lock.waitLock(15000);
  try {
    return getOrCreateJobRootFolderUnlocked_(jobNumber);
  } finally {
    lock.releaseLock();
  }
}

/* Shared by the two locked lookups above. Takes NO lock itself - each caller
   already holds the script lock, and nesting a second waitLock() inside the
   same execution is exactly what they must never do. Not for calling on its
   own from anywhere else. */
function getOrCreateJobRootFolderUnlocked_(jobNumber) {
  const parent = DriveApp.getFolderById(PARENT_FOLDER_ID);
  return getOrCreateChild_(parent, String(jobNumber));
}

/* getFoldersByName also returns folders sitting in the trash - photos or a
   Pack Log written into a trashed "<Job>" / "Packing Photos" folder would be
   invisible (and gone when the trash is emptied), so only live ones count. */
function getOrCreateChild_(parent, name) {
  return findLiveFolderByName_(parent, name) || parent.createFolder(name);
}

function findLiveFolderByName_(parent, name) {
  const it = parent.getFoldersByName(name);
  while (it.hasNext()) {
    const folder = it.next();
    if (!folder.isTrashed()) return folder;
  }
  return null;
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
          // Item IDs are NOT merged like the other two columns: a repeated
          // scan changes an entry's own label (e.g. "xxxxxx-2" ->
          // "xxxxxx-3"), and the portal always sends the complete current
          // list - union-merging old and new would leave stale labels like
          // "xxxxxx-2" sitting next to "xxxxxx-3" instead of being replaced.
          const mergedItemIds = itemIds.join('\n');
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
  const jobFolder = findLiveFolderByName_(parent, String(jobNumber));
  if (!jobFolder) return null;

  const packing = findLiveFolderByName_(jobFolder, 'Packing Photos');
  if (!packing) return null;

  const file = findLiveFileByName_(packing, jobNumber + ' - Photo Log');
  if (!file) return null;

  return SpreadsheetApp.open(file).getSheets()[0];
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

  const existing = findLiveFileByName_(folder, name);
  if (existing) {
    sheet = SpreadsheetApp.open(existing).getSheets()[0];
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

// New Store Kit Pack Log layout - see logKit_. Kept together so the header
// row and the column-by-column formatting below can't drift apart.
const KIT_PACKS_TAB = 'Packs';
const KIT_SUMMARY_TAB = 'Summary';
const KIT_PACKS_HEADERS = ['Pack', 'First Edit', 'Last Edit', 'Item Number(s)', 'Collaborators', 'Photo Links'];
// Same shape as the portal's own on-screen times (app.py's started_at
// filter: "2026/09/27 10:01:02 AM"), so the Sheet and the phone read alike.
const KIT_DATE_TIME_FORMAT = 'yyyy/mm/dd hh:mm:ss AM/PM';
// Summary's Missing column can hold a long list ("001, 002, ... 915") -
// fixed width + wrap, for the same reason as PHOTO_LINKS_COLUMN_WIDTH.
const KIT_MISSING_COLUMN_WIDTH = 420;
// Sheets refuses any single cell over 50,000 characters, and it does so by
// throwing from setValues() - the WHOLE write fails, not just that cell. The
// portal would then retry the identical payload forever and the kit would
// never get a Sheet at all. Only a pathological list gets anywhere near it
// (one mistyped index like "456789" makes Missing ~456,000 numbers long),
// so every list cell is joined up to a safe margin under the limit and the
// remainder summarized as "& N more", the same way the portal's on-screen
// report caps its text. See joinForCell_.
const KIT_CELL_MAX_CHARS = 49000;

/* Writes one New Store Kit's "<jobNumber> - <kitName> - Pack Log" sheet into
   "<jobNumber>/" - the job folder itself; the kit's photos are one level
   down, in Packing Photos, alongside the job's other packing photos. Called
   by the portal once the kit has been Final Submitted and its photos have
   reached Drive (see _log_kit in the portal's drive_sync.py) - possibly more
   than once: a partial Sheet if some photo is slow to sync, then a rewrite
   once more of them are in. And again after every re-submission: a finished
   kit can be reopened on the portal, changed (items added or removed in any
   pack, new packs, more photos) and submitted again, and each submission's
   Sheet replaces the previous one's.

   So unlike logConsignments_ (which MERGES into rows several sessions keep
   adding to), this is a full, idempotent OVERWRITE: the portal always sends
   the complete kit, every old data row is cleared and rewritten from it, and
   Summary is rebuilt from scratch - a retry or rewrite can never duplicate a
   row or leave a stale one behind (a removed item or pack simply isn't in
   the new payload). Layout:
     Packs:   Pack | First Edit | Last Edit | Item Number(s) | Collaborators |
              Photo Links - one row per pack, ascending
     Summary: Kit, Job Number, Submitted by/at (the latest submission), Times
              submitted, Packed completely = Yes, a blank row, then
              Series | Last Number | Missing (full list)

   Every text cell is formatted as plain text ('@') BEFORE its value is
   written - otherwise Sheets parses each string the way it parses typing: a
   last number "09" would become 9, an item line "1/2" a date, a kit called
   "=x" a formula. Dates go in as real Date objects with an explicit format,
   so they still sort and filter as dates. Locked as a whole (lookup AND
   writes) so the portal's retry of a slow call can't interleave its clear
   and rewrite with the original still running. */
function logKit_(body) {
  if (!body.jobNumber || !body.kitName || !Array.isArray(body.packs)) {
    return jsonOut({ ok: false, error: 'missing jobNumber/kitName/packs' });
  }

  // getOrCreateJobRootFolder_ takes its own lock internally, so it stays
  // outside this one to avoid nesting two waitLock() calls in one execution.
  const jobFolder = getOrCreateJobRootFolder_(body.jobNumber);

  const lock = LockService.getScriptLock();
  lock.waitLock(15000);
  try {
    const previousJob = body.previousJobNumber ? String(body.previousJobNumber) : '';
    const ss = getOrCreateKitSpreadsheet_(
      jobFolder, String(body.jobNumber), String(body.kitName),
      body.previousKitName ? String(body.previousKitName) : '', previousJob
    );
    writeKitPacksTab_(ss, body.packs, body.generalPhotos);
    writeKitSummaryTab_(ss, body);
    // Sheet writes are buffered and only applied at the end of the
    // execution unless flushed - and the lock is released BEFORE the end (in
    // the finally below). Flushing here commits the clear + rewrite while
    // this call still holds the lock, which is what actually stops an
    // overlapping retry from interleaving with it (LockService's own advice
    // for spreadsheets).
    SpreadsheetApp.flush();
    // The labels PDF comes second: if it fails, the whole call answers
    // ok:false and the portal sends everything again - the Sheet rewrite is
    // idempotent, and so is the PDF swap below.
    const result = { ok: true };
    if (previousJob && previousJob !== String(body.jobNumber)) {
      result.movedPhotos = moveKitPhotos_(jobFolder, body.photoFolder || 'Packing Photos', body.moveFileIds);
      result.jobMoved = true;
    }
    if (body.labelsPdf) result.labelsFileId = replaceKitLabelsPdf_(jobFolder, body);
    return jsonOut(result);
  } finally {
    lock.releaseLock();
  }
}

/* body.labelsPdf = {fileName, contentB64 (null = no packs to label),
   packCount, submitCount, previousFileId}. Saves the new PDF in the job
   folder FIRST, then moves the kit's old labels PDF(s) to the trash - the
   one the portal last recorded (by id, so it's found after a rename too) and
   any other live file of the new name (a copy made by a call whose answer
   never reached the portal). Only PDFs are ever trashed. Returns the new
   file's id, or '' when there was nothing to label. */
function replaceKitLabelsPdf_(jobFolder, body) {
  const lp = body.labelsPdf;
  const name = String(lp.fileName || (body.jobNumber + ' - ' + body.kitName + ' - Labels.pdf'));
  const old = [];
  const keep = function (f) {
    if (f && !f.isTrashed() && f.getMimeType() === 'application/pdf' &&
        old.every(function (o) { return o.getId() !== f.getId(); })) old.push(f);
  };
  if (lp.previousFileId) {
    try { keep(DriveApp.getFileById(String(lp.previousFileId))); } catch (e) { /* already gone */ }
  }
  const same = jobFolder.getFilesByName(name);
  while (same.hasNext()) keep(same.next());

  let newId = '';
  if (lp.contentB64) {
    const blob = Utilities.newBlob(Utilities.base64Decode(String(lp.contentB64)), 'application/pdf', name);
    const file = jobFolder.createFile(blob);
    file.setDescription('Pack labels for ' + body.kitName + ' (' + (lp.packCount || 0) + ' pack(s)) - ' +
                        'submission ' + (lp.submitCount || 1) + ', submitted by ' + (body.finalizedBy || '?') +
                        ' at ' + (body.finalizedAt || '?'));
    newId = file.getId();
  }
  old.forEach(function (f) { if (f.getId() !== newId) f.setTrashed(true); });
  return newId;
}

/* Finds "<jobNumber> - <kitName> - Pack Log" in the job folder, or creates
   it - moved out of Drive's root the same way as getOrCreateLogSheet_,
   since SpreadsheetApp.create() always drops a new file there. The default
   first tab ("Sheet1") becomes Packs rather than being left behind empty.
   previousKitName: the kit was renamed in the portal since its Sheet was
   last written - if there's no Sheet under the new name yet but there is
   one under the old name, that file is renamed (a Sheet's title IS its
   Drive file name) and rewritten, so one kit never ends up with two Sheets. */
function getOrCreateKitSpreadsheet_(jobFolder, jobNumber, kitName, previousKitName, previousJobNumber) {
  const name = jobNumber + ' - ' + kitName + ' - Pack Log';

  const current = findLiveFileByName_(jobFolder, name);
  if (current) return SpreadsheetApp.open(current);

  // The kit moved to another job number since its Sheet was written: the
  // Sheet is still "<old job> - <old name> - Pack Log" in "<old job>/" -
  // move it here and rename it (moveTo keeps the file, its id and its link).
  if (previousJobNumber && previousJobNumber !== jobNumber) {
    const parent = DriveApp.getFolderById(PARENT_FOLDER_ID);
    const oldJobFolder = findLiveFolderByName_(parent, previousJobNumber);
    const oldSheet = oldJobFolder &&
      findLiveFileByName_(oldJobFolder, previousJobNumber + ' - ' + (previousKitName || kitName) + ' - Pack Log');
    if (oldSheet) {
      oldSheet.moveTo(jobFolder);
      oldSheet.setName(name);
      return SpreadsheetApp.open(oldSheet);
    }
  }

  if (previousKitName && previousKitName !== kitName) {
    const old = findLiveFileByName_(jobFolder, jobNumber + ' - ' + previousKitName + ' - Pack Log');
    if (old) {
      old.setName(name);
      return SpreadsheetApp.open(old);
    }
  }

  const ss = SpreadsheetApp.create(name);
  const file = DriveApp.getFileById(ss.getId());
  jobFolder.addFile(file);
  DriveApp.getRootFolder().removeFile(file);
  ss.getSheets()[0].setName(KIT_PACKS_TAB);
  return ss;
}

/* A kit that changed job number: its photos already in Drive (sent by an
   earlier submission into "<old job>/Packing Photos") move to this job's
   photo folder. Moving keeps each file's id, so the links in the Pack Log
   (and anywhere else they were shared) keep working. Idempotent: a photo
   already there, trashed or gone is skipped. Returns how many moved. */
function moveKitPhotos_(jobFolder, photoFolderName, fileIds) {
  if (!Array.isArray(fileIds) || !fileIds.length) return 0;
  const target = getOrCreateChild_(jobFolder, String(photoFolderName));
  const targetId = target.getId();
  let moved = 0;
  fileIds.forEach(function (id) {
    let file;
    try { file = DriveApp.getFileById(String(id)); } catch (e) { return; }
    if (file.isTrashed()) return;
    const parents = file.getParents();
    while (parents.hasNext()) {
      if (parents.next().getId() === targetId) return;
    }
    file.moveTo(target);
    moved++;
  });
  return moved;
}

/* The first file called `name` in `folder` that isn't in Drive's trash, or
   null. getFilesByName also returns trashed files - writing into one of
   those would report success while nobody could see the Sheet. */
function findLiveFileByName_(folder, name) {
  const it = folder.getFilesByName(name);
  while (it.hasNext()) {
    const file = it.next();
    if (!file.isTrashed()) return file;
  }
  return null;
}

/* generalPhotos: the kit's photos that belong to NO pack ({firstEdit, lastEdit, contributors,
   photoLinks}, or null) - written as one last "No pack" row, so every photo of the kit is linked
   from its Pack Log. An older portal simply doesn't send it. */
function writeKitPacksTab_(ss, packs, generalPhotos) {
  // insertSheet only if an older/half-made file somehow lacks the tab.
  const sheet = ss.getSheetByName(KIT_PACKS_TAB) || ss.insertSheet(KIT_PACKS_TAB, 0);
  const cols = KIT_PACKS_HEADERS.length;

  sheet.getRange(1, 1, 1, cols).setValues([KIT_PACKS_HEADERS]);
  sheet.setFrozenRows(1);

  // Both guards matter: Apps Script throws on a 0-row range, so the clear
  // only runs when there's something under the header, and the write only
  // when there's at least one pack.
  const lastRow = sheet.getLastRow();
  if (lastRow > 1) {
    sheet.getRange(2, 1, lastRow - 1, cols).clearContent();
  }

  const rows = packs.map(function (p) {
    return [
      p.packNumber == null ? '' : String(p.packNumber),
      toSheetDate_(p.firstEdit),
      toSheetDate_(p.lastEdit),
      joinForCell_(p.items, '\n'),        // one formatted line per series
      joinForCell_(p.contributors, ', '),
      joinForCell_(p.photoLinks, '\n'),
    ];
  });
  // Kit photos that belong to no pack: one last "No pack" row.
  if (generalPhotos && Array.isArray(generalPhotos.photoLinks) && generalPhotos.photoLinks.length) {
    rows.push([
      KIT_NO_PACK_LABEL,
      toSheetDate_(generalPhotos.firstEdit),
      toSheetDate_(generalPhotos.lastEdit),
      '',
      joinForCell_(generalPhotos.contributors || [], ', '),
      joinForCell_(generalPhotos.photoLinks, '\n'),
    ]);
  }

  if (rows.length > 0) {
    const n = rows.length;
    sheet.getRange(2, 1, n, 1).setNumberFormat('@');                    // Pack
    sheet.getRange(2, 2, n, 2).setNumberFormat(KIT_DATE_TIME_FORMAT);   // First Edit, Last Edit
    sheet.getRange(2, 4, n, 3).setNumberFormat('@');                    // Item Number(s), Collaborators, Photo Links
    sheet.getRange(2, 1, n, cols).setValues(rows);
  }

  // Multi-line cells (one item line / one link per line) - see the same
  // wrap handling in getOrCreateLogSheet_.
  sheet.getRange('D2:D').setWrapStrategy(SpreadsheetApp.WrapStrategy.WRAP);
  sheet.getRange('F2:F').setWrapStrategy(SpreadsheetApp.WrapStrategy.WRAP);
  sheet.autoResizeColumns(1, 5);                     // everything but the links
  sheet.setColumnWidth(6, PHOTO_LINKS_COLUMN_WIDTH); // Photo Links - fixed, not auto-fit
}

// The Packs tab's last row for kit photos that belong to no pack.
const KIT_NO_PACK_LABEL = 'No pack';

function writeKitSummaryTab_(ss, body) {
  const sheet = ss.getSheetByName(KIT_SUMMARY_TAB) ||
    ss.insertSheet(KIT_SUMMARY_TAB, ss.getSheets().length);
  sheet.clear(); // contents AND formats - rebuilt from scratch every call

  const SUBMITTED_AT_ROW = 4; // the only non-text cell below
  const details = [
    ['Kit', String(body.kitName)],
    ['Job Number', String(body.jobNumber)],
    // A reopened kit's re-submission sends its own submitter/time - these
    // two always describe the LATEST submission, the row after says how
    // many there have been.
    ['Submitted by', String(body.finalizedBy || '')],
    ['Submitted at', toSheetDate_(body.finalizedAt)],
    ['Times submitted', String(kitSubmitCount_(body.submitCount))],
    ['Packed completely', 'Yes'], // logKit only ever follows a "Yes" to "Is this packed completely?"
  ];
  const report = Array.isArray(body.report) ? body.report : [];
  const table = [['Series', 'Last Number', 'Missing']].concat(report.map(function (r) {
    const missing = Array.isArray(r.missing) ? r.missing : [];
    return [
      String(r.series || ''),
      r.lastNumber == null ? '' : String(r.lastNumber),
      // The FULL list - the portal's on-screen text caps it at 30, a cell
      // only at Sheets' own per-cell limit (see KIT_CELL_MAX_CHARS).
      missing.length ? joinForCell_(missing, ', ') : 'None',
    ];
  }));
  const tableStart = details.length + 2; // one blank row between the two blocks

  sheet.getRange(1, 1, details.length, 2).setNumberFormat('@');
  sheet.getRange(SUBMITTED_AT_ROW, 2).setNumberFormat(KIT_DATE_TIME_FORMAT);
  sheet.getRange(1, 1, details.length, 2).setValues(details);

  sheet.getRange(tableStart, 1, table.length, 3).setNumberFormat('@');
  sheet.getRange(tableStart, 1, table.length, 3).setValues(table);
  sheet.getRange(tableStart, 3, table.length, 1).setWrapStrategy(SpreadsheetApp.WrapStrategy.WRAP);

  sheet.autoResizeColumns(1, 2);
  sheet.setColumnWidth(3, KIT_MISSING_COLUMN_WIDTH);
}

/* body.submitCount as a whole number of at least 1. A portal from before
   kits could be reopened doesn't send it at all, and logKit only ever
   follows a Final Submit - so anything missing, zero or malformed means the
   kit has been submitted once. */
function kitSubmitCount_(value) {
  const n = parseInt(value, 10);
  return n >= 1 ? n : 1;
}

/* A portal ISO timestamp -> a Date for a date-formatted cell, or '' (an
   empty cell) when there isn't one. Never new Date(null) - that's
   1970-01-01, a plausible-looking wrong date instead of an obvious blank.
   Anything unparseable is kept as its raw text rather than silently lost. */
function toSheetDate_(value) {
  if (!value) return '';
  const d = new Date(value);
  return isNaN(d.getTime()) ? String(value) : d;
}

/* Joins a list for one Pack Log cell, stopping short of KIT_CELL_MAX_CHARS
   and summarizing whatever didn't fit as "& N more" (on its own line for
   newline-separated lists). Anything that fits - i.e. every realistic kit -
   comes out exactly like list.join(sep). A missing list is an empty cell. */
function joinForCell_(list, sep) {
  if (!Array.isArray(list) || !list.length) return '';
  const room = KIT_CELL_MAX_CHARS - 40; // leaves space for the "& N more" suffix
  let out = '';
  for (let i = 0; i < list.length; i++) {
    const part = String(list[i]);
    const piece = (i ? sep : '') + part;
    if (out.length + piece.length > room) {
      // A single entry too long on its own is cut rather than dropped.
      if (i === 0) out = part.slice(0, room) + '…';
      const left = list.length - (i === 0 ? 1 : i);
      return left ? out + (sep === '\n' ? '\n' : ' ') + '& ' + left + ' more' : out;
    }
    out += piece;
  }
  return out;
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
