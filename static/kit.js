// New Store Kits - the kit-mode upload page. templates/upload.html loads this
// INSTEAD of upload.js whenever the session is a kit (kit_mode), so upload.js
// stays exactly as it was and knows nothing about kits.
//
// A kit is ONE shared session that up to ~4 people pack into at the same
// time. The kit is split into numbered packs (physical boxes). A person edits
// a pack only while they hold its reservation, and holds at most one pack at a
// time. Everything on screen is drawn from a KitState (see app.py's
// _kit_state): first the page's own data-kit-state, then every poll
// (/api/kit/state, every kit_poll_ms while the page is visible) and every API
// response, which all carry a fresh one.
//
// The rules below come from real problems on warehouse phones over a slow
// hotspot:
//   * States can arrive out of order (a slow poll landing after a fast add),
//     so each carries kits.rev and a state older than the last one applied is
//     dropped (apply()).
//   * render() is keyed and in-place. It never rebuilds a list wholesale and
//     never touches what the user is typing in #seriesInput/#indexInput, so a
//     colleague's change arriving mid-entry can't move a field or a button
//     under the user's thumb or wipe a half-typed value. A node replaced
//     under a finger drops the tap on iOS, so re-renders wait until a touch
//     has ended.
//   * No poll is sent while a JSON mutation is in flight, and polls never
//     overlap. That way an old poll can't undo what the user just did. Every
//     request has a timeout, because iOS can leave a fetch hanging forever
//     after the phone locks.
//   * The number pad only opens if focus() runs synchronously inside the
//     user's own tap or keystroke, so every "move the cursor" happens there,
//     never after an await.
//
// Pack labels print on the server's Zebra printer (POST /api/kit/labels/print).
// Which packs are ticked for a bulk print is this phone's own business, so
// the selection lives here, never in the state, and survives every re-render.
//
// A submitted kit can be reopened (POST /session/<id>/reopen). Photos that
// were already sent to Drive then come back "locked" - shown, never removable -
// and the next Final Submit rewrites the kit's Drive sheet.
(function () {
  'use strict';

  var pageDataEl = document.getElementById('pageData');
  if (!pageDataEl) return;
  var pageData = pageDataEl.dataset;
  if (!parseJson(pageData.kitMode, false)) return;

  var SESSION_ID = parseJson(pageData.sessionId, '');
  var initialState = parseJson(pageData.kitState, null);
  if (!initialState) return;

  var POLL_MS = Math.max(1000, Number(parseJson(pageData.kitPollMs, 4000)) || 4000);
  var POLL_TIMEOUT_MS = 10000;
  var MUTATION_TIMEOUT_MS = 20000;
  var UPLOAD_TIMEOUT_MS = 120000;
  // Plain HTTP/1.1 allows only ~6 connections per host. Two uploads at a
  // time leaves the rest free for polls, item adds and the release on the
  // way out, which would otherwise queue behind 8MB photos on a hotspot.
  var MAX_PARALLEL_UPLOADS = 2;
  var TOUCH_SETTLE_MS = 300;
  var TOUCH_DEFER_MAX_MS = 1500; // never hold a render back longer than this (a lost touchend must not freeze the page)
  var NOTICE_MS = 4000;
  var ERROR_NOTICE_MS = 8000;       // a printer error is worth more than 4s to read
  var KEYBOARD_MIN_PX = 80;         // visual viewport this much shorter than the layout one = keyboard up
  var LEAVE_RELEASE_WAIT_MS = 1500;
  var RELEASE_URL = '/api/kit/pack/release';
  var PRINT_URL = '/api/kit/labels/print';
  var JSON_HEADERS = { 'Content-Type': 'application/json' };
  var J_SERIES_RE = /^[Jj]\d{6}$/;
  // Same as the server's kits.J_WITH_INDEX_RE: indexes are digits only.
  var J_WITH_INDEX_RE = /^([Jj]\d{6})\s*[-–—]\s*(\d{1,6})$/;
  var INDEX_LEAD_RE = /^[\s\-–—]+/;
  var INDEX_MAX_LEN = 6;

  function byId(id) { return document.getElementById(id); }

  var counter = byId('counter');
  var homeLink = byId('homeLink');
  var cancelLink = byId('cancelLink');
  var deleteForm = byId('kitDeleteForm');
  var kitNotice = byId('kitNotice');
  var packEditor = byId('packEditor');
  var packEditorTitle = byId('packEditorTitle');
  var savePackBtn = byId('savePackBtn');
  var seriesInput = byId('seriesInput');
  var addNoIndexBtn = byId('addNoIndexBtn');
  var seriesButtons = byId('seriesButtons');
  var packStatus = byId('packStatus');
  var indexInput = byId('indexInput');
  var indexAddBtn = byId('indexAddBtn');
  var packItemLines = byId('packItemLines');
  var packPhotoStrip = byId('packPhotoStrip');
  var packActions = byId('packActions');
  var printControls = byId('printControls');
  var printSelectAll = byId('printSelectAll');
  var printSelectedBtn = byId('printSelectedBtn');
  var printBarFinalized = byId('printBarFinalized');
  var reopenedBanner = byId('reopenedBanner');
  var reopenedText = byId('reopenedText');
  var reopenedDismiss = byId('reopenedDismiss');
  var reopenForm = byId('kitReopenForm');
  var reopenBtn = byId('kitReopenBtn');
  var submitBar = document.querySelector('.kit-submit-bar');
  var cameraInput = byId('cameraInput');
  var libraryInput = byId('libraryInput');
  var cameraLabel = byId('cameraLabel');
  var libraryLabel = byId('libraryLabel');
  var uploadingStrip = byId('uploadingStrip');
  var newPackBar = byId('newPackBar');
  var newPackBtn = byId('newPackBtn');
  var newPackNext = byId('newPackNext');
  var kitWorking = byId('kitWorking');
  var kitReport = byId('kitReport');
  var kitReportLines = byId('kitReportLines');
  var kitReportOpen = byId('kitReportOpen');
  var packList = byId('packList');
  var packDialog = byId('packDialog');
  var packDialogTitle = byId('packDialogTitle');
  var packDialogCollab = byId('packDialogCollab');
  var packDialogClose = byId('packDialogClose');
  var packDialogBody = packDialog ? packDialog.querySelector('.section-dialog-body') : null;
  var packDialogLines = byId('packDialogLines');
  var packDialogItemLines = byId('packDialogItemLines');
  var packDialogGallery = byId('packDialogGallery');
  var packDialogMeta = byId('packDialogMeta');
  var packDialogNotice = byId('packDialogNotice');
  var packDialogEdit = byId('packDialogEdit');
  var finalSubmitBtn = byId('finalSubmitBtn');
  var finalWaiting = byId('finalWaiting');
  var finalDialog = byId('finalDialog');
  var finalDialogClose = byId('finalDialogClose');
  var finalDialogReport = byId('finalDialogReport');
  var finalDialogEmpty = byId('finalDialogEmpty');
  var finalDialogResubmit = byId('finalDialogResubmit');
  var finalDialogNotice = byId('finalDialogNotice');
  var finalNoBtn = byId('finalNoBtn');
  var finalYesBtn = byId('finalYesBtn');
  var kitPhotos = byId('kitPhotos');
  var kitPhotosCount = byId('kitPhotosCount');
  var kitPhotoStrip = byId('kitPhotoStrip');
  var kitPhotosEmpty = byId('kitPhotosEmpty');
  var kitCaptureBar = byId('kitCaptureBar');
  var kitCameraInput = byId('kitCameraInput');
  var kitLibraryInput = byId('kitLibraryInput');
  var kitUploadingStrip = byId('kitUploadingStrip');
  var kitNameDisplay = byId('kitNameDisplay');
  var kitRenameBtn = byId('kitRenameBtn');
  var kitRenameForm = byId('kitRenameForm');
  var kitRenameInput = byId('kitRenameInput');
  var kitJobInput = byId('kitJobInput');
  var jobDisplay = byId('jobDisplay');
  var kitRenameSave = byId('kitRenameSave');
  var kitRenameCancel = byId('kitRenameCancel');
  var TITLE_SUFFIX = ' - New Store Kit';
  var JOB_NUMBER_RE = /^[Jj]\d{6}$/;

  // --- Client state ---------------------------------------------------------
  var state = null;                 // last KitState applied
  var lastRev = -Infinity;
  var initialFinalized = !!initialState.finalized;
  // The pack this client believes it holds. It is sent as holding_pack_id on
  // every poll, which doubles as the reservation's heartbeat. It is set to
  // null the moment a release/reserve/new starts (see packMove), so a poll
  // that was already in flight can't report the pack being left as "lost".
  var heldPackId = null;
  var heldPackNumber = null;        // cached, since lostPack.number is null once the pack row is gone
  var editorPackId = null;          // the pack #packEditor is currently showing (null = hidden)
  var seriesJumpedFor = '';         // the series the item field last auto-advanced for (see onSeriesInput)
  var failedAdds = [];              // [{series, raw}] index adds that failed and couldn't go back in the field
  var packTransitions = 0;          // release/reserve/new requests in flight
  var heldSyncPending = false;      // a state was applied mid-transition without updating heldPackId
  var mutationsInFlight = 0;        // every JSON mutation (item add/remove, photo delete, pack moves, finalize)
  var addsInFlight = 0;
  var addWaiters = [];
  var leaving = false;              // navigating away / reloading - nothing more gets applied or polled
  var leaveInProgress = false;
  var suppressUnloadPrompt = false;

  // Keyed render registries: key -> {el, sig}. A node is rebuilt only when
  // its own signature changes. It is moved only when its position is wrong.
  var cardRegistry = {};
  var lineRegistry = {};            // editor item lines
  var stripRegistry = {};           // editor photo strip
  var seriesRegistry = {};
  var reportRegistry = {};
  var dialogLineRegistry = {};
  var dialogPhotoRegistry = {};
  var dialogLinesSig = null;
  var openPackId = null;            // pack #packDialog is showing
  var finalDialogRev = null;        // the rev the final dialog is showing - sent with "Yes"
  var finalReportSig = null;
  var finalBusy = false;

  // Touch tracking for the render deferral.
  var touchActive = false;
  var touchTimer = null;
  var renderPending = false;
  var deferTimer = null;

  // Polling.
  var pollTimer = null;
  var pollToken = null;             // the in-flight poll, if any ({ctrl})
  var pollDeferred = false;         // the timer fired during a mutation - poll right after it settles
  var pollFailures = 0;
  var pollingStopped = false;

  // Uploads.
  var uploadQueue = [];
  var uploadsActive = 0;
  var pendingUploads = 0;           // queued + in flight
  var pendingPackUploads = 0;       // ...of those, the ones going into a pack (a no-pack kit photo needs no pack held)
  var generalRegistry = {};         // keyed tiles of #kitPhotoStrip
  var failedJobs = [];
  var unloadGuardOn = false;

  // Label printing. selectedPacks is the set of pack ids ticked for a bulk
  // print ({id: true}), pruned whenever a pack disappears or empties.
  // printing is what's printing right now: null, 'bulk' or 'pack:<id>' - one
  // print at a time, and the button that started it reads "Printing…".
  var selectedPacks = {};
  var printing = null;

  // Rev 5: the "reopened" note is hidden for this page load once dismissed.
  var reopenedDismissed = false;

  // Notices.
  var noticeTimer = null;
  var noticeMessage = '';
  var noticeLink = null;            // {href, text} - a toast carrying a link stays until closed
  var noticeSticky = false;
  var reconnecting = false;

  // ==========================================================================
  // Small helpers
  // ==========================================================================
  function parseJson(raw, fallback) {
    if (raw === undefined || raw === null || raw === '') return fallback;
    try { return JSON.parse(raw); } catch (e) { return fallback; }
  }

  function noop() {}

  function preventDefault(e) { e.preventDefault(); }

  function setText(node, text) {
    if (node && node.textContent !== text) node.textContent = text;
  }

  function show(node, visible) {
    if (node && node.classList.contains('hidden') === !!visible) node.classList.toggle('hidden', !visible);
  }

  function clearChildren(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
  }

  function findPack(st, packId) {
    if (!st || packId === null || packId === undefined) return null;
    var packs = st.packs || [];
    for (var i = 0; i < packs.length; i++) {
      if (packs[i].id === packId) return packs[i];
    }
    return null;
  }

  // Same wording as kits.join_and on the server: "02", "02 & 03", "02, 03 & 04".
  function joinAnd(parts) {
    if (parts.length <= 1) return parts.join('');
    return parts.slice(0, -1).join(', ') + ' & ' + parts[parts.length - 1];
  }

  // The idle suffix only appears from 2 minutes. Ordinary 4s poll jitter
  // must never read as "idle 0 min".
  function idleShown(minutes) {
    return (typeof minutes === 'number' && minutes >= 2) ? minutes : 0;
  }

  function workingLabel(w) {
    var idle = idleShown(w.idleMinutes);
    return w.name + ' (Pack ' + w.packNumber + (idle ? ', idle ' + idle + ' min' : '') + ')';
  }

  function cleanIndex(raw) {
    return String(raw || '').replace(INDEX_LEAD_RE, '').trim();
  }

  // The server's display stamps ("2026/09/27 10:01:02 AM AEST") are too long
  // for a pack card's side column. Today's print reads "10:01 AM", an older
  // one "27/09 10:01 AM". The full stamp stays in the title.
  function shortStamp(display) {
    var text = String(display || '');
    var m = /^(\d{4})\/(\d{2})\/(\d{2}) (\d{1,2}):(\d{2})(?::\d{2})?(?: ?(AM|PM))?/.exec(text);
    if (!m) return text;
    var now = new Date();
    var today = now.getFullYear() === Number(m[1]) && now.getMonth() + 1 === Number(m[2]) && now.getDate() === Number(m[3]);
    var time = m[4].replace(/^0(?=\d)/, '') + ':' + m[5] + (m[6] ? ' ' + m[6] : '');
    return today ? time : m[3] + '/' + m[2] + ' ' + time;
  }

  function selectedIds() {
    return Object.keys(selectedPacks).map(Number);
  }

  function kitError(kind, message) {
    var err = new Error(message);
    err.kind = kind;
    return err;
  }

  function failMessage(err, what) {
    if (err && err.kind === 'timeout') return 'Couldn\'t ' + what + ' - the server took too long. Try again.';
    return 'Couldn\'t ' + what + ' - check your connection and try again.';
  }

  function newController() {
    return typeof AbortController === 'function' ? new AbortController() : null;
  }

  function isOpen(dialog) {
    return !!(dialog && dialog.open);
  }

  function openDialog(dialog) {
    if (typeof dialog.showModal === 'function') {
      if (!dialog.open) dialog.showModal();
    } else {
      dialog.setAttribute('open', ''); // very old browsers only
    }
  }

  function closeDialog(dialog) {
    if (!dialog || !dialog.open) return;
    if (typeof dialog.close === 'function') {
      dialog.close();
    } else {
      dialog.removeAttribute('open');
      dialog.dispatchEvent(new Event('close'));
    }
  }

  // "Working…" from the tap until the response settles. That stops the
  // double tap which, on a slow hotspot, would send the request twice. The
  // idle label is parked in data-idle-label, and render() writes a changed
  // label there while the button is busy (see setButtonLabel).
  function setBusy(btn, busy) {
    if (!btn) return;
    if (busy) {
      if (!btn.hasAttribute('data-idle-label')) btn.setAttribute('data-idle-label', btn.textContent);
      btn.textContent = 'Working…';
      btn.disabled = true;
      btn.classList.add('is-busy');
    } else {
      var label = btn.getAttribute('data-idle-label');
      if (label !== null) btn.textContent = label;
      btn.removeAttribute('data-idle-label');
      btn.disabled = false;
      btn.classList.remove('is-busy');
    }
  }

  function isBusy(btn) {
    return !!(btn && btn.hasAttribute('data-idle-label'));
  }

  function setButtonLabel(btn, text) {
    if (!btn) return;
    if (isBusy(btn)) btn.setAttribute('data-idle-label', text);
    else setText(btn, text);
  }

  // ==========================================================================
  // Notices
  // ==========================================================================
  // #kitNotice is the page-level toast for kit-level messages, at the bottom
  // just above the Cancel / Final Submit bar (never over the editor header,
  // the inputs or Save Pack, which sit near the top). It auto-hides after a
  // few seconds, except "Reconnecting…", which stays until a poll gets
  // through again, and a toast carrying a link (the label preview), which
  // stays until its × is tapped - it's no use if it vanishes mid-reach.
  // opts: {link: {href, text}, sticky: bool, ms: number}.
  function showNotice(message, opts) {
    // Once the page is on its way out (kit deleted, submitted elsewhere,
    // Home/Cancel), a late "Couldn't save the pack" would only flash up
    // over a page that is already being replaced.
    if (leaving) return;
    opts = opts || {};
    noticeMessage = message;
    noticeLink = opts.link || null;
    noticeSticky = !!(opts.link || opts.sticky);
    if (noticeTimer) { clearTimeout(noticeTimer); noticeTimer = null; }
    if (!noticeSticky) noticeTimer = setTimeout(clearNotice, opts.ms || NOTICE_MS);
    paintNotice();
  }

  function clearNotice() {
    if (noticeTimer) { clearTimeout(noticeTimer); noticeTimer = null; }
    noticeMessage = '';
    noticeLink = null;
    noticeSticky = false;
    paintNotice();
  }

  function setReconnecting(on) {
    if (reconnecting === on) return;
    reconnecting = on;
    paintNotice();
  }

  function paintNotice() {
    if (!kitNotice) return;
    var text = noticeMessage || (reconnecting ? 'Reconnecting…' : '');
    clearChildren(kitNotice);
    if (text) {
      var span = document.createElement('span');
      span.className = 'kit-notice-text';
      span.textContent = text;
      kitNotice.appendChild(span);
    }
    if (noticeMessage && noticeLink) {
      // A real link, not a window.open() after the fetch: that would come
      // too late to count as the user's tap, and popup blockers eat it.
      var a = document.createElement('a');
      a.className = 'kit-notice-link';
      a.href = noticeLink.href;
      a.target = '_blank';
      a.rel = 'noopener';
      a.textContent = noticeLink.text;
      kitNotice.appendChild(a);
    }
    if (noticeMessage && noticeSticky) {
      var close = document.createElement('button');
      close.type = 'button';
      close.className = 'kit-notice-close';
      close.textContent = '×';
      close.setAttribute('aria-label', 'Close this message');
      close.addEventListener('click', clearNotice);
      kitNotice.appendChild(close);
    }
    kitNotice.classList.toggle('is-reconnecting', !noticeMessage && reconnecting);
    kitNotice.classList.toggle('is-sticky', !!noticeMessage && noticeSticky);
    show(kitNotice, !!text);
    if (text) positionNotice();
  }

  // Just above the submit bar - or, while the on-screen keyboard is up (the
  // bar is then hidden behind it), just above the keyboard. Only the visual
  // viewport knows where the keyboard is; without it, CSS's default stands.
  function positionNotice() {
    if (!kitNotice) return;
    var bottom = '';
    var vv = window.visualViewport;
    var keyboard = vv ? window.innerHeight - vv.height - vv.offsetTop : 0;
    if (keyboard > KEYBOARD_MIN_PX) {
      bottom = Math.round(keyboard + 10) + 'px';
    } else if (submitBar && submitBar.offsetHeight) {
      bottom = (submitBar.offsetHeight + 10) + 'px';
    }
    if (kitNotice.style.bottom !== bottom) kitNotice.style.bottom = bottom;
  }

  function showDialogNotice(node, message) {
    if (!node) return;
    setText(node, message);
    show(node, !!message);
  }

  // A modal <dialog> sits in the browser's top layer, above the fixed
  // #kitNotice. So while one is open, its own notice line is where a message
  // has to go to be seen at all.
  function notify(message, opts) {
    if (leaving) return; // see showNotice
    if (isOpen(packDialog)) showDialogNotice(packDialogNotice, message);
    else if (isOpen(finalDialog)) showDialogNotice(finalDialogNotice, message);
    else showNotice(message, opts);
  }

  // #packStatus sits on the "Index number" label line, just above the index
  // field. That keeps it visible over the on-screen number pad, where
  // anything below the field would be hidden during continuous entry.
  function setPackStatus(text, kind) {
    if (!packStatus) return;
    setText(packStatus, text);
    packStatus.className = 'pack-status' + (kind ? ' is-' + kind : '');
    packStatus.title = text;
  }

  // ==========================================================================
  // Networking
  // ==========================================================================
  function goTo(url) {
    leaving = true;
    suppressUnloadPrompt = true;
    stopPolling();
    location.href = url;
  }

  function readJson(response) {
    // A logged-out session answers every API call with a redirect to the
    // login page. Nothing on this page can work any more, so go there.
    if (response.redirected && /\/login(?:[?#]|$)/.test(response.url)) {
      goTo('/login');
      throw kitError('logged_out', 'Logged out.');
    }
    return response.json().catch(function () {
      throw kitError('bad_response', 'Unexpected server response (' + response.status + ').');
    });
  }

  // Every call gets a timeout. An iOS fetch left hanging by a phone lock
  // would otherwise hold its in-flight flag (poll, mutation or upload count)
  // forever. Branching is on the JSON body's `code`, never on the HTTP status.
  function request(url, options, timeoutMs, ctrl) {
    ctrl = ctrl || newController();
    if (ctrl) options.signal = ctrl.signal;
    options.credentials = 'same-origin';
    return new Promise(function (resolve, reject) {
      var settled = false;
      var timer = setTimeout(function () {
        if (settled) return;
        settled = true;
        if (ctrl) { try { ctrl.abort(); } catch (e) { /* already done */ } }
        reject(kitError('timeout', 'The server took too long to answer.'));
      }, timeoutMs);
      fetch(url, options).then(readJson).then(function (data) {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(data || {});
      }, function (err) {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        reject(err && err.kind ? err : kitError('network', 'Network error.'));
      });
    });
  }

  function postJson(url, body, timeoutMs, ctrl) {
    return request(url, { method: 'POST', headers: JSON_HEADERS, body: JSON.stringify(body) }, timeoutMs, ctrl);
  }

  function mutate(url, body) {
    body.session_id = SESSION_ID;
    mutationsInFlight++;
    return postJson(url, body, MUTATION_TIMEOUT_MS).then(function (data) {
      mutationDone();
      return data;
    }, function (err) {
      mutationDone();
      throw err;
    });
  }

  function mutationDone() {
    mutationsInFlight = Math.max(0, mutationsInFlight - 1);
    if (mutationsInFlight === 0 && pollDeferred) {
      pollDeferred = false;
      schedulePoll(0); // a macrotask - the response's own state is applied first
    }
  }

  // Release / reserve / new. heldPackId goes null for the duration (see its
  // declaration), and capture is off, so a photo can't be tagged to a pack
  // that is being left.
  function packMove(url, body) {
    packTransitions++;
    heldPackId = null;
    updateCaptureEnabled();
    updateRetryButtons();
    return mutate(url, body).then(function (data) {
      packTransitions--;
      // A poll that the server handled AFTER this move may already have been
      // applied while the move was in flight (and heldPackId left alone). If
      // so, this response has an older or equal rev and may be dropped as
      // stale, so take heldPackId from that newer state now. If this
      // response is newer, the caller's handleResponse overwrites it anyway.
      if (packTransitions === 0 && heldSyncPending && state) {
        heldPackId = state.heldPackId || null;
        heldSyncPending = false;
      }
      return data;
    }, function (err) {
      packTransitions--;
      if (packTransitions === 0) heldSyncPending = false;
      // Whether the server acted is unknown. Ask it straight away rather
      // than guess (the poll sends holding_pack_id null, so it can't raise
      // a false lostPack, and its heldPackId is the truth).
      schedulePoll(0);
      throw err;
    });
  }

  // Applies whatever state a response carries. Returns true only for ok:true.
  function handleResponse(data, immediate) {
    if (!data) return false;
    if (data.ok) {
      if (data.state) apply(data.state, immediate);
      return true;
    }
    if (data.code === 'kit_missing') {
      kitGone();
      return false;
    }
    if (data.state) apply(data.state, immediate);
    return false;
  }

  function sendKeepaliveRelease(packId) {
    var body = JSON.stringify({ session_id: SESSION_ID, pack_id: packId });
    try {
      // keepalive: the request outlives this page, so the release still
      // lands even though we navigate away without waiting for it.
      return fetch(RELEASE_URL, {
        method: 'POST', headers: JSON_HEADERS, body: body, keepalive: true, credentials: 'same-origin',
      }).then(noop, noop);
    } catch (e) {
      return Promise.resolve();
    }
  }

  function kitGone() {
    if (leaving) return;
    leaving = true;
    suppressUnloadPrompt = true;
    stopPolling();
    sendKeepaliveRelease(null);
    alert('This kit was deleted.');
    location.href = '/';
  }

  function finalizedElsewhere() {
    // A collaborator's Final Submit went through (or, on a submitted kit's
    // page, someone reopened it). Reload, so the server renders the view
    // that fits now: read-only with its "Submitted by" banner, or editable.
    leaving = true;
    suppressUnloadPrompt = true;
    stopPolling();
    location.reload();
  }

  // ==========================================================================
  // State application
  // ==========================================================================
  function apply(st, immediate) {
    if (!st || typeof st.rev !== 'number' || leaving) return false;
    if (st.rev < lastRev) return false; // older than what's on screen - a slow response overtaken by a newer one
    lastRev = st.rev;

    // The server couldn't heartbeat the pack we said we hold. Only believe
    // that for the pack this client still thinks it holds (not one it is
    // itself leaving), and only if the server doesn't say we hold it anyway.
    var lost = st.lostPack;
    if (lost && heldPackId !== null && lost.id === heldPackId && st.heldPackId !== lost.id) {
      var number = (lost.number !== null && lost.number !== undefined) ? lost.number : heldPackNumber;
      notify(lost.takenBy
        ? 'Pack ' + number + ' was taken over by ' + lost.takenBy + '.'
        : 'Pack ' + number + ' is no longer reserved for you.');
    }

    if (packTransitions === 0) {
      heldPackId = (st.heldPackId === undefined) ? null : st.heldPackId;
      heldSyncPending = false;
    } else {
      heldSyncPending = true;
    }
    var held = findPack(st, st.heldPackId);
    if (held) heldPackNumber = held.number;
    state = st;

    // Either way round, the page on screen is the wrong kind now. A submitted
    // kit's page doesn't poll, so the only way it learns about a reopen is
    // the state a label print hands back.
    if (!!st.finalized !== initialFinalized) {
      finalizedElsewhere();
      return true;
    }
    if (immediate || !touchActive) render();
    else deferRender();
    return true;
  }

  function deferRender() {
    renderPending = true;
    if (!deferTimer) {
      deferTimer = setTimeout(function () {
        deferTimer = null;
        if (renderPending) render();
      }, TOUCH_DEFER_MAX_MS);
    }
  }

  // A drawing bug must never break the state flow around it (in-flight
  // counters, busy buttons, the poll loop), so render() runs guarded. The
  // next state simply draws again.
  function render() {
    try {
      renderNow();
    } catch (err) {
      if (window.console) console.error('Kit render failed:', err);
    }
  }

  function renderNow() {
    renderPending = false;
    if (deferTimer) { clearTimeout(deferTimer); deferTimer = null; }
    if (!state) return;
    var st = state;
    setText(counter, st.photoCount + ' photo(s)');
    // A kit that was ever submitted can't be deleted (its photos are in
    // Drive). The server already left the button out if so; this covers a
    // submit + reopen that happened between two polls.
    if (deleteForm) show(deleteForm, st.canDelete !== false && !st.finalized);
    renderKitName(st);
    renderReopened(st);
    renderEditor(st);
    renderNewPack(st);
    renderPrintHost(st);
    renderWorking(st);
    renderReport(st);
    renderKitPhotos(st);
    renderPackList(st);
    syncPrintControls();
    renderPackDialog(st);
    renderSubmit(st);
    renderFinalDialog(st);
    updateRetryButtons();
  }

  // The kit's name and job number can change under us (anyone on the kit
  // may edit them), so the topbar and the tab title follow the state. The
  // edit pane is left alone while it's open - it holds what this person is
  // typing.
  function renderKitName(st) {
    if (!st.kitName) return;
    if (kitNameDisplay) setText(kitNameDisplay, st.kitName);
    if (jobDisplay && st.jobNumber) setText(jobDisplay, st.jobNumber);
    document.title = st.kitName + ' - ' + (st.jobNumber || '') + TITLE_SUFFIX;
    if (kitRenameBtn) show(kitRenameBtn, !st.finalized);
    if (kitRenameForm && st.finalized && !kitRenameForm.classList.contains('hidden')) closeRename();
  }

  function openRename() {
    if (!kitRenameForm || !kitRenameInput || !state || state.finalized) return;
    kitRenameInput.value = state.kitName || '';
    if (kitJobInput) kitJobInput.value = state.jobNumber || '';
    kitRenameForm.classList.remove('hidden');
    // Synchronous, inside the tap - so a phone raises its keyboard. The ✏️
    // sits next to the job number, so that's where the cursor goes.
    var first = kitJobInput || kitRenameInput;
    first.focus();
    first.select();
  }

  function closeRename() {
    if (!kitRenameForm) return;
    if (kitRenameForm.contains(document.activeElement) && typeof document.activeElement.blur === 'function') {
      document.activeElement.blur();
    }
    kitRenameForm.classList.add('hidden');
  }

  function submitRename(e) {
    if (e) e.preventDefault();
    if (!kitRenameInput || isBusy(kitRenameSave)) return;
    var name = kitRenameInput.value.replace(/\s+/g, ' ').trim();
    var job = kitJobInput ? kitJobInput.value.trim().toUpperCase() : (state && state.jobNumber) || '';
    if (kitJobInput && !JOB_NUMBER_RE.test(job)) {
      notify('Job Number must be "J" followed by exactly 6 digits - e.g. J457008.');
      kitJobInput.focus();
      return;
    }
    if (!name) {
      notify('Enter the New Store Kit name.');
      kitRenameInput.focus();
      return;
    }
    var jobChanged = !!state && job !== state.jobNumber;
    if (state && name === state.kitName && !jobChanged) {
      closeRename();
      return;
    }
    // Moving a kit to another job affects everyone packing it - say so once.
    if (jobChanged && !confirm('Move this kit from ' + state.jobNumber + ' to ' + job + '?\n\n' +
        'Everyone working on it moves with it, and its photos move to ' + job + '.' +
        ((state.submitCount || 0) > 0 ? ' Its Drive sheet, labels and photos move there at the next Final Submit.' : ''))) {
      return;
    }
    var body = { kit_name: name };
    if (jobChanged) body.job_number = job;
    setBusy(kitRenameSave, true);
    mutate('/api/kit/rename', body).then(function (data) {
      setBusy(kitRenameSave, false);
      if (handleResponse(data, true)) {
        closeRename();
        notify(data.message || 'Kit updated.');
      } else if (data && data.code !== 'kit_missing') {
        // name_taken / invalid / finalized - keep the form open to fix it
        notify(data.error || 'Could not rename the kit.');
      }
    }, function (err) {
      setBusy(kitRenameSave, false);
      notify(failMessage(err, 'rename the kit'));
    });
  }

  function reopenedMessage(st) {
    return '↩️ Reopened' + (st.reopenedBy ? ' by ' + st.reopenedBy : '') +
      (st.reopenedAt ? ' on ' + st.reopenedAt : '') +
      (st.lastSubmittedAt ? ' — last submitted ' + st.lastSubmittedAt : '') +
      '. Photos already sent to Drive can\'t be removed. Press Final Submit when you\'re done to update the Drive sheet.';
  }

  function renderReopened(st) {
    if (!reopenedBanner) return;
    var on = !!st.reopened && !st.finalized && !reopenedDismissed;
    if (on) setText(reopenedText, reopenedMessage(st));
    show(reopenedBanner, on);
  }

  // Keyed, in-place list update. It removes nodes whose key is gone, builds
  // nodes for new keys, rebuilds a node only when its signature changed, and
  // moves one only when it sits in the wrong position. A node that didn't
  // change is left exactly where it is, including one under a finger or one
  // the user is scrolling past.
  function syncKeyed(container, list, registry, keyOf, sigOf, build) {
    if (!container) return;
    var wanted = {};
    var i;
    for (i = 0; i < list.length; i++) wanted[keyOf(list[i])] = true;
    Object.keys(registry).forEach(function (key) {
      if (wanted[key]) return;
      var old = registry[key].el;
      if (old.parentNode === container) container.removeChild(old);
      delete registry[key];
    });
    var cursor = container.firstChild;
    for (i = 0; i < list.length; i++) {
      var item = list[i];
      var key = keyOf(item);
      var sig = sigOf(item);
      var entry = registry[key];
      if (!entry) {
        entry = registry[key] = { el: build(item), sig: sig };
        container.insertBefore(entry.el, cursor);
      } else {
        if (entry.sig !== sig) {
          var fresh = build(item);
          if (cursor === entry.el) cursor = fresh; // the node being replaced may be the insertion point itself
          container.replaceChild(fresh, entry.el);
          entry.el = fresh;
          entry.sig = sig;
        }
        if (entry.el !== cursor) container.insertBefore(entry.el, cursor);
      }
      cursor = entry.el.nextSibling;
    }
  }

  function idKey(item) { return String(item.id); }

  // --- Editor ---------------------------------------------------------------
  function renderEditor(st) {
    if (!packEditor) return;
    var target = (!st.finalized && st.heldPackId) ? st.heldPackId : null;
    if (target !== editorPackId) switchEditor(target);
    syncKeyed(seriesButtons, st.series || [], seriesRegistry, String, String, buildSeriesButton);
    markSeriesButtons();
    if (editorPackId !== null) {
      var pack = findPack(st, editorPackId);
      if (pack) {
        setText(packEditorTitle, 'Editing Pack ' + pack.number);
        syncKeyed(packItemLines, itemLineList(pack), lineRegistry, lineKey, itemLineSig,
          function (line) { return buildItemLine(line, true); });
        syncKeyed(packPhotoStrip, pack.photos || [], stripRegistry, idKey,
          function (ph) { return photoSig(ph, true); },
          function (ph) { return buildPhotoTile(ph, true); });
      }
    }
    updateCaptureEnabled();
  }

  // The only place the editor (and so the inputs inside it) is hidden,
  // shown or reset, and it runs only when the held pack actually changed.
  function switchEditor(target) {
    // Blur before hiding. iOS can otherwise leave the keyboard up for a
    // focused input that is no longer on screen.
    var active = document.activeElement;
    if (active && active !== document.body && packEditor.contains(active) && typeof active.blur === 'function') {
      active.blur();
    }
    if (target === null) {
      packEditor.classList.add('hidden');
    } else {
      // A different pack starts clean. Anything typed for the previous one
      // was auto-added before leaving it (see prepareToLeave). The series
      // buttons put a series back with one tap.
      if (seriesInput) seriesInput.value = '';
      if (indexInput) indexInput.value = '';
      seriesJumpedFor = '';
      failedAdds = [];
      setPackStatus('', '');
      updateAddNoIndex();
      clearChildren(packItemLines);
      lineRegistry = {};
      clearChildren(packPhotoStrip);
      stripRegistry = {};
      packEditor.classList.remove('hidden');
    }
    editorPackId = target;
  }

  function buildSeriesButton(series) {
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'series-btn';
    btn.textContent = series;
    btn.setAttribute('data-series', series);
    // mousedown preventDefault keeps focus (and the keyboard) where it is.
    // The action runs on click, where focusing the index field
    // synchronously is still inside the tap, so iOS opens the number pad.
    btn.addEventListener('mousedown', preventDefault);
    btn.addEventListener('click', function () {
      if (!seriesInput || !indexInput) return;
      seriesInput.value = series;
      seriesJumpedFor = series.toUpperCase();
      updateAddNoIndex();
      indexInput.focus();
    });
    return btn;
  }

  function markSeriesButtons() {
    var current = seriesInput ? seriesInput.value.trim().toUpperCase() : '';
    Object.keys(seriesRegistry).forEach(function (key) {
      var btn = seriesRegistry[key].el;
      var on = key.toUpperCase() === current;
      if (btn.classList.contains('is-active') !== on) btn.classList.toggle('is-active', on);
    });
  }

  // --- Item lines -----------------------------------------------------------
  // The held pack's items drawn as the same grouped lines as kits.pack_lines
  // ("J456781-02, 05, 06, 08 & 09") - not one chip per item - but with a ×
  // after every index, and after a bare series or a description, that
  // removes exactly that one item. They come from the state's `groups`,
  // which the server builds with the very grouping that makes `lines`, so
  // the two can never disagree. One entry per drawn line: a group with a
  // bare entry gives its own "J456781 ×" line before its indexes' line.
  function itemLineList(pack) {
    var out = [];
    if (!pack.groups) {
      // A server without `groups`: one removable line per item, as a fallback.
      (pack.items || []).forEach(function (item) {
        out.push({ key: 'i|' + item.id, series: item.label, bareItemId: item.id, indexes: [] });
      });
      return out;
    }
    pack.groups.forEach(function (g) {
      if (g.bareItemId !== null && g.bareItemId !== undefined) {
        out.push({ key: 'b|' + g.series, series: g.series, bareItemId: g.bareItemId, indexes: [] });
      }
      if (g.indexes && g.indexes.length) {
        out.push({ key: 'x|' + g.series, series: g.series, bareItemId: null, indexes: g.indexes });
      }
    });
    return out;
  }

  function lineKey(line) { return line.key; }

  function itemLineSig(line) {
    return JSON.stringify([line.series, line.bareItemId, line.indexes.map(function (i) { return [i.id, i.idx]; })]);
  }

  function buildItemLine(line, inEditor) {
    var div = document.createElement('div');
    div.className = 'pack-item-line';
    var series = document.createElement('span');
    series.className = 'item-series';
    if (!line.indexes.length) {
      series.textContent = line.series;
      div.appendChild(series);
      div.appendChild(buildItemX(line.bareItemId, line.series, inEditor));
      return div;
    }
    series.textContent = line.series + '-';
    div.appendChild(series);
    var last = line.indexes.length - 1;
    line.indexes.forEach(function (ix, i) {
      // Same separators as kits.join_and: "02", "02 & 03", "02, 03 & 04".
      if (i > 0) div.appendChild(document.createTextNode(i === last ? ' & ' : ', '));
      var token = document.createElement('span');
      token.className = 'item-token';
      var idx = document.createElement('span');
      idx.className = 'item-idx';
      idx.textContent = ix.idx;
      token.appendChild(idx);
      token.appendChild(buildItemX(ix.id, line.series + '-' + ix.idx, inEditor));
      div.appendChild(token);
    });
    return div;
  }

  function buildItemX(itemId, label, inEditor) {
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'item-x';
    btn.textContent = '×';
    btn.title = 'Remove ' + label;
    btn.setAttribute('aria-label', 'Remove ' + label);
    // In the editor, a typo can be removed without dropping the keyboard
    // mid-entry (the action still runs on click).
    if (inEditor) btn.addEventListener('mousedown', preventDefault);
    btn.addEventListener('click', function () { removeItem(itemId, label, btn, !inEditor); });
    return btn;
  }

  // The same updater runs after typing and after a series button fills the
  // field. Setting .value from script fires no input event.
  function updateAddNoIndex() {
    if (!seriesInput || !addNoIndexBtn) return;
    var s = seriesInput.value.trim();
    show(addNoIndexBtn, !!s);
    if (s) {
      addNoIndexBtn.title = 'Add "' + s + '" without an index';
      addNoIndexBtn.setAttribute('aria-label', addNoIndexBtn.title);
    } else {
      addNoIndexBtn.removeAttribute('title');
      addNoIndexBtn.removeAttribute('aria-label');
    }
    markSeriesButtons();
  }

  function updateCaptureEnabled() {
    var enabled = editorPackId !== null && heldPackId !== null && packTransitions === 0 && !leaving;
    if (cameraInput) cameraInput.disabled = !enabled;
    if (libraryInput) libraryInput.disabled = !enabled;
    [cameraLabel, libraryLabel].forEach(function (label) {
      if (label) label.classList.toggle('disabled', !enabled);
    });
  }

  // --- New pack / working / report / submit ---------------------------------
  function renderNewPack(st) {
    if (!newPackBar) return;
    show(newPackBar, editorPackId === null && !st.finalized);
    // The number goes in the small line under the button, so the button stays one line.
    setText(newPackNext, 'Next: Pack ' + st.nextPackNumber);
  }

  // --- Label printing -------------------------------------------------------
  // There is ONE #printControls. It is moved (only when its host changes,
  // which happens exactly when the editor opens or closes) next to whatever
  // the page's main action is right now: Save Pack while editing, + New Pack
  // when not, the bar above the report on a submitted kit.
  function renderPrintHost(st) {
    if (!printControls) return;
    var host = st.finalized ? printBarFinalized : (editorPackId !== null ? packActions : newPackBar);
    if (host && printControls.parentNode !== host) host.appendChild(printControls);
  }

  // Patches every print control in place from selectedPacks and `printing`
  // - checkbox ticks, disabled states, "Print (N)" - so ticking a box never
  // rebuilds a card, and a card rebuilt by a poll picks its tick back up.
  function syncPrintControls() {
    var st = state;
    if (!st) return;
    var packs = st.packs || [];
    var printable = {};
    var printableCount = 0;
    packs.forEach(function (p) {
      if (!p.isEmpty) { printable[p.id] = true; printableCount++; }
    });
    // A pack that has gone (deleted, emptied) drops out of the selection.
    Object.keys(selectedPacks).forEach(function (id) {
      if (!printable[id]) delete selectedPacks[id];
    });
    var count = Object.keys(selectedPacks).length;
    packs.forEach(function (p) {
      var entry = cardRegistry[String(p.id)];
      if (!entry) return;
      var ticked = !!selectedPacks[p.id];
      var box = entry.el.querySelector('.pack-select');
      if (box) {
        if (box.checked !== ticked) box.checked = ticked;
        if (box.disabled !== !!p.isEmpty) box.disabled = !!p.isEmpty;
      }
      var btn = entry.el.querySelector('.pack-print-btn');
      if (btn) {
        var busy = printing === 'pack:' + p.id;
        setText(btn, busy ? 'Printing…' : '🖨 Print');
        btn.disabled = !!p.isEmpty || printing !== null;
        btn.classList.toggle('is-busy', busy);
      }
      if (entry.el.classList.contains('is-selected') !== ticked) entry.el.classList.toggle('is-selected', ticked);
    });
    if (printSelectAll) {
      printSelectAll.disabled = printableCount === 0;
      printSelectAll.checked = printableCount > 0 && count === printableCount;
      printSelectAll.indeterminate = count > 0 && count < printableCount;
    }
    if (printSelectedBtn) {
      var bulkBusy = printing === 'bulk';
      setText(printSelectedBtn, bulkBusy ? 'Printing…' : '🖨 Print (' + count + ')');
      printSelectedBtn.disabled = count === 0 || printing !== null;
      printSelectedBtn.classList.toggle('is-busy', bulkBusy);
    }
  }

  function setSelected(packId, on) {
    if (on) selectedPacks[packId] = true;
    else delete selectedPacks[packId];
    syncPrintControls();
  }

  function heldByOthersQuestion(packs) {
    if (packs.length === 1) {
      return 'Pack ' + packs[0].number + ' is still being packed by ' + packs[0].reservedBy + ' - print it anyway?';
    }
    return joinAnd(packs.map(function (p) { return 'Pack ' + p.number + ' (' + p.reservedBy + ')'; })) +
      ' are still being packed - print them anyway?';
  }

  function previewUrlFor(ids) {
    return '/session/' + encodeURIComponent(SESSION_ID) + '/labels?packs=' + ids.join(',');
  }

  // Sends the packs to the label printer on the server - one spooler job
  // for the whole batch. Empty packs have nothing to print, so they're never
  // sent. A pack someone else still has open is printed only after asking,
  // since its label may not match the box once they're done with it.
  function printPacks(packIds, source) {
    if (printing !== null || !state || leaving) return;
    var packs = [];
    packIds.forEach(function (id) {
      var p = findPack(state, id);
      if (p && !p.isEmpty) packs.push(p);
    });
    if (!packs.length) {
      notify('Nothing to print - the selected packs are empty.');
      return;
    }
    packs.sort(function (a, b) { return a.number - b.number; });
    var others = packs.filter(function (p) { return p.reservedBy && !p.isMine; });
    if (others.length && !confirm(heldByOthersQuestion(others))) return;
    var ids = packs.map(function (p) { return p.id; });
    printing = source;
    syncPrintControls();
    mutate(PRINT_URL, { pack_ids: ids }).then(function (data) {
      printing = null;
      if (data.code === 'kit_missing') { kitGone(); return; }
      if (data.state) apply(data.state);
      if (data.ok) {
        ids.forEach(function (id) { delete selectedPacks[id]; });
        notify(data.message || 'Sent to the label printer.');
      } else if (data.code === 'printer_not_configured') {
        // Not an error the user can fix - the preview shows exactly what
        // would have printed, and prints from the phone's own browser.
        showNotice('No label printer is set up on the server yet.', {
          link: { href: data.previewUrl || previewUrlFor(ids), text: 'Open label preview' },
        });
      } else if (data.code === 'print_failed') {
        notify(data.error || 'The label printer didn\'t take the job.', { ms: ERROR_NOTICE_MS });
      } else {
        notify(data.error || 'Couldn\'t print those labels.', { ms: ERROR_NOTICE_MS });
      }
      syncPrintControls();
    }, function (err) {
      printing = null;
      syncPrintControls();
      notify(failMessage(err, 'print the labels'), { ms: ERROR_NOTICE_MS });
    });
  }

  function renderWorking(st) {
    if (!kitWorking) return;
    var list = st.working || [];
    var text = list.length ? 'Working now: ' + list.map(workingLabel).join(', ') : '';
    setText(kitWorking, text);
    show(kitWorking, !!text);
  }

  function reportSig(r) { return (r.complete ? '1|' : '0|') + r.text; }

  function buildReportLine(r) {
    var div = document.createElement('div');
    div.className = 'kit-report-line ' + (r.complete ? 'complete' : 'incomplete');
    div.textContent = r.text;
    return div;
  }

  function renderReport(st) {
    if (!kitReport) return;
    var report = st.report || [];
    var open = st.openPacks || [];
    syncKeyed(kitReportLines, report, reportRegistry, function (r) { return String(r.series); }, reportSig, buildReportLine);
    var openText = open.length
      ? 'Not counted yet: ' + open.map(function (p) { return 'Pack ' + p.number + ' (' + p.by + ')'; }).join(', ') + ' — still open.'
      : '';
    setText(kitReportOpen, openText);
    show(kitReportOpen, !!openText);
    show(kitReport, report.length > 0 || open.length > 0);
  }

  // Photos on THIS phone that the server doesn't have yet: still queued or
  // uploading, or failed and waiting for Retry/Discard. Pack photos already
  // hold up Final Submit through the pack's reservation; kit photos (no
  // pack) have no reservation, so without this a Final Submit landing first
  // would leave them out of the kit (and out of Drive) without a word.
  function uploadsBlockFinalText() {
    var failed = failedJobs.length;
    if (pendingUploads > 0) {
      return 'Wait for ' + pendingUploads + ' photo' + (pendingUploads === 1 ? '' : 's') +
        ' to finish uploading before Final Submit.';
    }
    if (failed > 0) {
      return failed + ' photo' + (failed === 1 ? '' : 's') + ' didn\'t upload - tap Retry (or \u00d7) on ' +
        (failed === 1 ? 'it' : 'them') + ' before Final Submit.';
    }
    return '';
  }

  function renderSubmit(st) {
    if (!finalSubmitBtn || !st) return;
    var blockedHere = st.canFinalize && !st.finalized ? uploadsBlockFinalText() : '';
    var can = !!st.canFinalize && !st.finalized && !blockedHere;
    show(finalSubmitBtn, can);
    setText(finalWaiting, can ? '' : (blockedHere || st.finalizeBlockedReason || ''));
    show(finalWaiting, !can);
  }

  // --- Pack cards -----------------------------------------------------------
  function packBadge(p) {
    if (p.isMine) return { text: '✏️ You\'re editing', cls: 'badge-mine' };
    if (!p.reservedBy) return null;
    var idle = idleShown(p.idleMinutes);
    if (idle) return { text: '💤 ' + p.reservedBy + ' idle ' + idle + ' min', cls: 'badge-warn' };
    return { text: '🔒 ' + p.reservedBy + ' editing', cls: 'badge-locked' };
  }

  // What a photo tile shows: the thumbnail - or, once this machine's copy
  // has been cleaned up (days after it reached Drive), an "In Drive" tile
  // like the gallery's. The thumbnail URL then redirects to the Drive file's
  // web page, which an <img> can't draw; the tile's link still opens it.
  function thumbContent(ph) {
    if (ph.localCopy === false) {
      var cleaned = document.createElement('div');
      cleaned.className = 'thumb-cleaned';
      cleaned.appendChild(document.createTextNode('☁️'));
      cleaned.appendChild(document.createElement('br'));
      cleaned.appendChild(document.createTextNode('In Drive'));
      return cleaned;
    }
    var img = document.createElement('img');
    img.src = ph.thumbUrl;
    img.loading = 'lazy';
    img.alt = '';
    return img;
  }

  function packCardSig(p) {
    return JSON.stringify([
      p.number, p.reservedBy || null, !!p.isMine, idleShown(p.idleMinutes), !!p.canTakeOver,
      p.contributors || [], p.lines || [],
      (p.photos || []).slice(0, 3).map(function (ph) { return ph.thumbUrl + (ph.localCopy === false ? '|drive' : ''); }),
      p.photoCount, !!p.isEmpty,
      p.printedAt || null, p.printedBy || null, p.printCount || 0,
    ]);
  }

  function printedTitle(p) {
    var times = p.printCount > 1 ? p.printCount + ' times, last ' : '';
    return 'Label printed ' + times + 'on ' + p.printedAt + (p.printedBy ? ' by ' + p.printedBy : '');
  }

  // A pack card holds its own controls now (tick for bulk print, 🖨 Print),
  // so it can't be one big <button> any more. The card is a div: its main
  // area is the keyboard-reachable button, and a tap anywhere on the card
  // opens the pack - except on the print controls in the right-hand column,
  // which keep their taps to themselves. Right column, top to bottom:
  // collaborators (far right), the tick + 🖨 Print, and when it was printed.
  function buildPackCard(p) {
    var card = document.createElement('div');
    card.className = 'consignment-card pack-card' + (p.isMine ? ' is-mine' : '') +
      (p.reservedBy && !p.isMine ? ' is-locked' : '') + (p.isEmpty ? ' is-empty' : '');
    card.setAttribute('data-pack-id', String(p.id));

    var main = document.createElement('div');
    main.className = 'pack-card-main';
    main.setAttribute('role', 'button');
    main.setAttribute('tabindex', '0');
    main.setAttribute('aria-label', 'Open Pack ' + p.number);
    var title = document.createElement('span');
    title.className = 'consignment-card-header pack-card-title';
    title.textContent = 'Pack ' + p.number;
    main.appendChild(title);

    var badge = packBadge(p);
    if (badge) {
      var b = document.createElement('span');
      b.className = 'badge pack-card-badge ' + badge.cls;
      b.textContent = badge.text;
      main.appendChild(b);
    }

    var lines = p.lines || [];
    if (lines.length) {
      var linesBox = document.createElement('div');
      linesBox.className = 'pack-lines';
      lines.forEach(function (line) {
        var div = document.createElement('div');
        div.className = 'pack-line';
        div.textContent = line;
        linesBox.appendChild(div);
      });
      main.appendChild(linesBox);
    } else {
      var none = document.createElement('div');
      none.className = 'consignment-card-subheader';
      none.textContent = 'No items yet';
      main.appendChild(none);
    }
    main.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar') {
        e.preventDefault(); // Space would otherwise scroll the page
        openPackDialog(p.id);
      }
    });
    card.appendChild(main);

    var side = document.createElement('div');
    side.className = 'pack-card-side';
    var collab = document.createElement('span');
    collab.className = 'pack-card-collab';
    collab.textContent = (p.contributors || []).join(', ');
    side.appendChild(collab);

    var printRow = document.createElement('div');
    printRow.className = 'pack-card-print';
    var tick = document.createElement('label');
    tick.className = 'pack-select-wrap';
    tick.title = 'Select Pack ' + p.number + ' to print';
    var box = document.createElement('input');
    box.type = 'checkbox';
    box.className = 'pack-select';
    box.setAttribute('aria-label', 'Select Pack ' + p.number);
    box.checked = !!selectedPacks[p.id];
    box.disabled = !!p.isEmpty;
    box.addEventListener('change', function () { setSelected(p.id, box.checked); });
    tick.appendChild(box);
    printRow.appendChild(tick);
    var printBtn = document.createElement('button');
    printBtn.type = 'button';
    printBtn.className = 'pack-print-btn';
    printBtn.textContent = '🖨 Print';
    printBtn.title = p.isEmpty ? 'Nothing to print yet' : 'Print the label for Pack ' + p.number;
    printBtn.disabled = !!p.isEmpty;
    printBtn.addEventListener('click', function () { printPacks([p.id], 'pack:' + p.id); });
    printRow.appendChild(printBtn);
    // The tick and 🖨 must never also open the pack (the label's own
    // synthetic click on the checkbox bubbles here too).
    printRow.addEventListener('click', function (e) { e.stopPropagation(); });
    side.appendChild(printRow);

    if (p.printedAt) {
      var printed = document.createElement('div');
      printed.className = 'pack-card-printed';
      printed.textContent = '🖨 printed ' + shortStamp(p.printedAt) + (p.printCount > 1 ? ' (×' + p.printCount + ')' : '');
      printed.title = printedTitle(p);
      side.appendChild(printed);
    }
    card.appendChild(side);

    var foot = document.createElement('div');
    foot.className = 'pack-card-foot';
    var photos = p.photos || [];
    var photoCount = typeof p.photoCount === 'number' ? p.photoCount : photos.length;
    if (photos.length) {
      var preview = document.createElement('div');
      preview.className = 'consignment-card-preview';
      photos.slice(0, 3).forEach(function (ph) {
        var thumb = document.createElement('div');
        thumb.className = 'thumb';
        thumb.appendChild(thumbContent(ph));
        preview.appendChild(thumb);
      });
      if (photoCount > 3) {
        var more = document.createElement('div');
        more.className = 'consignment-card-more';
        more.textContent = '+' + (photoCount - 3);
        preview.appendChild(more);
      }
      foot.appendChild(preview);
    }
    var count = document.createElement('div');
    count.className = 'consignment-card-count';
    count.textContent = photoCount ? photoCount + ' photo(s)' : 'No photos yet';
    foot.appendChild(count);
    card.appendChild(foot);

    card.addEventListener('click', function () { openPackDialog(p.id); });
    return card;
  }

  // --- Kit photos (not in any pack) ----------------------------------------
  // Pictures of the whole kit, a pallet, a delivery note - taken with this
  // section's own buttons while the kit is open. Hidden while this person
  // has a pack open (the editor's Take Photo is for that pack), back once
  // it's saved; uploads already going carry on meanwhile. Each can be
  // removed by whoever took it (or an admin) until Final Submit; the server
  // says which in `removable`.
  function renderKitPhotos(st) {
    if (!kitPhotos) return;
    var photos = st.generalPhotos || [];
    show(kitPhotos, st.finalized ? photos.length > 0 : editorPackId === null);
    if (kitPhotosCount) setText(kitPhotosCount, photos.length ? photos.length + ' photo(s)' : '');
    if (kitPhotosEmpty) show(kitPhotosEmpty, !photos.length && !st.finalized);
    if (kitCaptureBar) show(kitCaptureBar, !st.finalized);
    syncKeyed(kitPhotoStrip, photos, generalRegistry, idKey,
      function (ph) { return photoSig(ph, !!ph.removable) + '|' + (ph.by || ''); },
      function (ph) {
        var tile = buildPhotoTile(ph, !!ph.removable);
        if (ph.by) tile.title = 'Added by ' + ph.by;
        return tile;
      });
    var capture = !st.finalized && !leaving;
    [kitCameraInput, kitLibraryInput].forEach(function (input) { if (input) input.disabled = !capture; });
  }

  function handleGeneralFiles(fileList) {
    if (!state || state.finalized || leaving) return;
    Array.prototype.forEach.call(fileList, function (file) {
      queueUpload({ file: file, packId: null, general: true, tile: null, retryBtn: null });
    });
  }

  function renderPackList(st) {
    syncKeyed(packList, st.packs || [], cardRegistry, idKey, packCardSig, buildPackCard);
  }

  // --- Pack dialog ----------------------------------------------------------
  function photoSig(ph, removable) {
    return ph.thumbUrl + '|' + ph.fullUrl + (ph.locked ? '|locked' : '') + (removable ? '|x' : '') +
      (ph.localCopy === false ? '|drive' : '');
  }

  // A photo already submitted to Drive (a reopened kit's older photos) is
  // "locked": it can't be taken back, so it gets a ☁️ instead of a ×.
  function buildPhotoTile(ph, removable) {
    var a = document.createElement('a');
    a.href = ph.fullUrl;
    a.target = '_blank';
    a.rel = 'noopener';
    a.className = 'thumb' + (ph.locked ? ' is-locked' : '');
    a.setAttribute('data-id', String(ph.id));
    a.appendChild(thumbContent(ph));
    if (ph.locked) {
      var cloud = document.createElement('span');
      cloud.className = 'photo-locked';
      cloud.textContent = '☁️';
      cloud.title = 'Already in Drive';
      cloud.setAttribute('aria-label', 'Already in Drive');
      a.appendChild(cloud);
    } else if (removable) {
      var del = document.createElement('button');
      del.type = 'button';
      del.className = 'delete-btn';
      del.title = 'Remove photo';
      del.setAttribute('aria-label', 'Remove photo');
      del.textContent = '×';
      del.addEventListener('click', function (e) {
        e.preventDefault();
        e.stopPropagation();
        deletePhoto(ph.id, del);
      });
      a.appendChild(del);
    }
    return a;
  }

  function openPackDialog(packId) {
    if (!packDialog || !state || !findPack(state, packId)) return;
    openPackId = packId;
    // A different pack in the dialog starts from empty containers. After
    // that, render() only patches them.
    clearChildren(packDialogLines);
    dialogLinesSig = null;
    clearChildren(packDialogItemLines);
    dialogLineRegistry = {};
    clearChildren(packDialogGallery);
    dialogPhotoRegistry = {};
    showDialogNotice(packDialogNotice, '');
    renderPackDialog(state, true);
    openDialog(packDialog);
    if (packDialogBody) packDialogBody.scrollTop = 0;
  }

  function closePackDialog() {
    closeDialog(packDialog);
    openPackId = null;
  }

  function renderPackDialog(st, force) {
    if (!packDialog || openPackId === null) return;
    if (!force && !packDialog.open) return;
    var pack = findPack(st, openPackId);
    if (!pack) {
      closePackDialog();
      showNotice('That pack no longer exists.');
      return;
    }
    setText(packDialogTitle, 'Pack ' + pack.number);
    setText(packDialogCollab, (pack.contributors || []).join(', '));

    // Someone else's (or nobody's) pack: its plain lines. The viewer's own
    // pack: the same grouped lines as the editor, with a × per index.
    var removable = !!pack.isMine && !st.finalized;
    var grouped = removable && (pack.items || []).length > 0;
    var lines = pack.lines || [];
    var linesSig = JSON.stringify([grouped, lines]);
    if (linesSig !== dialogLinesSig) {
      dialogLinesSig = linesSig;
      clearChildren(packDialogLines);
      if (grouped) {
        // drawn by #packDialogItemLines below
      } else if (lines.length) {
        lines.forEach(function (line) {
          var div = document.createElement('div');
          div.className = 'pack-line';
          div.textContent = line;
          packDialogLines.appendChild(div);
        });
      } else {
        var none = document.createElement('div');
        none.className = 'kit-muted';
        none.textContent = 'No items yet';
        packDialogLines.appendChild(none);
      }
    }
    show(packDialogLines, !grouped);
    show(packDialogItemLines, grouped);
    syncKeyed(packDialogItemLines, grouped ? itemLineList(pack) : [], dialogLineRegistry, lineKey, itemLineSig,
      function (line) { return buildItemLine(line, false); });
    syncKeyed(packDialogGallery, pack.photos || [], dialogPhotoRegistry, idKey,
      function (ph) { return photoSig(ph, removable); },
      function (ph) { return buildPhotoTile(ph, removable); });

    var meta = [];
    if (pack.firstEdit) meta.push('First edit ' + pack.firstEdit);
    if (pack.lastEdit && pack.lastEdit !== pack.firstEdit) meta.push('Last edit ' + pack.lastEdit);
    setText(packDialogMeta, meta.join(' · '));
    show(packDialogMeta, meta.length > 0);

    renderDialogEdit(st, pack);
  }

  function renderDialogEdit(st, pack) {
    if (!packDialogEdit) return;
    if (st.finalized) {
      show(packDialogEdit, false);
      return;
    }
    show(packDialogEdit, true);
    if (isBusy(packDialogEdit)) return; // "Working…" until its own response settles
    var mode, label;
    if (pack.isMine) {
      mode = 'mine';
      label = 'Edit this pack';
    } else if (!pack.reservedBy) {
      mode = 'reserve';
      label = 'Edit this pack';
    } else if (pack.canTakeOver) {
      mode = 'takeover';
      label = 'Take over from ' + pack.reservedBy + ' (idle ' + pack.idleMinutes + ' min)';
    } else {
      mode = 'locked';
      label = '🔒 ' + pack.reservedBy + ' is editing this pack';
    }
    setText(packDialogEdit, label);
    packDialogEdit.disabled = mode === 'locked';
    packDialogEdit.setAttribute('data-mode', mode);
  }

  // --- Final dialog ---------------------------------------------------------
  function emptyPacksText(numbers) {
    if (numbers.length === 1) return 'Pack ' + numbers[0] + ' has no items or photos.';
    return 'Packs ' + joinAnd(numbers.map(String)) + ' have no items or photos.';
  }

  // Returns true when the report shown actually changed (not on first fill).
  function renderFinalDialogContent(st) {
    var report = st.report || [];
    var empty = st.emptyPacks || [];
    var sig = JSON.stringify([report.map(reportSig), empty, st.packCount, st.photoCount]);
    if (sig === finalReportSig) return false;
    var changed = finalReportSig !== null;
    finalReportSig = sig;
    clearChildren(finalDialogReport);
    var summary = document.createElement('div');
    summary.className = 'kit-muted';
    summary.textContent = st.packCount + ' pack(s) · ' + st.photoCount + ' photo(s)';
    finalDialogReport.appendChild(summary);
    if (report.length) {
      report.forEach(function (r) { finalDialogReport.appendChild(buildReportLine(r)); });
    } else {
      var none = document.createElement('div');
      none.className = 'kit-muted';
      none.textContent = 'No J-series items to report.';
      finalDialogReport.appendChild(none);
    }
    setText(finalDialogEmpty, empty.length ? emptyPacksText(empty) : '');
    show(finalDialogEmpty, empty.length > 0);
    // A reopened kit: "Yes" re-submits, which rewrites the Drive sheet.
    show(finalDialogResubmit, (st.submitCount || 0) > 0);
    return changed;
  }

  function renderFinalDialog(st) {
    if (!isOpen(finalDialog) || finalBusy) return;
    var blockedHere = uploadsBlockFinalText();
    if (!st.canFinalize || blockedHere) {
      // Someone opened a pack (or similar) while this was up, or a photo
      // from this phone failed. Close it and say why, rather than let "Yes"
      // run into a 409 (or leave a photo behind).
      closeDialog(finalDialog);
      showNotice((!st.canFinalize && st.finalizeBlockedReason) || blockedHere ||
        'Final Submit isn\'t available right now.');
      return;
    }
    if (renderFinalDialogContent(st)) {
      showDialogNotice(finalDialogNotice, 'The report just changed - check it again before answering.');
    }
    finalDialogRev = st.rev;
  }

  function openFinalDialog() {
    if (!finalDialog || !state || !state.canFinalize) return;
    var blockedHere = uploadsBlockFinalText();
    if (blockedHere) { notify(blockedHere); renderSubmit(state); return; }
    showDialogNotice(finalDialogNotice, '');
    finalReportSig = null;
    renderFinalDialogContent(state);
    finalDialogRev = state.rev;
    openDialog(finalDialog);
  }

  // --- Upload tiles ---------------------------------------------------------
  function updateRetryButtons() {
    var can = heldPackId !== null && packTransitions === 0 && !leaving;
    var canGeneral = !leaving && !!state && !state.finalized;
    failedJobs.forEach(function (job) {
      if (!job.retryBtn) return;
      if (job.general) {
        job.retryBtn.disabled = !canGeneral;
        job.retryBtn.title = 'Upload this kit photo again';
        return;
      }
      job.retryBtn.disabled = !can;
      job.retryBtn.title = can ? 'Upload this photo to the pack you\'re editing' : 'Open a pack first';
    });
  }

  // ==========================================================================
  // Polling
  // ==========================================================================
  function schedulePoll(delay) {
    if (pollingStopped) return;
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = setTimeout(poll, typeof delay === 'number' ? delay : POLL_MS);
  }

  function stopPolling() {
    pollingStopped = true;
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    abortPoll();
  }

  function abortPoll() {
    if (!pollToken) return;
    var token = pollToken;
    pollToken = null; // its callbacks see they've been superseded and do nothing
    if (token.ctrl) { try { token.ctrl.abort(); } catch (e) { /* already done */ } }
  }

  function poll() {
    pollTimer = null;
    if (pollingStopped || leaving) return;
    if (document.visibilityState === 'hidden') return; // visibilitychange restarts it
    if (mutationsInFlight > 0) { pollDeferred = true; return; }
    if (pollToken) return; // never overlap

    var token = { ctrl: newController() };
    pollToken = token;
    function finish() {
      if (pollToken !== token) return;
      pollToken = null;
      schedulePoll();
    }
    postJson('/api/kit/state', { session_id: SESSION_ID, holding_pack_id: heldPackId }, POLL_TIMEOUT_MS, token.ctrl)
      .then(function (data) {
        if (pollToken !== token) return;
        pollFailures = 0;
        setReconnecting(false);
        if (!data.ok && data.code === 'kit_missing') { kitGone(); return; }
        if (data.state) apply(data.state);
      }, function (err) {
        if (pollToken !== token || (err && err.kind === 'logged_out')) return;
        pollFailures++;
        if (pollFailures >= 2) setReconnecting(true);
      })
      .then(finish, function (err) {
        if (window.console) console.error('Kit poll failed:', err);
        finish();
      });
  }

  // ==========================================================================
  // Item entry
  // ==========================================================================
  function splitCombined(match) {
    seriesInput.value = match[1].toUpperCase();
    seriesJumpedFor = seriesInput.value;
    indexInput.value = match[2];
    updateAddNoIndex();
    indexInput.focus();
    try { indexInput.setSelectionRange(indexInput.value.length, indexInput.value.length); } catch (e) { /* not supported */ }
  }

  function onSeriesInput(e) {
    var trimmed = seriesInput.value.trim();
    // A pasted/scanned "J456789-02" is split into its two fields. The user
    // still taps Add, so nothing is added without a look.
    var combined = J_WITH_INDEX_RE.exec(trimmed);
    if (combined) {
      splitCombined(combined);
      return;
    }
    // A complete J + 6 digits auto-advances to the index. This runs inside
    // the keystroke, so the number pad takes over straight away. Only ONCE
    // per series typed: someone who comes back to the field to carry on
    // with a description ("J456789 window kit") isn't pulled away again on
    // the next keystroke, and deleting back to J + 6 digits doesn't jump.
    // Changing the series itself (deleting a digit) re-arms it.
    if (!/^[Jj]\d{6}/.test(trimmed)) seriesJumpedFor = '';
    var deleting = !!(e && e.inputType && e.inputType.indexOf('delete') === 0);
    if (J_SERIES_RE.test(trimmed) && !deleting && trimmed.toUpperCase() !== seriesJumpedFor) {
      seriesJumpedFor = trimmed.toUpperCase();
      // Only rewrite the value when it really differs (e.g. a lower-case
      // "j"). Replacing it mid-composition makes some Android keyboards
      // type the text twice.
      if (seriesInput.value !== trimmed.toUpperCase()) seriesInput.value = trimmed.toUpperCase();
      updateAddNoIndex();
      indexInput.focus();
      return;
    }
    updateAddNoIndex();
  }

  // The index is digits only (the server refuses anything else), so any
  // other character is dropped the moment it lands - typed, pasted or put
  // there by a keyboard's suggestion - with the caret kept where it was
  // relative to the digits around it. Not while an IME is still composing:
  // rewriting the value mid-composition makes some Android keyboards type
  // it twice. compositionend runs the same clean-up once it's done.
  function stripIndex() {
    if (!indexInput) return;
    var value = indexInput.value;
    if (!/\D/.test(value) && value.length <= INDEX_MAX_LEN) return;
    var caret = indexInput.selectionStart;
    var digitsBefore = typeof caret === 'number' ? value.slice(0, caret).replace(/\D+/g, '').length : null;
    var cleaned = value.replace(/\D+/g, '').slice(0, INDEX_MAX_LEN);
    indexInput.value = cleaned;
    if (digitsBefore !== null) {
      var at = Math.min(digitsBefore, cleaned.length);
      try { indexInput.setSelectionRange(at, at); } catch (e) { /* not supported */ }
    }
    // A leading "-" or a space ("-05" typed out of habit) is harmless -
    // only a real non-digit earns the hint.
    if (/[^\d\s\-–—]/.test(value)) setPackStatus('Index numbers can only contain digits.', 'hint');
  }

  function onIndexInput(e) {
    if (e && e.isComposing) return;
    stripIndex();
  }

  // A paste is handled whole, before the browser inserts it: a full item
  // number ("J456789-02") fills both fields; one number is inserted as its
  // digits; anything else ("02, 03", "J456789", a 7-digit number) is
  // refused with a hint rather than squeezed into a wrong-but-plausible
  // index that one tap on Add would store.
  function onIndexPaste(e) {
    var data = e.clipboardData || window.clipboardData;
    if (!data || !seriesInput) return; // no clipboard access - stripIndex cleans up after
    var text = String(data.getData('text') || '').trim();
    e.preventDefault();
    var combined = J_WITH_INDEX_RE.exec(text);
    if (combined) {
      splitCombined(combined);
      return;
    }
    var groups = text.match(/\d+/g) || [];
    var start = indexInput.selectionStart;
    var end = indexInput.selectionEnd;
    if (typeof start !== 'number') { start = end = indexInput.value.length; }
    var next = groups.length === 1
      ? indexInput.value.slice(0, start) + groups[0] + indexInput.value.slice(end)
      : null;
    if (next === null || next.length > INDEX_MAX_LEN) {
      setPackStatus('Paste one index number (digits only, at most ' + INDEX_MAX_LEN + ').', 'hint');
      return;
    }
    indexInput.value = next;
    var at = start + groups[0].length;
    try { indexInput.setSelectionRange(at, at); } catch (err) { /* not supported */ }
    if (/[^\d\s\-–—]/.test(text)) setPackStatus('Only the digits were kept.', 'hint');
  }

  function onSeriesEnter() {
    var trimmed = seriesInput.value.trim();
    var combined = J_WITH_INDEX_RE.exec(trimmed);
    if (combined) {
      splitCombined(combined);
      return;
    }
    if (!trimmed) return;
    if (J_SERIES_RE.test(trimmed)) {
      seriesInput.value = trimmed.toUpperCase();
      seriesJumpedFor = seriesInput.value;
      updateAddNoIndex();
    }
    indexInput.focus();
  }

  // Index Enter or the Add button. The field is read and cleared, and focus
  // stays in it, synchronously inside the tap. Continuous entry
  // (02, Add, 03, Add…) then never loses the number pad, and a second tap
  // on Add sees an empty field, so it is a no-op rather than a stray bare item.
  function submitIndex() {
    if (!indexInput || !seriesInput) return;
    stripIndex(); // Enter can land before a composition's clean-up
    var raw = indexInput.value;
    var idx = cleanIndex(raw);
    if (!idx && failedAdds.length) {
      // Add with an empty field re-sends the ones that failed.
      resendFailedAdds();
      return;
    }
    if (!idx) {
      setPackStatus('Type an index, or tap "Add as-is"', 'hint');
      return;
    }
    var series = seriesInput.value.trim();
    if (!series) {
      setPackStatus('Type the item number first.', 'error');
      seriesInput.focus();
      return;
    }
    indexInput.value = '';
    indexInput.focus();
    addItem(series, idx, raw);
  }

  // "Add as-is" adds the series or description with no index. It only ever
  // happens on an explicit tap. A free-text description is cleared after
  // it's added, ready for the next one. A J-series stays, because its
  // indexes usually follow.
  function addAsIs() {
    if (!seriesInput) return;
    var series = seriesInput.value.trim();
    if (!series) return;
    var combined = J_WITH_INDEX_RE.exec(series);
    if (combined) {
      splitCombined(combined);
      return;
    }
    var isJ = J_SERIES_RE.test(series);
    addItem(isJ ? series.toUpperCase() : series, '', null).then(function (ok) {
      if (ok && !isJ && seriesInput.value.trim() === series) {
        seriesInput.value = '';
        updateAddNoIndex();
      }
    });
  }

  // Resolves true once the item is in the pack (or was already), false
  // otherwise. On failure, the typed index is put back if the field is still
  // empty and the same pack is still open.
  function addItem(series, idx, restoreRaw) {
    var packId = heldPackId;
    if (packId === null) {
      // e.g. typed and hit Enter while Save Pack was still on its way (the
      // held pack reads null for the duration). submitIndex has already
      // cleared the field, so put the number back rather than lose it.
      if (restoreRaw && indexInput && editorPackId !== null && !indexInput.value) indexInput.value = restoreRaw;
      reportEntryError('You\'re not editing a pack right now.');
      return Promise.resolve(false);
    }
    addsInFlight++;
    return mutate('/api/kit/item/add', { pack_id: packId, series: series, idx: idx }).then(function (data) {
      if (handleResponse(data)) {
        if (data.added === false) {
          setPackStatus(data.message || ((data.label || series) + ' is already in this pack.'), 'warn');
        } else {
          setPackStatus((data.message || ('Added ' + (data.label || series))) + ' ✓', 'ok');
        }
        return true;
      }
      restoreIndex(packId, series, restoreRaw);
      reportEntryError(withFailedAdds(data.error || 'Couldn\'t add that item.'));
      return false;
    }, function (err) {
      restoreIndex(packId, series, restoreRaw);
      reportEntryError(withFailedAdds(failMessage(err, 'add that item')));
      return false;
    }).then(addSettled, function (err) {
      // Never let a bug above leave addsInFlight stuck. prepareToLeave
      // waits on it, so Save / New Pack / Home would hang for good.
      if (window.console) console.error('Item add handling failed:', err);
      return addSettled(false);
    });
  }

  function addSettled(ok) {
    addsInFlight = Math.max(0, addsInFlight - 1);
    if (addsInFlight === 0) {
      var waiters = addWaiters;
      addWaiters = [];
      waiters.forEach(function (resolve) { resolve(); });
    }
    return ok;
  }

  // A failed add's index goes back in the field if the field is empty and
  // the same pack is still open. When several adds fail together (quick
  // 02, Add, 03, Add... on a dropping hotspot) only one fits there, so the
  // rest are kept in failedAdds, named in the error and re-sent by the next
  // Add with an empty field, or before leaving the pack - never dropped.
  function restoreIndex(packId, series, raw) {
    if (!raw || !indexInput || editorPackId !== packId || heldPackId !== packId) return;
    var seriesNow = seriesInput ? seriesInput.value.trim() : '';
    if (!indexInput.value && seriesNow.toUpperCase() === String(series).toUpperCase()) {
      indexInput.value = raw;
      return;
    }
    var dup = failedAdds.some(function (f) { return f.series === series && f.raw === raw; });
    if (!dup) failedAdds.push({ series: series, raw: raw });
  }

  function withFailedAdds(message) {
    if (!failedAdds.length) return message;
    var names = failedAdds.map(function (f) {
      return J_SERIES_RE.test(f.series) ? f.series.toUpperCase() + '-' + cleanIndex(f.raw) : f.series + ' ' + f.raw;
    });
    return message.replace(/\.?$/, '.') + ' Not added yet: ' + names.join(', ') +
      ' - tap Add to try ' + (names.length === 1 ? 'it' : 'them') + ' again.';
  }

  // Resolves true when every failed add went in this time.
  function resendFailedAdds() {
    var queued = failedAdds;
    failedAdds = [];
    if (!queued.length) return Promise.resolve(true);
    return Promise.all(queued.map(function (f) {
      return addItem(f.series, cleanIndex(f.raw), f.raw);
    })).then(function (results) {
      return results.every(Boolean);
    });
  }

  function reportEntryError(message) {
    if (editorPackId !== null) setPackStatus(message, 'error');
    else notify(message);
  }

  function waitForAdds() {
    if (addsInFlight === 0) return Promise.resolve();
    return new Promise(function (resolve) { addWaiters.push(resolve); });
  }

  function removeItem(itemId, label, btn, fromDialog) {
    var packId = heldPackId;
    if (packId === null) return;
    btn.disabled = true;
    mutate('/api/kit/item/remove', { pack_id: packId, item_id: itemId }).then(function (data) {
      if (handleResponse(data)) {
        if (!fromDialog && editorPackId !== null) setPackStatus('Removed ' + label, 'ok');
        return;
      }
      btn.disabled = false;
      if (fromDialog || editorPackId === null) notify(data.error || 'Couldn\'t remove ' + label + '.');
      else setPackStatus(data.error || 'Couldn\'t remove ' + label + '.', 'error');
    }, function (err) {
      btn.disabled = false;
      if (fromDialog) notify(failMessage(err, 'remove ' + label));
      else setPackStatus(failMessage(err, 'remove ' + label), 'error');
    });
  }

  function deletePhoto(photoId, btn) {
    if (!confirm('Remove this photo?')) return;
    btn.disabled = true;
    mutate('/api/delete', { upload_id: Number(photoId) }).then(function (data) {
      if (handleResponse(data)) return;
      btn.disabled = false;
      notify(data.error || 'Couldn\'t remove the photo.');
    }, function (err) {
      btn.disabled = false;
      notify(failMessage(err, 'remove the photo'));
    });
  }

  // ==========================================================================
  // Pack lifecycle
  // ==========================================================================
  // Runs before anything that leaves the held pack: Save Pack, New Pack,
  // editing another pack, Home and Cancel. Pending uploads block it, since
  // they'd land after the release and be refused. In-flight adds are waited
  // out. Then an index that was typed but never added (the iOS number pad
  // has no return key, so "type, forget Add, tap Save" is common) is added
  // first. If that add fails, the move stops, so nothing is silently lost.
  function prepareToLeave(where, leavingPage) {
    var say = where === 'dialog'
      ? function (message) { showDialogNotice(packDialogNotice, message); }
      : notify;
    // Leaving the page (🏠 / Cancel) cuts off every upload; saving or
    // switching packs only matters to photos going into the pack being left,
    // so a kit photo (no pack) still uploading doesn't hold that up.
    var blocking = leavingPage ? pendingUploads : pendingPackUploads;
    if (blocking > 0) {
      say('Wait for ' + blocking + ' photo(s) to finish uploading first.');
      return Promise.resolve(false);
    }
    return waitForAdds().then(function () {
      if (heldPackId === null || editorPackId === null || !indexInput || !seriesInput) return true;
      return resendFailedAdds();
    }).then(function (resent) {
      if (!resent) {
        say((packStatus && packStatus.textContent) || 'Some index numbers didn\'t add - try again first.');
        return false;
      }
      if (heldPackId === null || editorPackId === null || !indexInput || !seriesInput) return true;
      var raw = indexInput.value;
      var idx = cleanIndex(raw);
      var series = seriesInput.value.trim();
      if (!idx || !series) return true;
      indexInput.value = '';
      return addItem(series, idx, raw).then(function (ok) {
        if (!ok) say((packStatus && packStatus.textContent) || 'Couldn\'t add the index you typed - fix or clear it first.');
        return ok;
      });
    });
  }

  function scrollToEditor() {
    if (!packEditor || packEditor.classList.contains('hidden')) return;
    try {
      packEditor.scrollIntoView({ block: 'start' });
    } catch (e) {
      packEditor.scrollIntoView(true);
    }
  }

  function onSavePack() {
    if (!savePackBtn || savePackBtn.disabled) return;
    setBusy(savePackBtn, true);
    prepareToLeave('page').then(function (ok) {
      if (!ok) { setBusy(savePackBtn, false); return; }
      var packId = heldPackId;
      var number = heldPackNumber;
      return packMove(RELEASE_URL, { pack_id: packId }).then(function (data) {
        setBusy(savePackBtn, false);
        if (!handleResponse(data, true)) {
          notify(data.error || 'Couldn\'t save the pack.');
          return;
        }
        if (!data.state) { schedulePoll(0); return; } // kit gone - the next poll says so
        var removed = packId !== null && !findPack(data.state, packId);
        showNotice(removed ? 'Pack ' + number + ' was empty, so it was removed.' : 'Pack ' + number + ' saved.');
      }, function (err) {
        setBusy(savePackBtn, false);
        notify(failMessage(err, 'save the pack'));
      });
    });
  }

  function onNewPack() {
    if (!newPackBtn || newPackBtn.disabled) return;
    setBusy(newPackBtn, true);
    prepareToLeave('page').then(function (ok) {
      if (!ok) { setBusy(newPackBtn, false); return; }
      return packMove('/api/kit/pack/new', {}).then(function (data) {
        setBusy(newPackBtn, false);
        if (!handleResponse(data, true)) {
          notify(data.error || 'Couldn\'t start a new pack.');
          return;
        }
        // No focus() here: after an await it wouldn't raise the keyboard on
        // iOS anyway. The editor is brought into view for a tap instead.
        scrollToEditor();
      }, function (err) {
        setBusy(newPackBtn, false);
        notify(failMessage(err, 'start a new pack'));
      });
    });
  }

  function onDialogEdit() {
    if (!packDialogEdit || packDialogEdit.disabled || openPackId === null || !state) return;
    var packId = openPackId;
    var pack = findPack(state, packId);
    if (!pack) return;
    var mode = packDialogEdit.getAttribute('data-mode');
    if (mode === 'mine' || pack.isMine) {
      closePackDialog();
      scrollToEditor();
      return;
    }
    if (mode === 'locked') return;
    var force = false;
    if (mode === 'takeover') {
      if (!confirm(pack.reservedBy + ' has been idle for ' + pack.idleMinutes + ' min on Pack ' + pack.number + '. Take it over?')) return;
      force = true;
    }
    setBusy(packDialogEdit, true);
    prepareToLeave('dialog').then(function (ok) {
      if (!ok) {
        setBusy(packDialogEdit, false);
        if (state) renderPackDialog(state);
        return;
      }
      return reserve(packId, force);
    });
  }

  function reserve(packId, force) {
    return packMove('/api/kit/pack/reserve', { pack_id: packId, force: !!force }).then(function (data) {
      if (data.ok) {
        setBusy(packDialogEdit, false);
        handleResponse(data, true);
        closePackDialog();
        scrollToEditor();
        return;
      }
      if (data.code === 'idle' && !force) {
        // The holder went idle long enough for a takeover. That only ever
        // happens deliberately, so ask first.
        handleResponse(data);
        var message = (data.error || ((data.holder || 'Someone') + ' has been idle on this pack.')) + ' Take it over?';
        if (confirm(message)) return reserve(packId, true);
        setBusy(packDialogEdit, false);
        if (state) renderPackDialog(state);
        return;
      }
      setBusy(packDialogEdit, false);
      handleResponse(data, true);
      if (leaving) return;
      if (data.code === 'pack_missing') {
        closePackDialog();
        showNotice(data.error || 'That pack no longer exists.');
        return;
      }
      if (isOpen(packDialog)) {
        showDialogNotice(packDialogNotice, data.error || 'Couldn\'t open that pack.');
        if (state) renderPackDialog(state);
      } else {
        notify(data.error || 'Couldn\'t open that pack.');
      }
    }, function (err) {
      setBusy(packDialogEdit, false);
      if (isOpen(packDialog)) {
        showDialogNotice(packDialogNotice, failMessage(err, 'open that pack'));
        if (state) renderPackDialog(state);
      } else {
        notify(failMessage(err, 'open that pack'));
      }
    });
  }

  // 🏠 and Cancel. If a pack is held, any typed index is added first, then
  // the pack is released with a keepalive fetch, then the page navigates.
  // It waits briefly for the release to land, so the page arrived at
  // doesn't still show "You still have Pack 3 open". It never waits long.
  function onLeaveLink(e) {
    var link = e.currentTarget;
    if (e.defaultPrevented || e.button > 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    var holding = heldPackId !== null || packTransitions > 0 || editorPackId !== null;
    if (!holding && pendingUploads === 0) return; // nothing to release - an ordinary navigation
    e.preventDefault();
    if (leaveInProgress) return;
    leaveInProgress = true;
    prepareToLeave('page', true).then(function (ok) {
      if (!ok) { leaveInProgress = false; return; }
      var packId = heldPackId;
      leaving = true;
      stopPolling();
      heldPackId = null;
      updateCaptureEnabled();
      var go = function () { location.href = link.href; };
      var waited = new Promise(function (resolve) { setTimeout(resolve, LEAVE_RELEASE_WAIT_MS); });
      Promise.race([sendKeepaliveRelease(packId), waited]).then(go, go);
    });
  }

  // ==========================================================================
  // Uploads
  // ==========================================================================
  // A client-side FIFO queue with at most MAX_PARALLEL_UPLOADS in flight.
  // Placeholders appear immediately. A failed upload (network, timeout, or
  // the pack no longer held) keeps its File on an error tile with Retry.
  // Photos taken with <input capture> aren't saved anywhere else, so
  // dropping a failed one would lose it for good.
  function handleFiles(fileList) {
    if (heldPackId === null || packTransitions > 0) {
      notify('Open a pack before adding photos.');
      return;
    }
    var packId = heldPackId;
    Array.prototype.forEach.call(fileList, function (file) {
      queueUpload({ file: file, packId: packId, tile: null, retryBtn: null });
    });
  }

  function queueUpload(job) {
    if (!job.tile) {
      job.tile = document.createElement('div');
      var strip = job.general ? kitUploadingStrip : uploadingStrip;
      if (strip) strip.appendChild(job.tile);
    }
    paintTileUploading(job);
    pendingUploads++;
    if (!job.general) pendingPackUploads++;
    updateUnloadGuard();
    uploadQueue.push(job);
    pumpUploads();
  }

  function pumpUploads() {
    while (uploadsActive < MAX_PARALLEL_UPLOADS && uploadQueue.length) sendUpload(uploadQueue.shift());
  }

  function sendUpload(job) {
    uploadsActive++;
    var fd = new FormData();
    fd.append('session_id', SESSION_ID);
    fd.append('file', job.file);
    // Explicit either way - the server never guesses "no pack" from a missing pack_id.
    if (job.general) fd.append('general', '1');
    else fd.append('pack_id', String(job.packId));
    function settle() {
      uploadsActive--;
      pendingUploads--;
      if (!job.general) pendingPackUploads--;
      updateUnloadGuard();
      pumpUploads();
    }
    request('/api/upload', { method: 'POST', body: fd }, UPLOAD_TIMEOUT_MS).then(function (data) {
      if (data.ok) {
        removeTile(job);
        if (data.state) apply(data.state);
        return;
      }
      if (data.code === 'kit_missing') { kitGone(); return; }
      if (data.state) apply(data.state);
      failUpload(job, data.error || 'Upload failed.');
    }, function (err) {
      if (err && err.kind === 'logged_out') return;
      var kind = err && err.kind;
      failUpload(job, kind === 'timeout' ? 'Upload timed out.'
        : kind === 'bad_response' ? err.message
          : 'Upload failed - check your connection.');
    }).then(settle, function (err) {
      if (window.console) console.error('Upload handling failed:', err);
      settle();
    });
  }

  function paintTileUploading(job) {
    var tile = job.tile;
    clearChildren(tile);
    tile.className = 'thumb uploading';
    tile.removeAttribute('title');
    var spin = document.createElement('span');
    spin.className = 'spinner';
    spin.textContent = '⏳';
    tile.appendChild(spin);
    job.retryBtn = null;
  }

  function failUpload(job, message) {
    if (leaving) return;
    var tile = job.tile;
    clearChildren(tile);
    tile.className = 'thumb error upload-failed';
    tile.title = message;
    var icon = document.createElement('span');
    icon.className = 'spinner';
    icon.textContent = '⚠️';
    tile.appendChild(icon);

    var retry = document.createElement('button');
    retry.type = 'button';
    retry.className = 'upload-retry';
    retry.textContent = 'Retry';
    retry.addEventListener('click', function () { retryUpload(job); });
    tile.appendChild(retry);
    job.retryBtn = retry;

    var discard = document.createElement('button');
    discard.type = 'button';
    discard.className = 'delete-btn';
    discard.textContent = '×';
    discard.title = 'Discard this photo';
    discard.setAttribute('aria-label', 'Discard this photo');
    discard.addEventListener('click', function () {
      if (!confirm('Discard this photo? It hasn\'t been uploaded.')) return;
      forgetFailed(job);
      removeTile(job);
    });
    tile.appendChild(discard);

    if (failedJobs.indexOf(job) === -1) failedJobs.push(job);
    updateRetryButtons();
    renderSubmit(state);
    notify('A photo didn\'t upload (' + message.replace(/\.$/, '') + '). Tap Retry on it' +
      (heldPackId === null && !job.general ? ' once you\'re editing a pack.' : '.'));
  }

  // A pack photo is re-sent to the pack held NOW. That may be a different
  // pack from the one it was taken for, e.g. after a takeover. That's the
  // user's call. A kit photo (no pack) just goes again as one.
  function retryUpload(job) {
    if (leaving) return;
    if (job.general) {
      if (!state || state.finalized) return;
      forgetFailed(job);
      queueUpload(job);
      return;
    }
    if (heldPackId === null || packTransitions > 0) return;
    forgetFailed(job);
    job.packId = heldPackId;
    queueUpload(job);
  }

  function forgetFailed(job) {
    var i = failedJobs.indexOf(job);
    if (i !== -1) failedJobs.splice(i, 1);
    renderSubmit(state);
  }

  function removeTile(job) {
    if (job.tile && job.tile.parentNode) job.tile.parentNode.removeChild(job.tile);
  }

  function onBeforeUnload(e) {
    if (suppressUnloadPrompt || pendingUploads === 0) return undefined;
    e.preventDefault();
    e.returnValue = '';
    return '';
  }

  // Registered only while something is uploading. A standing beforeunload
  // listener makes some browsers (Firefox) skip the back/forward cache,
  // which the pagehide release below relies on.
  function updateUnloadGuard() {
    renderSubmit(state); // the upload count also gates Final Submit
    var want = pendingUploads > 0;
    if (want && !unloadGuardOn) {
      window.addEventListener('beforeunload', onBeforeUnload);
      unloadGuardOn = true;
    } else if (!want && unloadGuardOn) {
      window.removeEventListener('beforeunload', onBeforeUnload);
      unloadGuardOn = false;
    }
  }

  // ==========================================================================
  // Wiring
  // ==========================================================================
  if (seriesInput) {
    seriesInput.addEventListener('input', onSeriesInput);
    seriesInput.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter' && e.keyCode !== 13) return;
      if (e.isComposing) return;
      e.preventDefault();
      onSeriesEnter();
    });
  }
  if (indexInput) {
    indexInput.addEventListener('input', onIndexInput);
    indexInput.addEventListener('compositionend', stripIndex);
    indexInput.addEventListener('paste', onIndexPaste);
    indexInput.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter' && e.keyCode !== 13) return;
      if (e.isComposing) return;
      e.preventDefault();
      submitIndex();
    });
  }
  [indexAddBtn, addNoIndexBtn].forEach(function (btn) {
    // Keep focus (and the keyboard) in the field being typed in. The action
    // runs on click. touchstart is never prevented, so scrolling still works.
    if (btn) btn.addEventListener('mousedown', preventDefault);
  });
  if (indexAddBtn) indexAddBtn.addEventListener('click', submitIndex);
  if (addNoIndexBtn) addNoIndexBtn.addEventListener('click', addAsIs);
  if (savePackBtn) savePackBtn.addEventListener('click', onSavePack);
  if (kitRenameBtn) kitRenameBtn.addEventListener('click', openRename);
  if (kitRenameForm) kitRenameForm.addEventListener('submit', submitRename);
  if (kitRenameCancel) kitRenameCancel.addEventListener('click', closeRename);
  [kitRenameInput, kitJobInput].forEach(function (input) {
    if (!input) return;
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeRename();
    });
  });
  if (kitJobInput && kitRenameInput) {
    // "next" on the phone keyboard (Enter) goes on to the kit name.
    kitJobInput.addEventListener('keydown', function (e) {
      if ((e.key === 'Enter' || e.keyCode === 13) && !e.isComposing) {
        e.preventDefault();
        kitRenameInput.focus();
      }
    });
  }
  if (newPackBtn) newPackBtn.addEventListener('click', onNewPack);

  // Bulk print. "Select all" ticks every pack that has something in it (an
  // empty one has nothing to print); from a partial selection (the
  // indeterminate dash) a tap selects all, from all a tap clears.
  if (printSelectAll) {
    printSelectAll.addEventListener('change', function () {
      if (!state) return;
      if (printSelectAll.checked) {
        (state.packs || []).forEach(function (p) { if (!p.isEmpty) selectedPacks[p.id] = true; });
      } else {
        selectedPacks = {};
      }
      syncPrintControls();
    });
  }
  if (printSelectedBtn) {
    printSelectedBtn.addEventListener('click', function () { printPacks(selectedIds(), 'bulk'); });
  }

  if (reopenedDismiss) {
    reopenedDismiss.addEventListener('click', function () {
      reopenedDismissed = true;
      show(reopenedBanner, false);
    });
  }
  // Reopening changes the kit for everyone (it's back in Active Sessions,
  // and its Drive sheet is out of date until the next Final Submit), so a
  // stray tap asks first.
  if (reopenForm) {
    reopenForm.addEventListener('submit', function (e) {
      var name = (state && state.kitName) || 'this kit';
      if (!confirm('Reopen "' + name + '" to make changes? Photos already in Drive stay there - press Final Submit again when you\'re done to update the Drive sheet.')) {
        e.preventDefault();
        return;
      }
      if (reopenBtn) {
        reopenBtn.disabled = true; // a second tap on a slow hotspot would post it twice
        reopenBtn.textContent = 'Reopening…';
      }
      leaving = true;
      suppressUnloadPrompt = true;
    });
  }

  // Keep a showing notice above the keyboard as it opens and closes.
  if (window.visualViewport) {
    var onViewport = function () {
      if (kitNotice && !kitNotice.classList.contains('hidden')) positionNotice();
    };
    window.visualViewport.addEventListener('resize', onViewport);
    window.visualViewport.addEventListener('scroll', onViewport);
  }

  [cameraInput, libraryInput].forEach(function (input) {
    if (!input) return;
    input.addEventListener('change', function () {
      if (this.files && this.files.length) handleFiles(this.files);
      this.value = ''; // allow the same shot/file again immediately
    });
  });
  // The "Kit photos" section's own buttons - photos that go into no pack.
  [kitCameraInput, kitLibraryInput].forEach(function (input) {
    if (!input) return;
    input.addEventListener('change', function () {
      if (this.files && this.files.length) handleGeneralFiles(this.files);
      this.value = '';
    });
  });

  if (packDialog) {
    if (packDialogClose) packDialogClose.addEventListener('click', closePackDialog);
    // A tap on the backdrop targets the <dialog> itself, never its content.
    packDialog.addEventListener('click', function (e) {
      if (e.target === packDialog) closePackDialog();
    });
    packDialog.addEventListener('close', function () {
      openPackId = null;
      showDialogNotice(packDialogNotice, '');
    });
  }
  if (packDialogEdit) packDialogEdit.addEventListener('click', onDialogEdit);

  if (finalSubmitBtn) finalSubmitBtn.addEventListener('click', openFinalDialog);
  if (finalDialog) {
    var closeFinal = function () { if (!finalBusy) closeDialog(finalDialog); };
    if (finalNoBtn) finalNoBtn.addEventListener('click', closeFinal);
    if (finalDialogClose) finalDialogClose.addEventListener('click', closeFinal);
    finalDialog.addEventListener('click', function (e) {
      if (e.target === finalDialog) closeFinal();
    });
    finalDialog.addEventListener('cancel', function (e) {
      if (finalBusy) e.preventDefault(); // Escape mid-submit - let the answer arrive first
    });
    finalDialog.addEventListener('close', function () {
      showDialogNotice(finalDialogNotice, '');
      finalReportSig = null;
    });
  }
  if (finalYesBtn) {
    finalYesBtn.addEventListener('click', function () {
      if (finalBusy || !state) return;
      var blockedHere = uploadsBlockFinalText();
      if (blockedHere) { showDialogNotice(finalDialogNotice, blockedHere); return; }
      finalBusy = true;
      setBusy(finalYesBtn, true);
      if (finalNoBtn) finalNoBtn.disabled = true;
      if (finalDialogClose) finalDialogClose.disabled = true;
      showDialogNotice(finalDialogNotice, '');
      // Sends the rev the dialog showed. The server finalizes only if
      // nothing has changed since, so "Yes" always answers for what was
      // actually on screen.
      mutate('/api/kit/finalize', { confirm: 'yes', rev: finalDialogRev }).then(function (data) {
        if (data.ok) {
          goTo(data.redirect || '/');
          return;
        }
        finalBusy = false;
        setBusy(finalYesBtn, false);
        if (finalNoBtn) finalNoBtn.disabled = false;
        if (finalDialogClose) finalDialogClose.disabled = false;
        if (data.code === 'kit_missing') { kitGone(); return; }
        // "changed" re-renders the dialog with the fresh report (and its
        // rev) for the user to answer again. reserved/empty/not_collaborator
        // make canFinalize false, which closes it with the reason.
        if (data.state) apply(data.state, true);
        if (isOpen(finalDialog)) showDialogNotice(finalDialogNotice, data.error || 'Couldn\'t submit - try again.');
      }, function (err) {
        finalBusy = false;
        setBusy(finalYesBtn, false);
        if (finalNoBtn) finalNoBtn.disabled = false;
        if (finalDialogClose) finalDialogClose.disabled = false;
        showDialogNotice(finalDialogNotice, failMessage(err, 'submit'));
      });
    });
  }

  [homeLink, cancelLink].forEach(function (link) {
    if (link) link.addEventListener('click', onLeaveLink);
  });
  if (deleteForm) {
    deleteForm.addEventListener('submit', function (e) {
      if (e.defaultPrevented) return; // the confirm() was declined
      // Deleting the kit - no "This kit was deleted." alert racing the
      // navigation, and nothing left to release.
      leaving = true;
      suppressUnloadPrompt = true;
      stopPolling();
    });
  }
  // Touch tracking. A node replaced between touchstart and touchend drops
  // the tap on iOS, or lands it on whatever took its place on Android.
  // Renders wait until the touch ends (+300ms). Passive listeners only:
  // scrolling is never blocked.
  var passive = { passive: true, capture: true };
  document.addEventListener('touchstart', function () {
    touchActive = true;
    if (touchTimer) { clearTimeout(touchTimer); touchTimer = null; }
  }, passive);
  function touchEnded(e) {
    if (e.touches && e.touches.length) return; // another finger still down
    if (touchTimer) clearTimeout(touchTimer);
    touchTimer = setTimeout(function () {
      touchTimer = null;
      touchActive = false;
      if (renderPending) render();
    }, TOUCH_SETTLE_MS);
  }
  document.addEventListener('touchend', touchEnded, passive);
  document.addEventListener('touchcancel', touchEnded, passive);

  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState !== 'visible') {
      touchActive = false;
      return;
    }
    // Back from the camera app or a locked screen. Any poll still in flight
    // may be one iOS froze. Drop it and ask for fresh state now.
    if (pollingStopped || leaving) return;
    abortPoll();
    if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
    poll();
  });

  // Browser Back / swipe-back puts the page into the back/forward cache
  // (persisted === true). That is a real "moved out of the pack", so release
  // via sendBeacon, which survives the page going away. A reload or
  // pull-to-refresh has persisted === false and keeps the pack (the reloaded
  // page picks it straight back up). Switching to the camera app fires no
  // pagehide at all.
  window.addEventListener('pagehide', function (e) {
    if (!e.persisted || leaving) return;
    if (heldPackId === null && packTransitions === 0) return;
    var body = JSON.stringify({ session_id: SESSION_ID, pack_id: heldPackId });
    try {
      if (navigator.sendBeacon) navigator.sendBeacon(RELEASE_URL, new Blob([body], { type: 'application/json' }));
      else sendKeepaliveRelease(heldPackId);
    } catch (err) { /* best effort - the index page's [Release] is the fallback */ }
  });
  // Coming back to a page restored from that cache: its state is stale and
  // its pack was released. Reload for the truth. The server hands back any
  // pack still held.
  window.addEventListener('pageshow', function (e) {
    if (e.persisted) location.reload();
  });

  // ==========================================================================
  // Start
  // ==========================================================================
  apply(initialState, true);
  // Zero-tap start (like the consignment page): arriving with a pack already
  // held (e.g. Pack 1 auto-created by /start) puts the cursor straight into
  // the Item number field, where the browser allows it.
  if (heldPackId !== null && editorPackId !== null && seriesInput) seriesInput.focus();
  // Arriving straight from a reopen (?reopened=1): no pack is opened for
  // anyone on a reopen, so say what to do next - unless the server already
  // put up its own message. The flag comes off the address, so a reload
  // doesn't say it again.
  if (parseJson(pageData.reopenedNotice, false) && !initialFinalized) {
    showNotice('Kit reopened - tap a pack to change it, or start a new one.');
  }
  if (/[?&]reopened=1(?:&|$)/.test(location.search) && window.history && history.replaceState) {
    try { history.replaceState(history.state, '', location.pathname + location.hash); } catch (e) { /* cosmetic only */ }
  }
  // A submitted kit is read-only and final, so there is nothing to poll for.
  // pollingStopped also stops visibilitychange from starting a poll.
  if (initialFinalized) pollingStopped = true;
  else schedulePoll();
})();
