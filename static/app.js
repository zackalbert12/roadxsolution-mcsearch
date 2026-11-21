/* Shared site JS (base.html): theme toggle, CSRF token helper, tap-to-copy
   buttons, generic confirm-on-submit forms, and reload links.
   No inline handlers: buttons are wired here via delegation on
   data-* attributes, so the CSP needs no 'unsafe-inline'. */

function currentTheme() {
  return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
}
function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  try { localStorage.setItem('roadx-theme', t); } catch (e) {}
  document.querySelectorAll('.theme-toggle').forEach(function (b) {
    var icon = b.querySelector('.theme-toggle-icon');
    var label = b.querySelector('.theme-toggle-label');
    if (icon) icon.textContent = t === 'dark' ? '☀' : '☾';
    if (label) label.textContent = t === 'dark' ? 'Light' : 'Dark';
  });
}
function toggleTheme() { applyTheme(currentTheme() === 'dark' ? 'light' : 'dark'); }
document.addEventListener('DOMContentLoaded', function () { applyTheme(currentTheme()); });

function csrfToken() {
  var m = document.querySelector('meta[name="csrf-token"]');
  return m ? m.content : '';
}

async function copyText(btn) {
  var text = btn.getAttribute('data-copy');
  if (!text) return;
  try { await navigator.clipboard.writeText(text); }
  catch (e) {
    var t = document.createElement('textarea');
    t.value = text; document.body.appendChild(t); t.select();
    try { document.execCommand('copy'); } catch (_) {}
    t.remove();
  }
  btn.classList.add('copied');
  var old = btn.textContent;
  btn.textContent = 'Copied ✓';
  setTimeout(function () { btn.textContent = old; btn.classList.remove('copied'); }, 1200);
}

async function copyPhone(btn) {
  var phone = btn.getAttribute('data-phone');
  if (!phone) return;
  try { await navigator.clipboard.writeText(phone); }
  catch (e) {
    var t = document.createElement('textarea');
    t.value = phone; document.body.appendChild(t); t.select();
    try { document.execCommand('copy'); } catch (_) {}
    t.remove();
  }
  btn.classList.add('copied');
  var old = btn.textContent;
  btn.textContent = 'Copied ✓';
  setTimeout(function () { btn.textContent = old; btn.classList.remove('copied'); }, 1200);
}

/* Tap-to-copy + theme toggle, wired by delegation so buttons rendered
   anywhere (search results, qualified, saved, restarted) just work. */
document.addEventListener('click', function (e) {
  var themeBtn = e.target.closest('.theme-toggle');
  if (themeBtn) { toggleTheme(); return; }
  var copyBtn = e.target.closest('[data-copy]');
  if (copyBtn) { copyText(copyBtn); return; }
  var phoneBtn = e.target.closest('[data-phone]');
  if (phoneBtn) { copyPhone(phoneBtn); return; }
  var reloadLink = e.target.closest('[data-reload]');
  if (reloadLink) { e.preventDefault(); location.reload(); }
});

/* Generic confirm: any <form data-confirm="..."> asks before submitting. */
document.addEventListener('submit', function (e) {
  var form = e.target;
  if (form && form.matches && form.matches('form[data-confirm]')) {
    if (!window.confirm(form.getAttribute('data-confirm'))) e.preventDefault();
  }
});
