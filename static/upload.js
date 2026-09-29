(function () {
  var gallery = document.getElementById('gallery');
  var counter = document.getElementById('counter');
  var cameraInput = document.getElementById('cameraInput');
  var libraryInput = document.getElementById('libraryInput');
  var submitBtn = document.getElementById('submitBtn');
  var uploadStatus = document.getElementById('uploadStatus');

  var consignmentLogging = !!window.CONSIGNMENT_LOGGING;
  var finalized = !!window.FINALIZED;

  // Filled in by whichever mode block below runs (grouped or flat): where a
  // photo's tile goes while it uploads, what happens once it's saved, and
  // what extra fields the upload carries. The upload queue itself is shared.
  var uploadHooks = {
    placeTile: function () {},
    onSaved: function () {},
    addFields: function () {},
    countSaved: function () { return 0; },
  };

  function setCaptureEnabled(enabled) {
    if (cameraInput) cameraInput.disabled = !enabled;
    if (libraryInput) libraryInput.disabled = !enabled;
    ['cameraLabel', 'libraryLabel'].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.classList.toggle('disabled', !enabled);
    });
  }

  // ==========================================================================
  // Upload queue (shared by both modes)
  //
  // A pick of 50-100 photos can take 15-20 minutes to send over a busy
  // hotspot, and anything that cuts the phone off in that time - the Wi-Fi
  // dropping, walking out of range, the portal PC restarting - used to fail
  // every photo still waiting, for good, with nothing saying which ones. So:
  //   * at most MAX_PARALLEL_UPLOADS go at once; the rest wait here, in order;
  //   * a connection problem (no answer, a dropped or stalled connection, a
  //     5xx) never fails a photo: it goes back to the front of the queue and
  //     the WHOLE queue pauses and tries again after 2 s, 5 s, 10 s, 20 s,
  //     30 s, then every 60 s - for as long as the page stays open - or at
  //     once when the phone says it's back online;
  //   * only a clear refusal from the portal (e.g. "This batch was already
  //     submitted") fails a photo; its tile then shows that photo, with Retry
  //     and x, so it's obvious which one it was;
  //   * a stalled upload is spotted by its progress stopping (STALL_MS), not
  //     by a fixed time limit, so a big photo on a slow link is never cut
  //     off while it's still moving;
  //   * Submit waits for the queue, and leaving the page asks first.
  // ==========================================================================
  var MAX_PARALLEL_UPLOADS = 2;
  var STALL_MS = 60000;          // no upload progress for this long: the connection is gone
  var ANSWER_WAIT_MS = 90000;    // the photo is fully sent: how long to wait for the portal's answer
  var RETRY_DELAYS_S = [2, 5, 10, 20, 30, 60];

  var queue = [];                // jobs waiting to be sent; the front goes next
  var active = 0;                // jobs being sent right now
  var failedJobs = [];           // refused by the portal - waiting for Retry or x
  var connFailures = 0;          // connection problems in a row (reset by any success)
  var pausedUntil = 0;           // no new sends before this time (ms)
  var pauseTimer = null;
  var loggedOut = false;         // the portal answered with its login page: wait for "Try again"
  var batchTotal = 0;            // photos picked since the queue was last empty...
  var batchDone = 0;             // ...and how many of them are saved
  var unloadGuardOn = false;

  function pendingCount() { return queue.length + active; }

  function handleFiles(fileList) {
    Array.prototype.forEach.call(fileList, function (file) { addToQueue(file); });
    pump();
  }

  function addToQueue(file) {
    if (pendingCount() === 0 && failedJobs.length === 0) { batchTotal = 0; batchDone = 0; }
    var job = { file: file, tile: document.createElement('div'), previewUrl: null, fields: {} };
    uploadHooks.addFields(job);    // e.g. the consignment it was picked for - fixed NOW, not when it's sent
    uploadHooks.placeTile(job);
    paintQueued(job);
    queue.push(job);
    batchTotal++;
    refresh();
  }

  function pump() {
    if (loggedOut) { refresh(); return; }
    var wait = pausedUntil - Date.now();
    if (wait > 0) {
      if (!pauseTimer) pauseTimer = setTimeout(function () { pauseTimer = null; pump(); }, wait);
      refresh();
      return;
    }
    while (active < MAX_PARALLEL_UPLOADS && queue.length) sendJob(queue.shift());
    refresh();
  }

  function resumeNow() {
    pausedUntil = 0;
    if (pauseTimer) { clearTimeout(pauseTimer); pauseTimer = null; }
    pump();
  }

  function sendJob(job) {
    active++;
    paintSending(job, 0);
    var xhr = new XMLHttpRequest();
    var fd = new FormData();
    fd.append('session_id', window.SESSION_ID);
    fd.append('file', job.file);
    Object.keys(job.fields).forEach(function (k) { fd.append(k, job.fields[k]); });

    var lastProgress = Date.now();
    var sent = false;
    var done = false;
    var watchdog = setInterval(function () {
      // A hidden page's timers and network can be frozen by the phone -
      // that's not a stalled connection, so don't hold it against the upload.
      if (document.hidden) { lastProgress = Date.now(); return; }
      var idle = Date.now() - lastProgress;
      if ((!sent && idle > STALL_MS) || (sent && idle > ANSWER_WAIT_MS)) {
        finish('connection', null);
        try { xhr.abort(); } catch (e) { /* already gone */ }
      }
    }, 5000);

    function finish(kind, data) {
      if (done) return;
      done = true;
      clearInterval(watchdog);
      active--;
      if (kind === 'saved') {
        connFailures = 0;
        batchDone++;
        forgetPreview(job);
        uploadHooks.onSaved(job, data);
      } else if (kind === 'refused') {
        connFailures = 0;
        failedJobs.push(job);
        paintFailed(job, (data && data.error) || 'The portal refused this photo.');
      } else {
        // 'connection' or 'logged_out': the photo goes back to the front of
        // the queue, untouched, and the queue waits.
        queue.unshift(job);
        paintQueued(job, kind === 'logged_out' ? 'Waiting for login' : 'Waiting to retry');
        if (kind === 'logged_out') {
          loggedOut = true;
        } else if (Date.now() >= pausedUntil) {
          // Only the first failure of an outage lengthens the wait - the
          // other photo that was in flight at the same moment doesn't.
          var delay = RETRY_DELAYS_S[Math.min(connFailures, RETRY_DELAYS_S.length - 1)];
          connFailures++;
          pausedUntil = Date.now() + delay * 1000;
        }
      }
      // Next turn, not now: this request (still inside its own load/error
      // event) finishes closing first, so there are never more than
      // MAX_PARALLEL_UPLOADS connections open at once.
      setTimeout(pump, 0);
    }

    xhr.upload.onprogress = function (e) {
      lastProgress = Date.now();
      if (e.lengthComputable && e.total) paintSending(job, e.loaded / e.total);
    };
    xhr.upload.onload = function () { sent = true; lastProgress = Date.now(); paintSending(job, 1); };
    xhr.onload = function () {
      // A login that ran out: the portal redirects to its login page, which
      // the browser follows - the photo isn't refused, it just has to wait.
      if (/\/login(?:[?#]|$)/.test(xhr.responseURL || '')) { finish('logged_out', null); return; }
      var data = null;
      try { data = JSON.parse(xhr.responseText); } catch (e) { data = null; }
      if (!data || xhr.status >= 500 || xhr.status === 0) { finish('connection', null); return; }
      finish(data.ok ? 'saved' : 'refused', data);
    };
    xhr.onerror = function () { finish('connection', null); };
    xhr.onabort = function () { finish('connection', null); };
    xhr.open('POST', '/api/upload');
    xhr.send(fd);
  }

  // --- Tiles ------------------------------------------------------------------
  function paintQueued(job, label) {
    var tile = job.tile;
    tile.className = 'thumb uploading';
    tile.removeAttribute('title');
    tile.innerHTML = '';
    var spin = document.createElement('span');
    spin.className = 'spinner';
    spin.textContent = label ? '↻' : '⏳';
    tile.appendChild(spin);
    if (label) {
      var note = document.createElement('span');
      note.className = 'upload-progress';
      note.textContent = label;
      tile.appendChild(note);
      tile.title = label;
    }
  }

  function paintSending(job, fraction) {
    var tile = job.tile;
    var note = tile.querySelector('.upload-progress');
    if (!tile.classList.contains('sending')) {
      tile.className = 'thumb uploading sending';
      tile.removeAttribute('title');
      tile.innerHTML = '';
      var spin = document.createElement('span');
      spin.className = 'spinner';
      spin.textContent = '⏳';
      tile.appendChild(spin);
      note = document.createElement('span');
      note.className = 'upload-progress';
      tile.appendChild(note);
    }
    note.textContent = Math.round(fraction * 100) + '%';
  }

  function paintFailed(job, message) {
    var tile = job.tile;
    tile.className = 'thumb error upload-failed';
    tile.title = message;
    tile.innerHTML = '';
    // The photo itself, so it's clear WHICH one didn't make it.
    try {
      if (!job.previewUrl && window.URL && URL.createObjectURL) job.previewUrl = URL.createObjectURL(job.file);
    } catch (e) { job.previewUrl = null; }
    if (job.previewUrl) {
      var img = document.createElement('img');
      img.src = job.previewUrl;
      img.alt = '';
      img.decoding = 'async';
      tile.appendChild(img);
    }
    var icon = document.createElement('span');
    icon.className = 'spinner';
    icon.textContent = '⚠️';
    tile.appendChild(icon);

    var retry = document.createElement('button');
    retry.type = 'button';
    retry.className = 'upload-retry';
    retry.textContent = 'Retry';
    retry.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      dropFailed(job);
      paintQueued(job);
      queue.push(job);
      resumeNow();
    });
    tile.appendChild(retry);

    var discard = document.createElement('button');
    discard.type = 'button';
    discard.className = 'delete-btn upload-discard';
    discard.textContent = '×';
    discard.title = 'Leave this photo out';
    discard.setAttribute('aria-label', 'Leave this photo out');
    discard.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      if (!confirm('Leave this photo out? It hasn\'t been uploaded.')) return;
      dropFailed(job);
      forgetPreview(job);
      if (job.tile.parentNode) job.tile.parentNode.removeChild(job.tile);
      batchTotal = Math.max(batchDone, batchTotal - 1);
      refresh();
    });
    tile.appendChild(discard);
  }

  function dropFailed(job) {
    var i = failedJobs.indexOf(job);
    if (i !== -1) failedJobs.splice(i, 1);
  }

  function forgetPreview(job) {
    if (job.previewUrl) {
      try { URL.revokeObjectURL(job.previewUrl); } catch (e) { /* ignore */ }
      job.previewUrl = null;
    }
  }

  // --- The status line above the photos -----------------------------------------
  function statusButton(label, onClick, href) {
    var el = document.createElement(href ? 'a' : 'button');
    el.className = 'upload-status-btn';
    el.textContent = label;
    if (href) {
      el.href = href;
      el.target = '_blank';
      el.rel = 'noopener';
    } else {
      el.type = 'button';
      el.addEventListener('click', onClick);
    }
    return el;
  }

  function refresh() {
    updateUnloadGuard();
    if (counter) counter.textContent = uploadHooks.countSaved() + ' photo(s)';
    if (!uploadStatus) return;
    var pending = pendingCount();
    var failed = failedJobs.length;
    uploadStatus.innerHTML = '';
    uploadStatus.className = 'upload-status';
    var text = document.createElement('span');
    uploadStatus.appendChild(text);
    if (loggedOut && pending) {
      uploadStatus.classList.add('warn');
      text.textContent = 'You\'ve been logged out, so ' + pending + ' photo(s) are waiting - nothing is lost. ' +
        'Log in again in a new tab, then come back and tap Try again.';
      uploadStatus.appendChild(statusButton('Log in', null, '/login'));
      uploadStatus.appendChild(statusButton('Try again', function () { loggedOut = false; resumeNow(); }));
    } else if (pending && pausedUntil > Date.now()) {
      uploadStatus.classList.add('warn');
      var secs = Math.max(1, Math.round((pausedUntil - Date.now()) / 1000));
      text.textContent = 'Connection problem - ' + pending + ' photo(s) waiting, nothing is lost. ' +
        'Trying again in ' + secs + ' s. Keep this page open.';
      uploadStatus.appendChild(statusButton('Try now', resumeNow));
      if (!refresh.tick) refresh.tick = setTimeout(function () { refresh.tick = null; refresh(); }, 1000);
    } else if (pending) {
      text.textContent = 'Uploading - ' + batchDone + ' of ' + batchTotal + ' photo(s) saved. ' +
        'Keep this page open until it finishes.';
    } else if (failed) {
      uploadStatus.classList.add('warn');
      text.textContent = failed + ' photo(s) didn\'t upload - tap Retry on them, or × to leave them out.';
    } else {
      uploadStatus.classList.add('hidden');
    }
  }

  function onBeforeUnload(e) {
    e.preventDefault();
    e.returnValue = '';
    return '';
  }

  // Only registered while something is waiting: a standing beforeunload
  // listener keeps some browsers from using the back/forward cache.
  function updateUnloadGuard() {
    var want = pendingCount() > 0 || failedJobs.length > 0;
    if (want && !unloadGuardOn) {
      window.addEventListener('beforeunload', onBeforeUnload);
      unloadGuardOn = true;
    } else if (!want && unloadGuardOn) {
      window.removeEventListener('beforeunload', onBeforeUnload);
      unloadGuardOn = false;
    }
  }

  window.addEventListener('online', function () { if (queue.length) resumeNow(); });
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible' && queue.length && !loggedOut) resumeNow();
  });

  if (cameraInput) {
    cameraInput.addEventListener('change', function () {
      if (this.files && this.files.length) handleFiles(this.files);
      this.value = ''; // allow shooting the same scene again immediately
    });
  }
  if (libraryInput) {
    libraryInput.addEventListener('change', function () {
      if (this.files && this.files.length) handleFiles(this.files);
      this.value = '';
    });
  }

  // ==========================================================================
  // Grouped mode: photos organized into tappable per-consignment cards, each
  // opening a popup (native <dialog> - built into iOS Safari 15.4+ and
  // Android Chrome, so backdrop/centering/focus all work consistently on
  // both without hand-rolled overlay code) with a bigger view and delete
  // controls for both photos and Item IDs.
  // ==========================================================================
  if (consignmentLogging) {
    var keyValueInput = document.getElementById('keyValueInput');
    var keyValueSuggestions = document.getElementById('keyValueSuggestions');
    var CONSIGNMENT_VALUES = window.CONSIGNMENT_VALUES || [];
    var keyValueGo = document.getElementById('keyValueGo');
    var consignmentStatus = document.getElementById('consignmentStatus');
    var itemIdRow = document.getElementById('itemIdRow');
    var itemIdInput = document.getElementById('itemIdInput');
    var itemIdAdd = document.getElementById('itemIdAdd');
    var itemIdChips = document.getElementById('itemIdChips');
    var uploadingStrip = document.getElementById('uploadingStrip');
    var dialog = document.getElementById('sectionDialog');
    var dialogTitle = document.getElementById('sectionDialogTitle');
    var dialogClose = document.getElementById('sectionDialogClose');
    var dialogChips = document.getElementById('sectionDialogChips');
    var dialogGallery = document.getElementById('sectionDialogGallery');

    // consignmentId -> {consignmentId, keyValue, itemIds: [...], photos: [{id, thumbUrl, fullUrl}, ...]}
    var sections = new Map();
    var sectionOrder = []; // consignmentId list, most-recently-active first
    var activeConsignmentId = null; // which section new photos get tagged to
    var resolvedValue = null;
    var openDialogConsignmentId = null; // which section the popup is currently showing, if any

    function totalPhotoCount() {
      var n = 0;
      sections.forEach(function (s) { n += s.photos.length; });
      return n;
    }

    function updateCounter() {
      counter.textContent = totalPhotoCount() + ' photo(s)';
    }

    function buildPhotoTile(p, cid) {
      var a = document.createElement('a');
      a.href = p.fullUrl;
      a.target = '_blank';
      a.className = 'thumb';
      a.dataset.id = p.id;

      var img = document.createElement('img');
      img.src = p.thumbUrl;
      img.loading = 'lazy';
      a.appendChild(img);

      if (!finalized) {
        var del = document.createElement('button');
        del.type = 'button';
        del.className = 'delete-btn';
        del.title = 'Remove photo';
        del.textContent = '×';
        del.addEventListener('click', function (e) {
          e.preventDefault();
          e.stopPropagation();
          if (!confirm('Remove this photo?')) return;
          deleteSectionPhoto(cid, p.id);
        });
        a.appendChild(del);
      }
      return a;
    }

    function buildChip(cid, item) {
      var chip = document.createElement('span');
      chip.className = 'chip';

      // A count-1 chip just gets a single delete (x); once the same Item ID
      // has been scanned more than once, +/- step buttons flank the label
      // instead so a stray extra scan can be corrected without retyping.
      if (item.count > 1) {
        var minus = document.createElement('button');
        minus.type = 'button';
        minus.className = 'chip-step';
        minus.textContent = '−';
        minus.title = 'Remove one scan of ' + item.value;
        minus.addEventListener('click', function () { stepItemId(cid, item.value, -1); });
        chip.appendChild(minus);
      }

      var label = document.createElement('span');
      label.textContent = item.label;
      chip.appendChild(label);

      if (item.count > 1) {
        var plus = document.createElement('button');
        plus.type = 'button';
        plus.className = 'chip-step';
        plus.textContent = '+';
        plus.title = 'Add another scan of ' + item.value;
        plus.addEventListener('click', function () { stepItemId(cid, item.value, 1); });
        chip.appendChild(plus);
      } else {
        var removeBtn = document.createElement('button');
        removeBtn.type = 'button';
        removeBtn.className = 'chip-remove';
        removeBtn.textContent = '×';
        removeBtn.title = 'Remove ' + item.value;
        removeBtn.addEventListener('click', function () { stepItemId(cid, item.value, -1); });
        chip.appendChild(removeBtn);
      }

      return chip;
    }

    function renderInputChips() {
      if (!itemIdChips) return;
      itemIdChips.innerHTML = '';
      var section = activeConsignmentId ? sections.get(activeConsignmentId) : null;
      (section ? section.itemIds : []).forEach(function (item) {
        itemIdChips.appendChild(buildChip(activeConsignmentId, item));
      });
    }

    function renderDialogChips(section) {
      if (!dialogChips) return;
      dialogChips.innerHTML = '';
      section.itemIds.forEach(function (item) {
        dialogChips.appendChild(buildChip(section.consignmentId, item));
      });
    }

    function renderDialogGallery(section) {
      if (!dialogGallery) return;
      dialogGallery.innerHTML = '';
      section.photos.forEach(function (p) {
        dialogGallery.appendChild(buildPhotoTile(p, section.consignmentId));
      });
    }

    function buildCard(section) {
      var card = document.createElement('button');
      card.type = 'button';
      card.className = 'consignment-card';
      card.dataset.consignmentId = section.consignmentId;

      var header = document.createElement('div');
      header.className = 'consignment-card-header';
      header.textContent = section.keyValue;
      card.appendChild(header);

      if (section.itemIds.length) {
        var sub = document.createElement('div');
        sub.className = 'consignment-card-subheader';
        sub.textContent = 'Item ID(s): ' + section.itemIds.map(function (item) { return item.label; }).join(', ');
        card.appendChild(sub);
      }

      if (section.photos.length) {
        var preview = document.createElement('div');
        preview.className = 'consignment-card-preview';
        section.photos.slice(0, 3).forEach(function (p) {
          var thumb = document.createElement('div');
          thumb.className = 'thumb';
          var img = document.createElement('img');
          img.src = p.thumbUrl;
          img.loading = 'lazy';
          thumb.appendChild(img);
          preview.appendChild(thumb);
        });
        if (section.photos.length > 3) {
          var more = document.createElement('div');
          more.className = 'consignment-card-more';
          more.textContent = '+' + (section.photos.length - 3);
          preview.appendChild(more);
        }
        card.appendChild(preview);
      }

      var count = document.createElement('div');
      count.className = 'consignment-card-count';
      count.textContent = section.photos.length
        ? section.photos.length + ' photo(s)'
        : 'No photos yet';
      card.appendChild(count);

      card.addEventListener('click', function () { openDialog(section.consignmentId); });
      return card;
    }

    function renderSections() {
      if (!gallery) return;
      gallery.innerHTML = '';
      sectionOrder.forEach(function (cid) {
        var section = sections.get(cid);
        if (section) gallery.appendChild(buildCard(section));
      });
      updateCounter();
    }

    function openDialog(cid) {
      var section = sections.get(cid);
      if (!section || !dialog) return;
      openDialogConsignmentId = cid;
      dialogTitle.textContent = section.keyValue;
      renderDialogChips(section);
      renderDialogGallery(section);
      if (typeof dialog.showModal === 'function') {
        dialog.showModal();
      } else {
        dialog.setAttribute('open', ''); // very old browsers only
      }
    }

    if (dialog) {
      if (dialogClose) dialogClose.addEventListener('click', function () { dialog.close(); });
      // Tap on the backdrop (the click target is the <dialog> itself, never
      // its content, when a modal dialog's backdrop is tapped) closes it.
      dialog.addEventListener('click', function (e) {
        if (e.target === dialog) dialog.close();
      });
      dialog.addEventListener('close', function () { openDialogConsignmentId = null; });
    }

    function stepItemId(cid, rawValue, delta) {
      var url = delta > 0 ? '/api/consignment/item' : '/api/consignment/item/decrement';
      fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: window.SESSION_ID, consignment_id: cid, item_id: rawValue }),
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (!data.ok) throw new Error(data.error || 'Could not update Item ID.');
          var section = sections.get(cid);
          if (section) {
            section.itemIds = data.item_ids;
            renderSections();
            if (dialog && dialog.open && openDialogConsignmentId === cid) renderDialogChips(section);
          }
          if (cid === activeConsignmentId) renderInputChips();
        })
        .catch(function (err) { alert(err.message); });
    }

    function deleteSectionPhoto(cid, photoId) {
      fetch('/api/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: window.SESSION_ID, upload_id: Number(photoId) }),
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (!data.ok) throw new Error(data.error || 'Delete failed');
          var section = sections.get(cid);
          if (section) {
            section.photos = section.photos.filter(function (p) { return p.id !== photoId; });
            if (section.photos.length === 0) {
              // Nothing left to show for this consignment in this session -
              // drop the card entirely rather than leave an empty one.
              sections.delete(cid);
              sectionOrder = sectionOrder.filter(function (id) { return id !== cid; });
              if (dialog && dialog.open && openDialogConsignmentId === cid) dialog.close();
            } else if (dialog && dialog.open && openDialogConsignmentId === cid) {
              renderDialogGallery(section);
            }
          }
          renderSections();
        })
        .catch(function (err) { alert('Could not remove photo: ' + err.message); });
    }

    function invalidateConsignment() {
      activeConsignmentId = null;
      resolvedValue = null;
      consignmentStatus.classList.add('hidden');
      itemIdRow.classList.add('hidden');
      renderInputChips();
      setCaptureEnabled(false);
    }

    // Barcode/RF-gun scanners just "type" characters wherever the cursor is -
    // they never clear a field first. Selecting the existing text on focus
    // means the next keystroke (scanned or manually typed) overwrites the
    // selection instead of landing appended after it.
    function selectOnFocus(el) {
      if (el) el.addEventListener('focus', function () { el.select(); });
    }
    selectOnFocus(keyValueInput);
    selectOnFocus(itemIdInput);

    function hideSuggestions() {
      if (!keyValueSuggestions) return;
      keyValueSuggestions.classList.add('hidden');
      keyValueSuggestions.innerHTML = '';
    }

    function updateSuggestions() {
      if (!keyValueSuggestions) return;
      var typed = keyValueInput.value.trim().toLowerCase();
      if (!typed) { hideSuggestions(); return; }
      var matches = CONSIGNMENT_VALUES.filter(function (v) {
        return v.toLowerCase() !== typed && v.toLowerCase().indexOf(typed) !== -1;
      }).slice(0, 8);
      if (!matches.length) { hideSuggestions(); return; }

      keyValueSuggestions.innerHTML = '';
      matches.forEach(function (value) {
        var item = document.createElement('div');
        item.className = 'autocomplete-item';
        item.textContent = value;
        // mousedown (not click) fires before the input would blur, so
        // preventDefault here keeps focus in the input and lets us read/set
        // its value immediately - a click handler would arrive too late,
        // after blur already hid this dropdown.
        item.addEventListener('mousedown', function (e) {
          e.preventDefault();
          keyValueInput.value = value;
          hideSuggestions();
          resolveConsignment();
        });
        keyValueSuggestions.appendChild(item);
      });
      keyValueSuggestions.classList.remove('hidden');
    }

    function resolveConsignment() {
      var value = keyValueInput.value.trim();
      if (!value) return;
      hideSuggestions();

      fetch('/api/consignment/resolve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: window.SESSION_ID, key_type: 'consignment', key_value: value }),
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (!data.ok) throw new Error(data.error || 'Could not resolve consignment.');
          activeConsignmentId = data.consignment_id;
          resolvedValue = value;
          if (CONSIGNMENT_VALUES.indexOf(data.key_value) === -1) {
            CONSIGNMENT_VALUES.unshift(data.key_value); // available for autocomplete immediately, not just after reload
          }

          var existing = sections.get(data.consignment_id);
          sections.set(data.consignment_id, {
            consignmentId: data.consignment_id,
            keyValue: data.key_value,
            itemIds: data.item_ids,
            photos: existing ? existing.photos : [],
          });
          // Move (or insert) this section to the front - re-scanning an
          // earlier consignment brings its card back to the top, pushing
          // whatever was active before it back down.
          sectionOrder = sectionOrder.filter(function (id) { return id !== data.consignment_id; });
          sectionOrder.unshift(data.consignment_id);
          renderSections();
          renderInputChips();

          consignmentStatus.textContent = data.existing
            ? 'Updating existing proofs for ' + data.key_value + ' - ' + data.photo_count + ' photo(s) already logged.'
            : 'New record for ' + data.key_value + '.';
          consignmentStatus.classList.remove('hidden');
          itemIdRow.classList.remove('hidden');
          setCaptureEnabled(true);
          // Hand off focus to Item ID so the next scan (an item, not another
          // consignment) lands correctly with zero taps in between.
          if (itemIdInput) itemIdInput.focus();
        })
        .catch(function (err) { alert(err.message); });
    }

    function addItemId() {
      var value = itemIdInput.value.trim();
      if (!value || !activeConsignmentId) return;
      fetch('/api/consignment/item', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: window.SESSION_ID, consignment_id: activeConsignmentId, item_id: value,
        }),
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (!data.ok) throw new Error(data.error || 'Could not add Item ID.');
          itemIdInput.value = '';
          itemIdInput.focus(); // ready for the next item ID scan, no tap needed
          var section = sections.get(activeConsignmentId);
          if (section) {
            section.itemIds = data.item_ids;
            renderSections();
          }
          renderInputChips();
        })
        .catch(function (err) { alert(err.message); });
    }

    if (keyValueGo) keyValueGo.addEventListener('click', resolveConsignment);
    if (keyValueInput) {
      keyValueInput.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') {
          e.preventDefault();
          resolveConsignment();
        } else if (e.key === 'Escape') {
          hideSuggestions();
        }
      });
      // Editing the value after it's resolved (or scanning a new one without
      // hitting Enter) must not leave photos tagged to the stale consignment.
      keyValueInput.addEventListener('input', function () {
        if (activeConsignmentId && keyValueInput.value.trim() !== resolvedValue) {
          invalidateConsignment();
        }
        updateSuggestions();
      });
      keyValueInput.addEventListener('blur', hideSuggestions);
    }
    if (itemIdAdd) itemIdAdd.addEventListener('click', addItemId);
    if (itemIdInput) {
      itemIdInput.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') {
          e.preventDefault();
          addItemId();
        }
      });
    }

    setCaptureEnabled(false);
    // Zero-tap start: the very first scan of the session can go straight in.
    if (keyValueInput) keyValueInput.focus();

    // Bootstrap from server-rendered initial state (a reload mid-session, or
    // resuming a job, already has grouped/ordered sections to show).
    (window.INITIAL_SECTIONS || []).forEach(function (s) {
      sections.set(s.consignmentId, s);
      sectionOrder.push(s.consignmentId);
    });
    renderSections();

    uploadHooks = {
      // Tagged to the consignment that was active when the photo was PICKED -
      // scanning the next consignment while a batch is still uploading must
      // not move the rest of it there.
      addFields: function (job) {
        if (activeConsignmentId) {
          job.fields.consignment_id = activeConsignmentId;
          var s = sections.get(activeConsignmentId);
          job.keyValue = s ? s.keyValue : '';
        }
      },
      placeTile: function (job) { if (uploadingStrip) uploadingStrip.appendChild(job.tile); },
      onSaved: function (job, data) {
        if (job.tile.parentNode) job.tile.parentNode.removeChild(job.tile);
        var cid = job.fields.consignment_id;
        var section = sections.get(cid);
        if (!section && cid) {
          // Its card was emptied (last photo removed) while this one was on its way.
          section = { consignmentId: cid, keyValue: job.keyValue || '', itemIds: [], photos: [] };
          sections.set(cid, section);
          sectionOrder.unshift(cid);
        }
        if (section) {
          section.photos.unshift({ id: data.id, thumbUrl: data.thumbUrl, fullUrl: data.fullUrl });
          renderSections();
          if (dialog && dialog.open && openDialogConsignmentId === cid) renderDialogGallery(section);
        }
      },
      countSaved: totalPhotoCount,
    };
  }

  // ==========================================================================
  // Flat mode: no consignment logging for this session - one plain grid,
  // unchanged from the original behavior.
  // ==========================================================================
  if (!consignmentLogging) {
    function savedFlatCount() {
      return gallery ? gallery.querySelectorAll('.thumb:not(.uploading):not(.upload-failed)').length : 0;
    }

    uploadHooks = {
      addFields: function () {},
      placeTile: function (job) { if (gallery) gallery.prepend(job.tile); },
      onSaved: function (job, data) {
        var a = document.createElement('a');
        a.href = data.fullUrl;
        a.target = '_blank';
        a.className = 'thumb';
        a.dataset.id = data.id;
        var img = document.createElement('img');
        img.src = data.thumbUrl;
        img.loading = 'lazy';
        a.appendChild(img);
        var del = document.createElement('button');
        del.type = 'button';
        del.className = 'delete-btn';
        del.dataset.id = data.id;
        del.title = 'Remove photo';
        del.textContent = '×';
        a.appendChild(del);
        if (job.tile.parentNode) job.tile.parentNode.replaceChild(a, job.tile);
      },
      countSaved: savedFlatCount,
    };

    function deleteOne(id, tile) {
      fetch('/api/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: window.SESSION_ID, upload_id: Number(id) }),
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (!data.ok) throw new Error(data.error || 'Delete failed');
          tile.remove();
          refresh();
        })
        .catch(function (err) { alert('Could not remove photo: ' + err.message); });
    }

    if (gallery) {
      gallery.addEventListener('click', function (e) {
        var btn = e.target.closest('.delete-btn');
        if (!btn || btn.classList.contains('upload-discard')) return;
        e.preventDefault();
        e.stopPropagation();
        var tile = btn.closest('.thumb');
        if (!confirm('Remove this photo?')) return;
        deleteOne(btn.dataset.id, tile);
      });
    }
  }

  refresh();

  // ==========================================================================
  // Shared: Submit
  // ==========================================================================
  if (submitBtn) {
    submitBtn.addEventListener('click', function () {
      var busy = pendingCount();
      if (busy > 0) {
        alert('Still uploading ' + busy + ' photo(s) - wait for them to finish first (keep this page open).');
        return;
      }
      if (failedJobs.length) {
        alert(failedJobs.length + ' photo(s) didn\'t upload - tap Retry on them, or × to leave them out, then Submit.');
        return;
      }
      var total = uploadHooks.countSaved();
      if (total === 0) {
        alert('No photos uploaded yet.');
        return;
      }
      submitBtn.disabled = true;
      submitBtn.textContent = 'Submitting...';
      fetch('/api/finalize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: window.SESSION_ID }),
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (!data.ok) throw new Error(data.error || 'Submit failed');
          window.location.href = '/?submitted=' + data.count + '&job=' + encodeURIComponent(window.JOB_NUMBER);
        })
        .catch(function (err) {
          alert('Submit failed: ' + err.message);
          submitBtn.disabled = false;
          submitBtn.textContent = 'Submit Batch';
        });
    });
  }
})();
