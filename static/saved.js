/* Saved MCs page: unstar removes the row.
   copyText/copyPhone live in app.js and are wired there by delegation. */
async function toggleStar(id, btn) {
  var r = await fetch('/api/carrier/' + id + '/star', {method: 'POST', headers: {'X-CSRF-Token': csrfToken()}});
  var d = await r.json();
  if (d.ok) btn.closest('tr').remove();
}
document.querySelectorAll('.star-unstar').forEach(function (btn) {
  btn.addEventListener('click', function () { toggleStar(btn.dataset.carrierId, btn); });
});
