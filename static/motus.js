/* Motus Search page: live polling + stop. API URLs come from data-*
   attributes on #motus-config (server-rendered with url_for). */
var motusConfig = document.getElementById('motus-config');
var MOTUS_STATUS_URL = motusConfig ? motusConfig.dataset.statusUrl : '';
var MOTUS_STOP_URL = motusConfig ? motusConfig.dataset.stopUrl : '';

function esc(v) { return String(v == null ? '' : v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;'); }
function pill(status) {
  if (status === 'Qualified') return '<span class="pill green">Qualified</span>';
  if (status === 'Not Found') return '<span class="pill red">Not Found</span>';
  if (status === 'Error') return '<span class="pill red">Error</span>';
  return '<span class="pill yellow">' + esc(status) + '</span>';
}
async function poll() {
  try {
    var r = await fetch(MOTUS_STATUS_URL);
    var d = await r.json();
    document.getElementById('m-checked').textContent = d.checked + ' / ' + d.total;
    document.getElementById('m-qualified').textContent = d.qualified;
    document.getElementById('m-errors').textContent = d.errors;
    document.getElementById('m-status').textContent = d.running ? 'Running…' : (d.status || 'Idle');
    document.getElementById('log-body').innerHTML = d.recent_log.map(function (x) {
      return '<tr><td>' + esc(x.mc_number) + '</td><td>' + pill(x.status) + '</td><td>' + esc(x.entity) + '</td><td>' + esc(x.power_units) + '</td><td>' + esc(x.cargo) + '</td><td>' + esc(x.checked_at) + '</td><td>' + esc(x.note) + '</td></tr>';
    }).join('');
  } catch (e) {}
  setTimeout(poll, 1500);
}
async function stopMotus() { await fetch(MOTUS_STOP_URL, {method: 'POST', headers: {'X-CSRF-Token': csrfToken()}}); }

if (motusConfig) {
  var btnStop = document.getElementById('btn-stop-motus');
  if (btnStop) btnStop.addEventListener('click', stopMotus);
  poll();
}
