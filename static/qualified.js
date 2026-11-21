/* Qualified Carriers page: commodity select-all/clear, star/unstar,
   call-status select. Carrier ids come from data-carrier-id attributes.
   copyText/copyPhone live in app.js and are wired there by delegation. */
function selectAllCommodities() {
  document.querySelectorAll('#qualified-filter input[name="commodity"]').forEach(function (cb) { cb.checked = true; });
  document.getElementById('qualified-filter').submit();
}
function clearCommodities() {
  document.querySelectorAll('#qualified-filter input[name="commodity"]').forEach(function (cb) { cb.checked = false; });
  document.getElementById('qualified-filter').submit();
}
async function toggleStar(id, btn) {
  var r = await fetch('/api/carrier/' + id + '/star', {method: 'POST', headers: {'X-CSRF-Token': csrfToken()}});
  var d = await r.json();
  if (d.ok) btn.classList.toggle('on', !!d.starred);
}
async function setCall(id, status) {
  var r = await fetch('/api/carrier/' + id + '/call-status', {
    method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken()},
    body: JSON.stringify({status: status})});
  var d = await r.json();
  if (d.ok) document.getElementById('calls-' + id).textContent = 'Calls: ' + d.calls;
}

var btnSelectAll = document.getElementById('btn-select-all-commodities');
if (btnSelectAll) btnSelectAll.addEventListener('click', selectAllCommodities);
var btnClear = document.getElementById('btn-clear-commodities');
if (btnClear) btnClear.addEventListener('click', clearCommodities);
document.querySelectorAll('.star-toggle').forEach(function (btn) {
  btn.addEventListener('click', function () { toggleStar(btn.dataset.carrierId, btn); });
});
document.querySelectorAll('.call-select').forEach(function (sel) {
  sel.addEventListener('change', function () { setCall(sel.dataset.carrierId, sel.value); });
});
