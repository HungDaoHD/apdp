let _file = null, _b64 = null, _filename = null, _errOnly = false;

// Every SKU field below comes from a user-uploaded .xlsx (product_name,
// remarks, agency_comment, error…) — none of it is trusted, so it must be
// escaped before it reaches innerHTML. esc() is for element text/attributes.
function esc(s) {
  return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

document.getElementById('fileInput').addEventListener('change', e => {
  const f = e.target.files[0]; if (!f) return;
  _file = f;
  const el = document.getElementById('fname');
  el.textContent = '📎 ' + f.name; el.classList.add('set');
  document.getElementById('runBtn').disabled = false;
  // Reset week selector when new file chosen
  const sc = document.getElementById('selCurr');
  sc.innerHTML = '<option value="">Auto</option>';
  sc.disabled = true;
  clearResult();
});

function clearResult() {
  ['errMsg','statsStrip','tableCard'].forEach(id => document.getElementById(id).classList.add('hidden'));
  document.getElementById('dlBtn').classList.add('hidden');
  document.getElementById('statusMsg').textContent = '';
  _b64 = null; _filename = null; _errOnly = false;
  document.getElementById('filterBtn').classList.remove('active');
}

function populateWeekSelectors(availableWeeks, currWeek) {
  const s = document.getElementById('selCurr');
  s.innerHTML = availableWeeks.map(w =>
    `<option value="${w.replace('W','')}" ${w===currWeek?'selected':''}>${w}</option>`
  ).join('');
  s.disabled = false;
}

async function runCheck() {
  if (!_file) return;
  clearResult();
  const btn = document.getElementById('runBtn');
  btn.disabled = true;
  document.getElementById('statusMsg').textContent = '⏳ Processing…';
  try {
    // Fetched fresh each run rather than cached at page load — this page has
    // no other init step, and price-check is infrequent enough that one
    // extra round-trip per run isn't worth adding a load-time flow for.
    const { csrf_token } = await fetch('/api/qme/status').then(r => r.json());
    const fd = new FormData();
    fd.append('file', _file);
    const cv = document.getElementById('selCurr').value;
    if (cv) fd.append('curr_week', cv);
    const res = await fetch('/api/price-check', {method:'POST', headers: { 'X-CSRF-Token': csrf_token }, body:fd});
    const data = await res.json();
    if (!res.ok) { showErr(data.detail || `Error ${res.status}`); return; }
    showResult(data);
  } catch(e) { showErr('Network error — ' + e.message); }
  finally { btn.disabled = false; document.getElementById('statusMsg').textContent = ''; }
}

function showErr(msg) {
  const el = document.getElementById('errMsg'); el.textContent = '⚠ ' + msg; el.classList.remove('hidden');
}

function showResult(d) {
  _b64 = d.base64; _filename = d.filename;

  // Populate week selectors with available weeks from file
  if (d.available_weeks?.length) {
    populateWeekSelectors(d.available_weeks, d.week_current);
  }

  // Stats
  const strip = document.getElementById('statsStrip');
  strip.innerHTML = `
    <span class="chip">Week <b>${d.week_current} vs ${d.week_prev}</b></span>
    <span class="chip">SKUs <b>${d.n_skus}</b></span>
    <span class="chip ${d.n_errors ? 'err':''}">Errors <b>${d.n_errors}</b></span>`;
  strip.classList.remove('hidden');
  document.getElementById('dlBtn').classList.remove('hidden');

  // Header
  const wc = d.week_current, wp = d.week_prev;
  document.getElementById('skuThead').innerHTML = '<tr>' + [
    ['STT',                        'c-base'],
    ['Product Description Detail', 'c-base'],
    [`Price ${wc}`,                'c-w24' ],
    [`Regular price ${wc}`,        'c-w24' ],
    [`Promo OFF ${wc}`,            'c-w24' ],
    ['Promo Start Date',           'c-date'],
    ['Promo End Date',             'c-date'],
    ['Others Remarks',             'c-rem' ],
    ["Agency's comment",           'c-rem' ],
    [`Price ${wp}`,                'c-w23' ],
    [`Regular price ${wp}`,        'c-w23' ],
    [`Promo OFF ${wp}`,            'c-w23' ],
    ['ERRORS',                     'c-err' ],
  ].map(([l,c]) => `<th class="${c}">${l}</th>`).join('') + '</tr>';

  // Body
  document.getElementById('skuTbody').innerHTML = d.skus.map(s => {
    const cls = s.error ? 'bad' : 'ok';
    return `<tr class="${cls}" data-err="${s.error?1:0}" data-name="${esc((s.product_name||'').toLowerCase())}">
      <td class="ctr">${esc(s.stt)}</td>
      <td class="desc" title="${esc(s.product_name||'')}">${s.product_name ? esc(s.product_name) : '—'}</td>
      <td class="num">${s.curr_price ? esc(s.curr_price) : '—'}</td>
      <td class="num">${s.curr_regular ? esc(s.curr_regular) : '—'}</td>
      <td class="ctr">${s.curr_promo_off ? esc(s.curr_promo_off) : '—'}</td>
      <td class="ctr">${s.promo_start ? esc(s.promo_start) : '—'}</td>
      <td class="ctr">${s.promo_end ? esc(s.promo_end) : '—'}</td>
      <td class="ctr">${s.remarks ? esc(s.remarks) : '—'}</td>
      <td class="ctr">${esc(s.agency_comment)}</td>
      <td class="num">${s.prev_price ? esc(s.prev_price) : '—'}</td>
      <td class="num">${s.prev_regular ? esc(s.prev_regular) : '—'}</td>
      <td class="ctr">${s.prev_promo_off ? esc(s.prev_promo_off) : '—'}</td>
      <td class="err-cell">${esc(s.error)}</td>
    </tr>`;
  }).join('');

  document.getElementById('tableTitle').textContent = `📋 SKU List — ${d.n_skus} items`;
  document.getElementById('tableCard').classList.remove('hidden');
}

function toggleFilter() {
  _errOnly = !_errOnly;
  const btn = document.getElementById('filterBtn');
  btn.classList.toggle('active', _errOnly);
  btn.textContent = _errOnly ? '📋 Show all' : '🔴 Errors only';
  applyFilters();
}

function applyFilters() {
  const q = (document.getElementById('searchBox')?.value || '').toLowerCase().trim();
  document.querySelectorAll('#skuTbody tr').forEach(tr => {
    const matchErr  = !_errOnly || tr.dataset.err === '1';
    const matchText = !q || (tr.dataset.name || '').includes(q);
    tr.style.display = (matchErr && matchText) ? '' : 'none';
  });
}

function downloadFile() {
  if (!_b64 || !_filename) return;
  const bin = atob(_b64), arr = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
  const a = Object.assign(document.createElement('a'), {
    href: URL.createObjectURL(new Blob([arr],
      {type:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'})),
    download: _filename,
  });
  a.click(); URL.revokeObjectURL(a.href);
}

document.getElementById('runBtn').addEventListener('click', runCheck);
document.getElementById('dlBtn').addEventListener('click', downloadFile);
document.getElementById('searchBox').addEventListener('input', applyFilters);
document.getElementById('filterBtn').addEventListener('click', toggleFilter);
