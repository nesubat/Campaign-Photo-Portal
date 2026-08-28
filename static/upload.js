(function () {
  var gallery = document.getElementById('gallery');
  var counter = document.getElementById('counter');
  var cameraInput = document.getElementById('cameraInput');
  var libraryInput = document.getElementById('libraryInput');
  var submitBtn = document.getElementById('submitBtn');

  function updateCounter() {
    var n = gallery.querySelectorAll('.thumb').length;
    counter.textContent = n + ' photo(s)';
  }

  function addPlaceholder() {
    var tile = document.createElement('div');
    tile.className = 'thumb uploading';
    tile.innerHTML = '<span class="spinner">⏳</span>';
    gallery.prepend(tile);
    updateCounter();
    return tile;
  }

  function uploadOne(file) {
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
  }

  function handleFiles(fileList) {
    Array.prototype.forEach.call(fileList, uploadOne);
  }

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
        updateCounter();
      })
      .catch(function (err) {
        alert('Could not remove photo: ' + err.message);
      });
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

  if (submitBtn) {
    submitBtn.addEventListener('click', function () {
      var busy = gallery.querySelectorAll('.thumb.uploading').length;
      if (busy > 0) {
        alert('Still uploading ' + busy + ' photo(s) - wait for them to finish first.');
        return;
      }
      var total = gallery.querySelectorAll('.thumb').length;
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
          window.location.href = '/?submitted=' + data.count;
        })
        .catch(function (err) {
          alert('Submit failed: ' + err.message);
          submitBtn.disabled = false;
          submitBtn.textContent = 'Submit Batch';
        });
    });
  }
})();
