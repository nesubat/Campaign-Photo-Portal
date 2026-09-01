(function () {
  var gallery = document.getElementById('gallery');
  var counter = document.getElementById('counter');
  var cameraInput = document.getElementById('cameraInput');
  var libraryInput = document.getElementById('libraryInput');
  var submitBtn = document.getElementById('submitBtn');

  var consignmentLogging = !!window.CONSIGNMENT_LOGGING;
  var finalized = !!window.FINALIZED;

  // Assigned inside whichever mode block below runs; kept as plain vars
  // (not globals) so both modes share the same submit-button wiring at the
  // bottom without duplicating it.
  var uploadOne = function () {};
  var getBusyCount = function () { return 0; };
  var getTotalCount = function () { return 0; };

  function setCaptureEnabled(enabled) {
    if (cameraInput) cameraInput.disabled = !enabled;
    if (libraryInput) libraryInput.disabled = !enabled;
    ['cameraLabel', 'libraryLabel'].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.classList.toggle('disabled', !enabled);
    });
  }

  function handleFiles(fileList) {
    Array.prototype.forEach.call(fileList, function (file) { uploadOne(file); });
  }

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
    var pendingUploads = 0;

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
        sub.textContent = 'Item ID(s): ' + section.itemIds.join(', ');
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

    uploadOne = function (file) {
      var placeholder = document.createElement('div');
      placeholder.className = 'thumb uploading';
      placeholder.innerHTML = '<span class="spinner">⏳</span>';
      if (uploadingStrip) uploadingStrip.appendChild(placeholder);
      pendingUploads++;

      var fd = new FormData();
      fd.append('session_id', window.SESSION_ID);
      fd.append('file', file);
      if (activeConsignmentId) fd.append('consignment_id', activeConsignmentId);

      fetch('/api/upload', { method: 'POST', body: fd })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (!data.ok) throw new Error(data.error || 'Upload failed');
          pendingUploads--;
          placeholder.remove();
          var section = sections.get(activeConsignmentId);
          if (section) {
            section.photos.unshift({ id: data.id, thumbUrl: data.thumbUrl, fullUrl: data.fullUrl });
            renderSections();
            if (dialog && dialog.open && openDialogConsignmentId === activeConsignmentId) {
              renderDialogGallery(section);
            }
          }
        })
        .catch(function (err) {
          pendingUploads--;
          placeholder.classList.remove('uploading');
          placeholder.classList.add('error');
          placeholder.innerHTML = '<span class="spinner">⚠️</span>';
          placeholder.title = err.message;
          console.error('Upload failed:', err);
        });
    };

    getBusyCount = function () { return pendingUploads; };
    getTotalCount = totalPhotoCount;
  }

  // ==========================================================================
  // Flat mode: no consignment logging for this session - one plain grid,
  // unchanged from the original behavior.
  // ==========================================================================
  if (!consignmentLogging) {
    function updateCounterFlat() {
      counter.textContent = gallery.querySelectorAll('.thumb').length + ' photo(s)';
    }

    function addPlaceholder() {
      var tile = document.createElement('div');
      tile.className = 'thumb uploading';
      tile.innerHTML = '<span class="spinner">⏳</span>';
      gallery.prepend(tile);
      updateCounterFlat();
      return tile;
    }

    uploadOne = function (file) {
      var tile = addPlaceholder();
      var fd = new FormData();
      fd.append('session_id', window.SESSION_ID);
      fd.append('file', file);

      fetch('/api/upload', { method: 'POST', body: fd })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (!data.ok) throw new Error(data.error || 'Upload failed');
          tile.classList.remove('uploading');
          tile.outerHTML =
            '<a href="' + data.fullUrl + '" target="_blank" class="thumb" data-id="' + data.id + '">' +
            '<img src="' + data.thumbUrl + '" loading="lazy">' +
            '<button type="button" class="delete-btn" data-id="' + data.id + '" title="Remove photo">×</button>' +
            '</a>';
        })
        .catch(function (err) {
          tile.classList.remove('uploading');
          tile.classList.add('error');
          tile.innerHTML = '<span class="spinner">⚠️</span>';
          tile.title = err.message;
          console.error('Upload failed:', err);
        });
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
          updateCounterFlat();
        })
        .catch(function (err) { alert('Could not remove photo: ' + err.message); });
    }

    if (gallery) {
      gallery.addEventListener('click', function (e) {
        var btn = e.target.closest('.delete-btn');
        if (!btn) return;
        e.preventDefault();
        e.stopPropagation();
        var tile = btn.closest('.thumb');
        if (!confirm('Remove this photo?')) return;
        deleteOne(btn.dataset.id, tile);
      });
    }

    getBusyCount = function () { return gallery.querySelectorAll('.thumb.uploading').length; };
    getTotalCount = function () { return gallery.querySelectorAll('.thumb').length; };
  }

  // ==========================================================================
  // Shared: Submit
  // ==========================================================================
  if (submitBtn) {
    submitBtn.addEventListener('click', function () {
      var busy = getBusyCount();
      if (busy > 0) {
        alert('Still uploading ' + busy + ' photo(s) - wait for them to finish first.');
        return;
      }
      var total = getTotalCount();
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
