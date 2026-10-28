// Scratchpad: type anything, see it as the label printer will print it, and
// print copies - optionally numbered ("Box 3 of 10"). The label is a list of
// texts (Text 1, Text 2, ... added with "+ Add text") plus the optional
// number; each has its own alignment, position and size, set in a popup from
// its Aa button. New texts are centred, in the middle, Auto size (as big as
// fits - smaller as more is typed). The preview is the very image the printer
// gets (POST /api/scratchpad/preview); Print sends every copy as one job
// (POST /api/scratchpad/print). See labels.scratch_*.
(function () {
  var root = document.getElementById('scratchpad');
  if (!root) return;
  var LIMITS = JSON.parse(root.getAttribute('data-limits'));
  var HAS_PRINTER = root.getAttribute('data-has-printer') === '1';
  var PREVIEW_URL = '/api/scratchpad/preview';
  var PRINT_URL = '/api/scratchpad/print';
  var STORAGE_KEY = 'scratchpad.v2';
  var PREVIEW_DELAY_MS = 250;
  var CONFIRM_COPIES_OVER = 20;
  var DEFAULT_SET_SIZE = 48;  // "Set size" with no Auto size measured yet
  // What − / + step through, like a word processor's size list.
  var SIZE_LADDER = [8, 9, 10, 11, 12, 14, 16, 18, 20, 24, 28, 32, 36, 40, 44, 48, 54, 60, 66, 72, 80, 90,
                     100, 110, 120, 140, 160, 180, 200, 220, 240, 260, 280, 300];
  var ALIGN_NAMES = { left: 'Left', center: 'Center', right: 'Right' };
  var VALIGN_NAMES = { top: 'Top', middle: 'Middle', bottom: 'Bottom' };

  function newField() { return { value: '', align: 'center', valign: 'middle', sizePt: 'auto' }; }
  function newNumbering() {
    return { enabled: false, prefix: '', format: 'of_total', align: 'center', valign: 'bottom', sizePt: 28 };
  }

  var state = { fields: [newField()], copies: 1, numbering: newNumbering() };
  var lastSizes = null;   // what the last preview says each field prints at
  var editing = null;     // the field object the format popup is changing

  function byId(id) { return document.getElementById(id); }
  var fieldsEl = byId('spFields');
  var fieldTemplate = byId('spFieldTemplate');
  var addFieldBtn = byId('spAddField');
  var copiesEl = byId('spCopies');
  var numberEl = byId('spNumber');
  var numberOptions = byId('spNumberOptions');
  var numberSummary = byId('spNumberSummary');
  var prefixEl = byId('spPrefix');
  var previewImg = byId('spPreviewImg');
  var previewEmpty = byId('spPreviewEmpty');
  var previewBox = byId('spPreview');
  var previewNote = byId('spPreviewNote');
  var pager = byId('spPager');
  var pageLabel = byId('spPageLabel');
  var pagePrev = byId('spPagePrev');
  var pageNext = byId('spPageNext');
  var printBtn = byId('spPrint');
  var notice = byId('spNotice');
  var formatOfTotal = byId('spFormatOfTotal');
  var formatCount = byId('spFormatCount');
  var dialog = byId('spFormatDialog');
  var dialogTitle = byId('spFormatTitle');
  var dialogSizeRow = byId('spFormatSizeRow');
  var dialogSize = byId('spFormatSize');
  var dialogHint = byId('spFormatHint');

  var previewPage = 1;
  var previewTimer = null;
  var previewSeq = 0;
  var previewAbort = null;
  var printing = false;

  function show(el, visible) { el.classList.toggle('hidden', !visible); }
  function clampInt(value, lo, hi, fallback) {
    var n = parseInt(value, 10);
    if (isNaN(n)) return fallback;
    return Math.min(hi, Math.max(lo, n));
  }
  function radioValue(name) {
    var checked = document.querySelector('input[name="' + name + '"]:checked');
    return checked ? checked.value : null;
  }
  function setRadio(name, value) {
    var input = document.querySelector('input[name="' + name + '"][value="' + value + '"]');
    if (input) input.checked = true;
  }
  function isAuto(field) { return field.sizePt === 'auto'; }

  function copies() { return clampInt(copiesEl.value, 1, LIMITS.maxCopies, 1); }
  function hasContent() {
    return state.numbering.enabled || state.fields.some(function (f) { return f.value.trim() !== ''; });
  }

  function spec() {
    return {
      fields: state.fields.map(function (f) {
        return { value: f.value, align: f.align, valign: f.valign, sizePt: f.sizePt };
      }),
      copies: copies(),
      numbering: {
        enabled: state.numbering.enabled, prefix: state.numbering.prefix, format: state.numbering.format,
        align: state.numbering.align, valign: state.numbering.valign, sizePt: state.numbering.sizePt,
      },
    };
  }

  // --- Remembering the draft (this browser only) -------------------------------

  function save() {
    state.copies = copies();
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); } catch (e) { /* private mode etc. */ }
  }

  function cleanField(raw, defaults) {
    var f = defaults;
    if (!raw || typeof raw !== 'object') return f;
    if (typeof raw.value === 'string') f.value = raw.value.slice(0, LIMITS.maxText);
    if (ALIGN_NAMES[raw.align]) f.align = raw.align;
    if (VALIGN_NAMES[raw.valign]) f.valign = raw.valign;
    if (raw.sizePt === 'auto') f.sizePt = 'auto';
    else if (raw.sizePt) f.sizePt = clampInt(raw.sizePt, LIMITS.minPt, LIMITS.maxPt, DEFAULT_SET_SIZE);
    return f;
  }

  function restore() {
    var saved = null;
    try { saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null'); } catch (e) { saved = null; }
    if (!saved || typeof saved !== 'object') return;
    if (Array.isArray(saved.fields) && saved.fields.length) {
      state.fields = saved.fields.slice(0, LIMITS.maxFields).map(function (raw) { return cleanField(raw, newField()); });
    }
    state.copies = clampInt(saved.copies, 1, LIMITS.maxCopies, 1);
    var n = cleanField(saved.numbering, newNumbering());
    var raw = saved.numbering || {};
    n.enabled = !!raw.enabled;
    if (typeof raw.prefix === 'string') n.prefix = raw.prefix.slice(0, LIMITS.maxPrefix);
    if (raw.format === 'of_total' || raw.format === 'count') n.format = raw.format;
    state.numbering = n;
  }

  // --- Drawing ----------------------------------------------------------------

  // "Center · Middle · Auto (62 pt)" / "Left · Top · 36 pt"
  function summaryText(field, actual) {
    var size;
    if (isAuto(field)) {
      size = 'Auto' + (actual ? ' (' + actual + ' pt)' : '');
    } else {
      size = field.sizePt + ' pt' + (actual && actual < field.sizePt ? ' → ' + actual + ' to fit' : '');
    }
    return ALIGN_NAMES[field.align] + ' · ' + VALIGN_NAMES[field.valign] + ' · ' + size;
  }

  function fieldTitle(index) { return 'Text ' + (index + 1); }

  // The text cards - rebuilt only when texts are added, removed or restored,
  // never while typing (that would drop the keyboard).
  function renderFields() {
    fieldsEl.textContent = '';
    state.fields.forEach(function (field, index) {
      var card = fieldTemplate.content.firstElementChild.cloneNode(true);
      card.querySelector('.sp-field-title').textContent = fieldTitle(index);
      var textarea = card.querySelector('textarea');
      textarea.value = field.value;
      textarea.maxLength = LIMITS.maxText;
      textarea.setAttribute('aria-label', fieldTitle(index));
      textarea.addEventListener('input', function () {
        field.value = textarea.value;
        changed();
      });
      var formatBtn = card.querySelector('.sp-format-btn');
      formatBtn.setAttribute('aria-label', 'Format ' + fieldTitle(index));
      formatBtn.addEventListener('click', function () { openFormat(field, fieldTitle(index)); });
      var removeBtn = card.querySelector('.sp-remove-btn');
      removeBtn.setAttribute('aria-label', 'Remove ' + fieldTitle(index));
      show(removeBtn, state.fields.length > 1);
      removeBtn.addEventListener('click', function () { removeField(index); });
      fieldsEl.appendChild(card);
    });
    syncSummaries();
  }

  function syncSummaries() {
    var cards = fieldsEl.querySelectorAll('.sp-field');
    state.fields.forEach(function (field, index) {
      var actual = lastSizes && lastSizes.fields ? lastSizes.fields[index] : null;
      if (cards[index]) cards[index].querySelector('.sp-field-summary').textContent = summaryText(field, actual);
    });
    numberSummary.textContent = summaryText(state.numbering, lastSizes ? lastSizes.counter : null);
  }

  function counterExample(number, total, format) {
    var prefix = state.numbering.prefix.trim().replace(/\s+/g, ' ');
    var count = format === 'of_total' ? number + ' of ' + total : String(number);
    return prefix ? prefix + ' ' + count : count;
  }

  function syncControls() {
    var total = copies();
    show(numberOptions, state.numbering.enabled);
    formatOfTotal.textContent = counterExample(1, total, 'of_total');
    formatCount.textContent = counterExample(1, total, 'count');
    var pages = state.numbering.enabled ? total : 1;
    if (previewPage > pages) previewPage = pages;
    show(pager, state.numbering.enabled && pages > 1);
    pageLabel.textContent = 'Copy ' + previewPage + ' of ' + pages;
    pagePrev.disabled = previewPage <= 1;
    pageNext.disabled = previewPage >= pages;
    addFieldBtn.disabled = state.fields.length >= LIMITS.maxFields;
    printBtn.textContent = '🖨 Print ' + total + (total === 1 ? ' copy' : ' copies');
    printBtn.disabled = !HAS_PRINTER || printing || !hasContent();
    syncSummaries();
  }

  function changed() {
    syncControls();
    save();
    schedulePreview();
  }

  // --- Adding and removing texts ----------------------------------------------

  function addField() {
    if (state.fields.length >= LIMITS.maxFields) return;
    state.fields.push(newField());
    lastSizes = null;
    renderFields();
    changed();
    var areas = fieldsEl.querySelectorAll('textarea');
    if (areas.length) areas[areas.length - 1].focus();
  }

  function removeField(index) {
    if (state.fields.length <= 1) return;
    if (state.fields[index].value.trim() && !confirm('Remove ' + fieldTitle(index) + '?')) return;
    state.fields.splice(index, 1);
    lastSizes = null;
    renderFields();
    changed();
  }

  // --- The format popup (one for every field) -----------------------------------

  function syncDialog() {
    if (!editing) return;
    setRadio('fmtAlign', editing.align);
    setRadio('fmtValign', editing.valign);
    setRadio('fmtSizeMode', isAuto(editing) ? 'auto' : 'fixed');
    show(dialogSizeRow, !isAuto(editing));
    if (!isAuto(editing)) dialogSize.value = editing.sizePt;
    dialogHint.textContent = isAuto(editing)
      ? 'Auto: as big as the label allows - it gets smaller as you type more.'
      : 'If it doesn\'t fit, it\'s made smaller to fit (never below ' + LIMITS.minPt + ' pt).';
  }

  function openFormat(field, title) {
    editing = field;
    dialogTitle.textContent = title;
    syncDialog();
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
  }

  function closeFormat() {
    if (typeof dialog.close === 'function') dialog.close();
    else dialog.removeAttribute('open');
  }

  // The size a field shows when switched from Auto to a set size: what Auto
  // was printing it at, so nothing jumps.
  function currentAutoSize(field) {
    if (!lastSizes) return DEFAULT_SET_SIZE;
    var index = state.fields.indexOf(field);
    var actual = index >= 0 ? (lastSizes.fields || [])[index] : lastSizes.counter;
    return actual ? clampInt(Math.round(actual), LIMITS.minPt, LIMITS.maxPt, DEFAULT_SET_SIZE) : DEFAULT_SET_SIZE;
  }

  function stepSize(current, direction) {
    if (direction > 0) return SIZE_LADDER.filter(function (s) { return s > current; })[0] || LIMITS.maxPt;
    var smaller = SIZE_LADDER.filter(function (s) { return s < current; });
    return smaller.length ? smaller[smaller.length - 1] : LIMITS.minPt;
  }

  document.querySelectorAll('input[name="fmtAlign"], input[name="fmtValign"], input[name="fmtSizeMode"]')
    .forEach(function (input) {
      input.addEventListener('change', function () {
        if (!editing) return;
        editing.align = radioValue('fmtAlign');
        editing.valign = radioValue('fmtValign');
        if (radioValue('fmtSizeMode') === 'auto') editing.sizePt = 'auto';
        else if (isAuto(editing)) editing.sizePt = currentAutoSize(editing);
        syncDialog();
        changed();
      });
    });
  [['spFormatSizeDown', -1], ['spFormatSizeUp', 1]].forEach(function (pair) {
    byId(pair[0]).addEventListener('click', function () {
      if (!editing || isAuto(editing)) return;
      editing.sizePt = stepSize(editing.sizePt, pair[1]);
      syncDialog();
      changed();
    });
  });
  dialogSize.addEventListener('input', function () {
    if (!editing || isAuto(editing)) return;
    var n = parseInt(dialogSize.value, 10);
    if (!isNaN(n) && n >= LIMITS.minPt && n <= LIMITS.maxPt) {
      editing.sizePt = n;
      changed();
    }
  });
  dialogSize.addEventListener('change', function () {
    if (!editing || isAuto(editing)) return;
    editing.sizePt = clampInt(dialogSize.value, LIMITS.minPt, LIMITS.maxPt, editing.sizePt);
    syncDialog();
    changed();
  });
  byId('spFormatDone').addEventListener('click', closeFormat);
  byId('spFormatClose').addEventListener('click', closeFormat);
  dialog.addEventListener('click', function (e) { if (e.target === dialog) closeFormat(); });
  dialog.addEventListener('close', function () { editing = null; });

  // --- Preview ------------------------------------------------------------------

  function setPreviewNote(text, isError) {
    previewNote.textContent = text || '';
    previewNote.classList.toggle('is-error', !!isError);
    show(previewNote, !!text);
  }

  function schedulePreview() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(loadPreview, PREVIEW_DELAY_MS);
  }

  function loadPreview() {
    if (previewAbort) previewAbort.abort();
    if (!hasContent()) {
      previewSeq++;
      lastSizes = null;
      syncSummaries();
      show(previewImg, false);
      show(previewEmpty, true);
      previewBox.classList.remove('is-stale');
      setPreviewNote('');
      return;
    }
    var seq = ++previewSeq;
    previewAbort = typeof AbortController !== 'undefined' ? new AbortController() : null;
    var body = spec();
    body.page = previewPage;
    previewBox.classList.add('is-loading');
    postJson(PREVIEW_URL, body, previewAbort && previewAbort.signal).then(function (data) {
      if (seq !== previewSeq) return;  // a newer change is already on its way
      previewBox.classList.remove('is-loading');
      if (!data.ok) {
        previewBox.classList.add('is-stale');
        setPreviewNote(data.error || 'Couldn\'t draw the label.', true);
        return;
      }
      previewBox.classList.remove('is-stale');
      lastSizes = data.sizes;
      syncSummaries();
      if (data.image) {
        previewImg.src = data.image;
        previewImg.style.aspectRatio = data.widthMm + ' / ' + data.heightMm;
        show(previewImg, true);
        show(previewEmpty, false);
      } else {
        show(previewImg, false);
        show(previewEmpty, true);
        previewEmpty.textContent = 'No preview - the label font couldn\'t be loaded on the server.';
      }
      setPreviewNote(data.shrunk ? 'The set sizes were made smaller to fit - see each text\'s size below.' : '',
                     false);
    }, function (err) {
      if (seq !== previewSeq || (err && err.name === 'AbortError')) return;
      previewBox.classList.remove('is-loading');
      previewBox.classList.add('is-stale');
      setPreviewNote(err && err.message ? err.message : 'Couldn\'t reach the portal - check the Wi-Fi.', true);
    });
  }

  // JSON in, JSON out. The answer's ok/error/code decide what happens, never
  // the HTTP status - except a trip to the login page (logged out elsewhere).
  function postJson(url, body, signal) {
    var options = {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
      body: JSON.stringify(body),
    };
    if (signal) options.signal = signal;
    return fetch(url, options).then(function (res) {
      if (res.redirected && /\/login/.test(res.url)) {
        throw new Error('You\'ve been logged out - log in again (in a new tab), then try again.');
      }
      return res.json().catch(function () {
        throw new Error('The portal sent an unexpected answer (' + res.status + ') - try again.');
      });
    });
  }

  // --- Print ----------------------------------------------------------------------

  function showNotice(text, isError) {
    notice.textContent = text;
    notice.classList.toggle('is-error', !!isError);
    show(notice, !!text);
  }

  function print() {
    if (printing || !HAS_PRINTER || !hasContent()) return;
    var total = copies();
    if (total > CONFIRM_COPIES_OVER && !confirm('Print ' + total + ' copies?')) return;
    printing = true;
    syncControls();
    printBtn.textContent = 'Printing…';
    showNotice('');
    postJson(PRINT_URL, spec()).then(function (data) {
      printing = false;
      syncControls();
      showNotice(data.ok ? data.message : (data.error || 'Couldn\'t print.'), !data.ok);
    }, function (err) {
      printing = false;
      syncControls();
      showNotice(err && err.message ? err.message : 'Couldn\'t reach the portal - check the Wi-Fi.', true);
    });
  }

  // --- Wiring ---------------------------------------------------------------------

  addFieldBtn.addEventListener('click', addField);

  [['spCopiesDown', -1], ['spCopiesUp', 1]].forEach(function (pair) {
    byId(pair[0]).addEventListener('click', function () {
      copiesEl.value = clampInt(copies() + pair[1], 1, LIMITS.maxCopies, 1);
      changed();
    });
  });
  // Typing a number: only tidied (clamped) when the field is left, so "1" on
  // the way to "12" isn't fought over.
  copiesEl.addEventListener('input', changed);
  copiesEl.addEventListener('change', function () {
    copiesEl.value = copies();
    changed();
  });

  numberEl.addEventListener('change', function () {
    state.numbering.enabled = numberEl.checked;
    changed();
  });
  prefixEl.addEventListener('input', function () {
    state.numbering.prefix = prefixEl.value;
    changed();
  });
  document.querySelectorAll('input[name="numFormat"]').forEach(function (input) {
    input.addEventListener('change', function () {
      state.numbering.format = radioValue('numFormat');
      changed();
    });
  });
  byId('spNumberFormat').addEventListener('click', function () { openFormat(state.numbering, 'Number'); });

  pagePrev.addEventListener('click', function () {
    if (previewPage > 1) { previewPage--; syncControls(); loadPreview(); }
  });
  pageNext.addEventListener('click', function () {
    if (previewPage < copies()) { previewPage++; syncControls(); loadPreview(); }
  });

  printBtn.addEventListener('click', print);

  function showState() {
    copiesEl.value = state.copies;
    numberEl.checked = state.numbering.enabled;
    prefixEl.value = state.numbering.prefix;
    setRadio('numFormat', state.numbering.format);
    renderFields();
  }

  byId('spClear').addEventListener('click', function () {
    if (hasContent() && !confirm('Clear the scratchpad and start over?')) return;
    try { localStorage.removeItem(STORAGE_KEY); } catch (e) { /* ignore */ }
    state = { fields: [newField()], copies: 1, numbering: newNumbering() };
    lastSizes = null;
    previewPage = 1;
    showState();
    showNotice('');
    changed();
    var first = fieldsEl.querySelector('textarea');
    if (first) first.focus();
  });

  restore();
  showState();
  syncControls();
  loadPreview();
})();
