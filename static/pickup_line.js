// Smart Pickup Line card (dashboard): Copy button. The line itself is
// rendered server-side from the real pickup-lines list; "Get Another
// Line" is a plain link, so this file only handles the clipboard copy.
document.addEventListener('DOMContentLoaded', function () {
  var btn = document.getElementById('copy-line-btn');
  if (!btn) return;
  btn.addEventListener('click', function () {
    var text = btn.getAttribute('data-line') || '';
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        btn.textContent = 'Copied!';
        setTimeout(function () { btn.textContent = 'Copy Line'; }, 1500);
      });
    }
  });
});
