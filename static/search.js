/* Carrier Search page (_search_body.html): search-type placeholder,
   Copy-all-details, and bulk CSV/JSON downloads.
   copyText/copyPhone live in app.js and are wired there by delegation. */

/* Search placeholder follows the selected search type — every sample is zeros. */
var SEARCH_PH = {auto: '000000, (000) 000-0000', mc: '000000', usdot: '000000', phone: '(000) 000-0000', email: '000000@000000.com', name: '000000'};
(function () {
  var sel = document.getElementById('stype');
  var inp = document.getElementById('q');
  if (sel && inp) {
    sel.addEventListener('change', function () { inp.placeholder = SEARCH_PH[sel.value] || SEARCH_PH.auto; });
  }
})();

function fallbackCopy(text) {
  var t = document.createElement('textarea');
  t.value = text; document.body.appendChild(t); t.select();
  try { document.execCommand('copy'); } catch (_) {}
  t.remove();
}

function copyAllDetails(btn) {
  var g = function (k) { return (btn.getAttribute('data-' + k) || '').trim(); };
  var lines = [];
  var name = g('name'); if (name) lines.push(name);
  var dba = g('dba'); if (dba) lines.push('DBA: ' + dba);
  var owner = g('owner'); if (owner) lines.push('Owner: ' + owner);
  var mc = g('mc'); if (mc) lines.push('MC: ' + mc);
  var dot = g('dot'); if (dot) lines.push('USDOT: ' + dot);
  var phone = g('phone'); if (phone) lines.push('Phone: ' + phone);
  var addr = g('address').replace(/^,\s*/, ''); if (addr) lines.push('Address: ' + addr);
  var expected = g('expected'); if (expected) lines.push('Expected Truck: ' + expected);
  var text = lines.join('\n');
  if (!text) return;
  var done = function () {
    var old = btn.textContent;
    btn.textContent = 'Copied ✓';
    setTimeout(function () { btn.textContent = old; }, 1200);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(function () { fallbackCopy(text); done(); });
  } else { fallbackCopy(text); done(); }
}
document.querySelectorAll('.copy-all-btn').forEach(function (btn) {
  btn.addEventListener('click', function () { copyAllDetails(btn); });
});

function bulkRows() {
  /* Same table data as downloadBulkCsv, as objects — shared by CSV + JSON. */
  var rows = [];
  document.querySelectorAll('#bulk-table tbody tr').forEach(function (tr) {
    var cells = [];
    tr.querySelectorAll('td').forEach(function (td) { cells.push(td.innerText.replace(/\n/g, ' ').trim()); });
    var mc = (cells[2].match(/MC-(\d+)/) || [])[1] || '';
    var dot = (cells[2].match(/USDOT (\d+)/) || [])[1] || '';
    var loc = cells[6].split(',');
    rows.push({input: cells[0], carrier: cells[1], mc: mc, usdot: dot,
               status: cells[3], authority: cells[4], phone: cells[5],
               city: (loc[0] || '').trim(), state: (loc[1] || '').trim(),
               cargo: cells[7]});
  });
  return rows;
}
function downloadBulkCsv() {
  var rows = [['Input', 'Carrier', 'MC', 'USDOT', 'Status', 'Authority', 'Phone', 'City', 'State', 'Cargo']];
  bulkRows().forEach(function (o) {
    rows.push([o.input, o.carrier, o.mc, o.usdot, o.status, o.authority,
               o.phone, o.city, o.state, o.cargo]);
  });
  var csv = rows.map(function (r) { return r.map(function (v) { return '"' + String(v).replace(/"/g, '""') + '"'; }).join(','); }).join('\n');
  var a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([csv], {type: 'text/csv'}));
  a.download = 'bulk-carrier-search.csv';
  a.click();
}
function downloadBulkJson() {
  var a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob(
    [JSON.stringify(bulkRows(), null, 2)], {type: 'application/json'}));
  a.download = 'bulk-carrier-search.json';
  a.click();
}
document.querySelectorAll('.download-bulk-csv').forEach(function (btn) {
  btn.addEventListener('click', downloadBulkCsv);
});
document.querySelectorAll('.download-bulk-json').forEach(function (btn) {
  btn.addEventListener('click', downloadBulkJson);
});
