/* Settings page: Delete My Account asks for confirmation, then disables
   the button and shows progress while the form submits. */
var deleteForm = document.getElementById('delete-account-form');
if (deleteForm) {
  deleteForm.addEventListener('submit', function (e) {
    if (!confirm('Delete your account and all its data? This cannot be undone.')) { e.preventDefault(); return; }
    var b = this.querySelector('button[type=submit]');
    b.disabled = true;
    b.textContent = 'Deleting…';
  });
}
