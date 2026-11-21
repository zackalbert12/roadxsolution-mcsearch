/* Admin page: owner-only access-duration popup.
   Access buttons pass their values via data-* attributes (Jinja autoescape
   makes them safe in attributes) instead of an inline onclick handler,
   whose double-quoted JSON strings truncated the attribute and broke the popup. */
document.querySelectorAll('.access-btn').forEach(function (btn) {
  btn.addEventListener('click', function () {
    openAccessModal(btn.dataset.userId, btn.dataset.username, btn.dataset.plan, btn.dataset.accessEnds);
  });
});
function openAccessModal(uid, username, plan, ends) {
  document.getElementById('access-user-id').value = uid;
  document.getElementById('access-username').textContent = username;
  document.getElementById('access-current').textContent = plan + ' — ends ' + ends;
  document.getElementById('access-modal').classList.add('open');
}
function closeAccessModal() {
  document.getElementById('access-modal').classList.remove('open');
}
var accessCancel = document.getElementById('access-cancel');
if (accessCancel) accessCancel.addEventListener('click', closeAccessModal);
var accessModal = document.getElementById('access-modal');
if (accessModal) accessModal.addEventListener('click', function (e) {
  if (e.target === this) closeAccessModal();
});
