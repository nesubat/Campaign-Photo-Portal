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
 *    3. Change SHARED_SECRET below to a long random string - treat it
 *       like a password. This is the only thing stopping a stranger who
 *       finds the URL from writing junk files into your Drive.
 *    3b. Set PARENT_FOLDER_ID below to the ID of the Drive folder that
 *       should hold every <JobNumber> folder (the long id in that
 *       folder's URL: drive.google.com/drive/folders/<ID>). The account
 *       running this script needs edit access to that folder.
 *    4. Deploy > New deployment > type: Web app.
 *         Execute as:      Me
 *         Who has access:  Anyone
 *       (svc runs as you regardless of who calls it - the "Anyone" only
 *       controls who can reach the URL at all; SHARED_SECRET is the
 *       actual gate.)
 *    5. Copy the Web App URL it gives you.
 *    6. On the Windows machine, copy
 *         data/drive_config.json.example  ->  data/drive_config.json
 *       and paste that URL into "webAppUrl", and your SHARED_SECRET
 *       value into "sharedSecret". Restart the portal (py app.py).
 *    7. Test: upload one photo from the portal and check this Drive
 *       account for a new folder matching the job number.
 *
 *  If you ever edit this script after deploying, use
 *  Deploy > Manage deployments > pencil icon > New version - editing
 *  the code alone does NOT update a live Web App URL.
 ***********************************************************************/

const SHARED_SECRET = 'IjbxBpCaXD7zlNyhPFl2uhQWgRbdt0';      // must match drive_config.json
const PARENT_FOLDER_ID = '14L_NMl5jchc1l11PhLQ5WKlpIfqUQMuy'; // Drive folder that holds every <JobNumber> folder

function doPost(e) {
  try {
    const body = JSON.parse(e.postData.contents);

    if (body.secret !== SHARED_SECRET) {
      return jsonOut({ ok: false, error: 'bad secret' });
    }
    if (!body.jobNumber || !body.category || !body.fileName || !body.contentB64) {
      return jsonOut({ ok: false, error: 'missing jobNumber/category/fileName/contentB64' });
    }

    const folder = getOrCreateJobFolder_(body.jobNumber, body.category);

    const bytes = Utilities.base64Decode(body.contentB64);
    const mime = guessMime_(body.fileName);
    const blob = Utilities.newBlob(bytes, mime, body.fileName);

    const file = folder.createFile(blob);
    file.setDescription(
      'Uploaded by ' + (body.employeeName || 'unknown') +
      ' at ' + (body.uploadedAt || new Date().toISOString())
    );

    return jsonOut({ ok: true, fileId: file.getId(), url: file.getUrl() });
  } catch (err) {
    return jsonOut({ ok: false, error: String(err) });
  }
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
