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
