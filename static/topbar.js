// Shared user-menu dropdown (see templates/_user_menu.html) - tap the
// name/icon to reveal Change PIN / Admin / Log out, tap elsewhere to close.
(function () {
  document.querySelectorAll('.user-menu').forEach(function (menu) {
    var trigger = menu.querySelector('.user-menu-trigger');
    var dropdown = menu.querySelector('.user-menu-dropdown');
    if (!trigger || !dropdown) return;

    trigger.addEventListener('click', function (e) {
      e.stopPropagation();
      var willOpen = dropdown.classList.contains('hidden');
      dropdown.classList.toggle('hidden', !willOpen);
      trigger.setAttribute('aria-expanded', String(willOpen));
    });

    document.addEventListener('click', function (e) {
      if (menu.contains(e.target)) return;
      dropdown.classList.add('hidden');
      trigger.setAttribute('aria-expanded', 'false');
    });
  });
})();

// On Android/Pixel Chrome, launching the native Camera app via
// <input capture> (see upload.html's cameraInput) sometimes leaves the
// system status bar painted black after returning to the page - Chrome
// only reliably repaints it to the page's theme-color on some real change,
// not just on regaining focus. Re-writing the same content value here forces
// that repaint once the tab is visible again.
(function () {
  var meta = document.querySelector('meta[name="theme-color"]');
  if (!meta) return;
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') {
      meta.setAttribute('content', meta.getAttribute('content'));
    }
  });
})();

// Delete-session forms in a list (Active Sessions on the job-number page,
// User Sessions on the admin page) - submitted via fetch instead of a plain
// form POST so the row just disappears in place, rather than reloading the
// whole page and dropping the admin/user back at the top of a long list.
(function () {
  document.querySelectorAll('form.delete-session-form').forEach(function (form) {
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var message = form.dataset.confirm || 'Delete this session and all its photos? This cannot be undone.';
      if (!confirm(message)) return;

      var row = form.closest('.session-row, .user-row');
      var formData = new FormData(form);
      formData.set('ajax', '1');
      fetch(form.action, { method: 'POST', body: formData })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (!data.ok) {
            alert(data.error || 'Could not delete this session.');
            return;
          }
          if (row) row.remove();
        })
        .catch(function () {
          alert('Could not delete this session - check your connection and try again.');
        });
    });
  });
})();
