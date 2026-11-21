/* Login page: Log In / Sign Up tabs. */
function showTab(which) {
  var paneLogin = document.getElementById('pane-login');
  var paneSignup = document.getElementById('pane-signup');
  if (paneLogin) paneLogin.classList.toggle('hidden', which !== 'login');
  if (paneSignup) paneSignup.classList.toggle('hidden', which !== 'signup');
  var tabLogin = document.getElementById('tab-login');
  var tabSignup = document.getElementById('tab-signup');
  if (tabLogin) tabLogin.className = which === 'login' ? 'active' : '';
  if (tabSignup) tabSignup.className = which === 'signup' ? 'active' : '';
}
document.querySelectorAll('[data-tab]').forEach(function (btn) {
  btn.addEventListener('click', function () { showTab(btn.dataset.tab); });
});
