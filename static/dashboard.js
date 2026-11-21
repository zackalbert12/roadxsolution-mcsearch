/* MC Scanner dashboard: scan start/stop/resume, live polling, commodity
   filter note. API URLs come from data-* attributes on #scan-form
   (server-rendered with url_for), so no Jinja is needed in this file. */

var scanForm = document.getElementById('scan-form');
var API_STATUS = scanForm ? scanForm.dataset.apiStatus : '';
var API_START = scanForm ? scanForm.dataset.apiStart : '';
var API_RESUME = scanForm ? scanForm.dataset.apiResume : '';
var API_STOP = scanForm ? scanForm.dataset.apiStop : '';
var QUALIFIED_URL = scanForm ? scanForm.dataset.qualifiedUrl : '';

var LINES = [
  "Hi, this is Zack from RoadxSolutions — are you set for next week, or do you need a load for Monday?",
  "Good morning — do you need a load for today? Send me the zip where your truck is empty and I'll check what's around.",
  "Quick question — where will your truck be empty on Monday morning? I'll line up loads so you don't start the week sitting.",
  "Hi, this is Zack. I have reefer freight moving this week — is your reefer empty, and where?",
  "Hi — got fresh produce loads posting. Is your truck free for a produce run?",
  "Hi, this is Zack from RoadxSolutions. We find loads for carriers before they even hit the load boards — do you have a truck running right now?"
];
var li = 0;
function anotherLine() { li = (li + 1) % LINES.length; var el = document.getElementById('pickup-line'); if (el) el.textContent = LINES[li]; }
function copyLine() { var el = document.getElementById('pickup-line'); if (el) navigator.clipboard.writeText(el.textContent); }
function fmt(s) { s = Math.max(0, Math.floor(s || 0)); return String(Math.floor(s / 60)).padStart(2, '0') + ':' + String(s % 60).padStart(2, '0'); }
function esc(v) { return String(v == null ? '' : v).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;'); }
function pill(status) {
  if (status === 'Qualified') return '<span class="pill green">Qualified</span>';
  if (status === 'Not Found') return '<span class="pill red">Not Found</span>';
  if (status === 'Error') return '<span class="pill red">Error</span>';
  return '<span class="pill yellow">' + esc(status) + '</span>';
}
async function poll() {
  try {
    var r = await fetch(API_STATUS);
    var ct = r.headers.get('content-type') || '';
    if (r.status === 403 || !ct.includes('application/json')) {
      // Audit fix 7: a 403 page (or a login-page redirect, which is
      // HTML) used to throw inside r.json() and fail silently. Show
      // the message instead, and keep polling.
      showScanMessage('Session expired — reload the page and try again.');
    } else {
      var d = await r.json();
      document.getElementById('st-checked').textContent = d.current + (d.total ? ' / ' + d.total : '');
      document.getElementById('st-qualified').textContent = d.results_count;
      document.getElementById('st-errors').textContent = d.error_count;
      document.getElementById('st-notfound').textContent = d.not_found_count;
      document.getElementById('st-elapsed').textContent = fmt(d.elapsed_seconds);
      document.getElementById('st-eta').textContent = d.running ? fmt(d.eta_seconds) : '--:--';
      document.getElementById('st-speed').textContent = d.rate_per_min + '/min';
      var pct = d.total ? Math.round(d.current / d.total * 100) : 0;
      document.getElementById('bar').style.width = pct + '%';
      if (d.status === 'Paused' || d.status === 'Stopped') {
        document.getElementById('progress-text').textContent = d.status + ' — scan MC-' + d.start_mc + ' → MC-' + d.end_mc + ' stopped at ' + d.current + ' checked, ' + d.results_count + ' qualified. It will not start on its own — press Resume Scan to continue.';
      } else {
        document.getElementById('progress-text').textContent = d.running
          ? pct + '% — Scanning MC-' + d.start_mc + ' → MC-' + d.end_mc + ' (' + d.status + ')'
          : (d.total ? 'Last scan: ' + d.status + ' — ' + d.current + ' checked, ' + d.results_count + ' qualified.' : 'Idle — no scan running.');
      }
      document.getElementById('btn-start').disabled = d.running;
      document.getElementById('btn-stop').disabled = !d.running;
      document.getElementById('btn-resume').classList.toggle('hidden', !d.can_resume);
      var body = document.getElementById('log-body');
      body.innerHTML = d.recent_log.map(function (x) {
        return '<tr><td>' + esc(x.mc_number) + '</td><td>' + pill(x.status) + '</td><td>' + esc(x.entity) + '</td><td>' + esc(x.power_units) + '</td><td>' + esc(x.cargo) + '</td><td>' + esc(x.checked_at) + '</td><td>' + esc(x.note) + '</td></tr>';
      }).join('');
    }
  } catch (e) {}
  setTimeout(poll, 1500);
}
function showScanMessage(msg) {
  var el = document.getElementById('scan-message');
  el.textContent = msg;
  el.classList.remove('hidden');
}
function hideScanMessage() {
  document.getElementById('scan-message').classList.add('hidden');
}
async function startScan(ev) {
  ev.preventDefault();
  var s = document.getElementById('start_mc').value.trim();
  var e = document.getElementById('end_mc').value.trim();
  if (!s || !e) {
    showScanMessage('Enter the MC numbers first — type the Start MC and End MC, then press Start Scanning.');
    return false;
  }
  hideScanMessage();
  var fd = new FormData();
  fd.append('csrf_token', csrfToken());
  fd.append('start_mc', s);
  fd.append('end_mc', e);
  var minPu = document.getElementById('min_pu').value.trim();
  var maxPu = document.getElementById('max_pu').value.trim();
  if (minPu) fd.append('min_pu', minPu);
  if (maxPu) fd.append('max_pu', maxPu);
  var commodity = document.getElementById('commodity').value;
  if (commodity) fd.append('commodity', commodity);
  // Audit fix 7: check the response before parsing JSON — a 403 page
  // (or a login redirect) is HTML, and r.json() on it used to throw
  // silently so the button just did nothing.
  var r;
  try {
    r = await fetch(API_START, {method: 'POST', body: fd});
  } catch (e) {
    showScanMessage('Could not start scan — check your connection and try again.');
    return false;
  }
  if (r.status === 403 || !(r.headers.get('content-type') || '').includes('application/json')) {
    // A 403 from the API carries a JSON error (e.g. the Demo 10,000-MC
    // cap) — show the server's message, not a generic session error.
    // Only fall back to "Session expired" when there is no JSON error.
    var d0 = null;
    try { d0 = await r.clone().json(); } catch (e) {}
    showScanMessage((d0 && d0.error) || 'Session expired — reload the page and try again.');
    return false;
  }
  var d;
  try { d = await r.json(); } catch (e) { showScanMessage('Could not start scan. Please reload the page and try again.'); return false; }
  if (!d.ok) showScanMessage(d.error || 'Could not start scan.');
  return false;
}
async function resumeScan() {
  var r;
  try {
    r = await fetch(API_RESUME, {method: 'POST', headers: {'X-CSRF-Token': csrfToken()}});
  } catch (e) {
    showScanMessage('Could not resume scan — check your connection and try again.');
    return;
  }
  if (r.status === 403 || !(r.headers.get('content-type') || '').includes('application/json')) {
    var d0 = null;
    try { d0 = await r.clone().json(); } catch (e) {}
    showScanMessage((d0 && d0.error) || 'Session expired — reload the page and try again.');
    return;
  }
  var d;
  try { d = await r.json(); } catch (e) { showScanMessage('Could not resume scan. Please reload the page and try again.'); return; }
  if (!d.ok) showScanMessage(d.error || 'Could not resume scan.');
  else showScanMessage('Scan resumed from MC-' + d.resumed_from + '.');
}
async function stopScan() {
  try {
    var r = await fetch(API_STOP, {method: 'POST', headers: {'X-CSRF-Token': csrfToken()}});
    if (r.status === 403) {
      var d = null;
      try { d = await r.clone().json(); } catch (e) {}
      showScanMessage((d && d.error) || 'Session expired — reload the page and try again.');
    }
  } catch (e) {}
}
function applyCommodity(v) {
  var note = document.getElementById('commodity-note');
  if (!v) { note.classList.add('hidden'); return; }
  note.classList.remove('hidden');
  note.textContent = '';
  note.appendChild(document.createTextNode('Commodity '));
  var b = document.createElement('b'); b.textContent = v; note.appendChild(b);
  note.appendChild(document.createTextNode(' selected — the scan will only qualify carriers that haul '));
  var b2 = document.createElement('b'); b2.textContent = v; note.appendChild(b2);
  note.appendChild(document.createTextNode('. After the scan, open '));
  var a = document.createElement('a');
  a.href = QUALIFIED_URL + '?commodity=' + encodeURIComponent(v);
  a.textContent = 'Qualified Carriers';
  note.appendChild(a);
  note.appendChild(document.createTextNode(' filtered by ' + v + ' to see matching MCs.'));
}

if (scanForm) {
  scanForm.addEventListener('submit', startScan);
  var commoditySel = document.getElementById('commodity');
  if (commoditySel) commoditySel.addEventListener('change', function () { applyCommodity(this.value); });
  var btnResume = document.getElementById('btn-resume');
  if (btnResume) btnResume.addEventListener('click', resumeScan);
  var btnStop = document.getElementById('btn-stop');
  if (btnStop) btnStop.addEventListener('click', stopScan);
  poll();
}
