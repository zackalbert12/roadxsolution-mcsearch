/* Pickup Lines page: Copy buttons pass the line via a data-* attribute
   (Jinja autoescape makes it safe in attributes) instead of an inline
   onclick handler, whose double-quoted JSON strings truncated the
   attribute and broke the button. Delete confirmations use the generic
   form[data-confirm] handler in app.js. */
document.querySelectorAll('.copy-line-btn').forEach(function (btn) {
  btn.addEventListener('click', function () {
    navigator.clipboard.writeText(btn.dataset.line).then(function () {
      btn.textContent = 'Copied!';
    });
  });
});
