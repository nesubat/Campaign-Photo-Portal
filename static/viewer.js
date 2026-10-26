/*
 * Photo viewer: tapping a photo opens it full size over the page - in the same
 * tab, with a Download button and × - instead of in a new tab. Loaded on every
 * page that shows photos (the upload page in both modes and kit mode, the
 * gallery, My Contributions).
 *
 * One delegated click listener covers every photo tile, including the ones
 * upload.js / kit.js add later: any <a class="thumb"> linking a full-size
 * /media/<job>/<category>/<file>. Left alone (they keep working as before):
 *   - a tile whose local copy has been cleaned up ("In Drive" / "View on
 *     Drive") - its link goes to the Drive page, there's nothing here to show;
 *   - a tap on a tile's own button (× remove, Retry) - those handlers run first
 *     and stop the event, and a click from inside a <button> is skipped anyway;
 *   - Ctrl/Shift/⌘/middle-click on a computer, which still open a new tab.
 *
 * The viewer is a <dialog> opened with showModal(), so it sits above the pack /
 * consignment dialogs it can be opened from, and Esc closes it. While it's
 * open it owns one history entry, so a phone's Back button closes the photo
 * rather than leaving the page - on a kit page leaving would release the pack
 * being edited, and on an upload page cut short the photos still uploading.
 * The thumbnail shows at once, the full photo replaces it once loaded.
 */
(function () {
  'use strict';

  var FULL_PHOTO_PATH = /^\/media\/[^/]+\/[^/]+\/(?!thumbs\/)[^/]+$/;

  var dialog = null;
  var img = null;
  var status = null;
  var caption = null;
  var downloadLink = null;
  var loadToken = 0;
  var pushed = false;          // our history entry is the current one
  var leavingByBack = false;   // we called history.back() and its popstate hasn't come yet

  function build() {
    dialog = document.createElement('dialog');
    dialog.className = 'photo-viewer';
    dialog.setAttribute('aria-label', 'Photo');

    var bar = document.createElement('div');
    bar.className = 'photo-viewer-bar';

    downloadLink = document.createElement('a');
    downloadLink.className = 'photo-viewer-download';
    downloadLink.textContent = '⬇ Download';

    var closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'photo-viewer-close';
    closeBtn.setAttribute('aria-label', 'Close photo');
    closeBtn.textContent = '×';
    closeBtn.addEventListener('click', close);

    bar.appendChild(downloadLink);
    bar.appendChild(closeBtn);

    var stage = document.createElement('div');
    stage.className = 'photo-viewer-stage';
    img = document.createElement('img');
    img.className = 'photo-viewer-img';
    img.alt = '';
    stage.appendChild(img);

    status = document.createElement('div');
    status.className = 'photo-viewer-status';
    status.setAttribute('aria-live', 'polite');

    caption = document.createElement('div');
    caption.className = 'photo-viewer-caption';

    dialog.appendChild(bar);
    dialog.appendChild(stage);
    dialog.appendChild(status);
    dialog.appendChild(caption);

    // A tap on the dark area around the photo closes it, like most viewers.
    dialog.addEventListener('click', function (e) {
      if (e.target === dialog || e.target === stage) close();
    });
    dialog.addEventListener('close', onClosed);
    document.body.appendChild(dialog);
  }

  function setStatus(text) {
    status.textContent = text;
    status.classList.toggle('hidden', !text);
  }

  function fileName(path) {
    var name = path.split('/').pop() || 'photo.jpg';
    try { return decodeURIComponent(name); } catch (e) { return name; }
  }

  function captionFor(tile) {
    var given = tile.getAttribute('data-caption');
    if (given) return given;
    var tag = tile.querySelector('.tag'); // the gallery's "name · Drive ✓" line
    return tag ? tag.textContent.replace(/\s+/g, ' ').trim() : '';
  }

  function open(tile, path) {
    if (!dialog) build();
    var thumb = tile.querySelector('img');
    var token = ++loadToken;

    img.classList.add('is-preview');
    if (thumb && thumb.getAttribute('src')) img.src = thumb.getAttribute('src');
    else img.removeAttribute('src');
    setStatus('Loading photo…');

    var full = new Image();
    full.onload = function () {
      if (token !== loadToken) return;
      img.src = path;
      img.classList.remove('is-preview');
      setStatus('');
    };
    full.onerror = function () {
      if (token !== loadToken) return;
      setStatus(thumb ? 'Couldn\'t load the full photo - showing the preview.' : 'Couldn\'t load this photo.');
    };
    full.src = path;

    downloadLink.href = path + (path.indexOf('?') === -1 ? '?' : '&') + 'download=1';
    downloadLink.setAttribute('download', fileName(path));
    var text = captionFor(tile);
    caption.textContent = text;
    caption.classList.toggle('hidden', !text);

    if (!dialog.open) {
      if (typeof dialog.showModal === 'function') dialog.showModal();
      else dialog.setAttribute('open', '');
    }
    // While our own Back is still on its way, its popstate re-adds the entry.
    if (!pushed && !leavingByBack) pushEntry();
  }

  function pushEntry() {
    try {
      history.pushState({ photoViewer: true }, '');
      pushed = true;
    } catch (e) { /* no history - Back just leaves the page, as before */ }
  }

  function close() {
    if (!dialog) return;
    if (typeof dialog.close === 'function' && dialog.open) dialog.close();
    else if (dialog.hasAttribute('open')) { dialog.removeAttribute('open'); onClosed(); }
  }

  function onClosed() {
    loadToken++;
    img.removeAttribute('src');
    // Closed with × / Esc / a tap outside: take our history entry back off,
    // so the next Back leaves the page as it always did.
    if (pushed) {
      pushed = false;
      leavingByBack = true;
      history.back();
    }
  }

  window.addEventListener('popstate', function () {
    if (leavingByBack) {
      leavingByBack = false;
      if (dialog && dialog.open && !pushed) pushEntry(); // reopened before that Back landed
      return;
    }
    if (pushed) {
      pushed = false; // Back took our entry off already
      close();
    }
  });

  document.addEventListener('click', function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
    var target = e.target;
    if (!target || !target.closest) return;
    if (target.closest('button, input, label')) return;
    var tile = target.closest('a.thumb');
    if (!tile || tile.querySelector('.thumb-cleaned')) return;
    var href = tile.getAttribute('href');
    if (!href) return;
    var path = href.split('#')[0].split('?')[0];
    if (!FULL_PHOTO_PATH.test(path)) return;
    e.preventDefault();
    open(tile, path);
  });
})();
