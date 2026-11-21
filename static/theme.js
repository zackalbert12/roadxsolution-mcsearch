/* Theme pre-paint: apply the saved theme before first paint so the page
   never flashes light. Loaded synchronously in <head> (no defer/async)
   so it still blocks parsing and sets data-theme in time.
   The saved theme comes from a data-saved-theme attribute on <html>
   (server-rendered: 'light' / 'dark' / 'system', or empty when logged
   out); the header toggle's stored value is only a fallback for
   logged-out pages. */
(function () {
  try {
    var saved = document.documentElement.getAttribute('data-saved-theme') || '';
    var t = '';
    if (saved === 'dark') t = 'dark';
    else if (saved === 'light') t = 'light';
    else if (saved === 'system') t = (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light';
    else { var ls = localStorage.getItem('roadx-theme'); if (ls === 'dark' || ls === 'light') t = ls; }
    if (t) document.documentElement.setAttribute('data-theme', t);
  } catch (e) {}
})();
