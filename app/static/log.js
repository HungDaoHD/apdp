const ACTION_LABEL = {
  login:         '🔑 Login',
  logout:        '🚪 Logout',
  page_load:     '🔃 Page load',
  preview:       '👁 Preview',
  save:          '💾 Save',
  download:      '📥 Download',
  open_datatable:'📂 Open file',
};
const ACTION_CLS = {
  login:'pill-login', logout:'pill-logout', page_load:'pill-page_load',
  preview:'pill-preview', save:'pill-save', download:'pill-download',
  open_datatable:'pill-open_datatable',
};

function esc(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function pillCls(a) { return ACTION_CLS[a] || 'pill-other'; }
// ACTION_LABEL values are trusted constants — the fallback is raw server data, so escape it
function pillLbl(a) { return ACTION_LABEL[a] || esc(a); }

function fmtTs(ts) {
  if (!ts) return '<span class="never">—</span>';
  const d = new Date(ts);
  // Show in local time
  return d.toLocaleString('vi-VN', { timeZone: 'Asia/Ho_Chi_Minh', hour12: false });
}

async function load() {
  document.getElementById('page').innerHTML = '<div class="no-data">Loading…</div>';
  try {
    const [sum, rec] = await Promise.all([
      fetch('/api/log/summary').then(r => { if (!r.ok) throw new Error(r.status+' '+r.statusText); return r.json(); }),
      fetch('/api/log/recent').then(r => { if (!r.ok) throw new Error(r.status+' '+r.statusText); return r.json(); }),
    ]);
    document.getElementById('loadTs').textContent = 'Updated: ' + new Date().toLocaleTimeString('vi-VN', {hour12:false});
    renderPage(sum, rec);
  } catch(e) {
    document.getElementById('page').innerHTML = `<div class="err">⚠ ${esc(e.message)}<br>Make sure you are logged in at <a href="/">the app</a>.</div>`;
  }
}

function renderPage(sum, rec) {
  const page = document.getElementById('page');

  // ── Summary card
  const EXCLUDE_ACTIONS = new Set(['upload_zip']);
  const allActions = new Set();
  sum.users.forEach(u => Object.keys(u.actions).forEach(a => allActions.add(a)));
  const actionCols = [...allActions].filter(a => !EXCLUDE_ACTIONS.has(a)).sort();

  let tHead = `<tr><th>User</th><th>Last seen (ICT)</th><th>Total</th>`;
  actionCols.forEach(a => { tHead += `<th>${pillLbl(a)}</th>`; });
  tHead += `</tr>`;

  let tBody = '';
  sum.users.forEach(u => {
    const last = fmtTs(u.last_seen);
    const total = u.total || 0;
    tBody += `<tr>
      <td class="email">${esc(u.email)}</td>
      <td class="ts-cell">${last}</td>
      <td class="total-badge">${total || '<span class="never">0</span>'}</td>`;
    actionCols.forEach(a => {
      const cnt = u.actions[a] || 0;
      tBody += `<td>${cnt ? `<span class="pill ${pillCls(a)}">${cnt}</span>` : '<span style="color:#ccc">—</span>'}</td>`;
    });
    tBody += '</tr>';
  });

  // ── Recent events (filtered by action)
  let recHtml = buildRecentTable(rec, 'all');

  page.innerHTML = `
    <div class="card">
      <div class="card-hdr">
        👥 FW Users Summary
        <span class="badge">${sum.users.length} users · ${sum.total_events} events</span>
      </div>
      <div style="overflow:auto;max-height:62vh">
        <table><thead>${tHead}</thead><tbody>${tBody || '<tr><td colspan="99" class="no-data">No data</td></tr>'}</tbody></table>
      </div>
    </div>

    <div class="card">
      <div class="card-hdr">
        🕐 Recent Activity
        <span class="badge">${rec.length} records</span>
      </div>
      <div class="filter-bar">
        <label>Filter:</label>
        <select id="actFilter">
          <option value="all">All actions</option>
          ${[...allActions].sort().map(a => `<option value="${esc(a)}">${pillLbl(a)}</option>`).join('')}
        </select>
        <input type="text" id="emailFilter" placeholder="Filter by email…">
      </div>
      <div id="recentWrap" style="overflow-x:auto">${recHtml}</div>
    </div>`;

  // Store recent for filtering
  window._recData = rec;
  window._allActions = allActions;

  // #actFilter/#emailFilter are (re)created by the innerHTML assignment above,
  // so their listeners must be (re)attached here rather than once at load.
  document.getElementById('actFilter').addEventListener('change', filterRecent);
  document.getElementById('emailFilter').addEventListener('input', filterRecent);
}

function buildRecentTable(records, actFilter, emailFilter) {
  let rows = [...records].reverse(); // newest first
  if (actFilter && actFilter !== 'all') rows = rows.filter(r => r.action === actFilter);
  if (emailFilter) rows = rows.filter(r => r.email.includes(emailFilter));

  if (!rows.length) return '<div class="no-data">No records</div>';

  let html = `<table class="log-table"><thead>
    <tr><th>Time (ICT)</th><th>User</th><th>Action</th><th>Survey</th></tr>
  </thead><tbody>`;
  rows.forEach(r => {
    const survey = r.survey_name ? `${esc(r.survey_name)}${r.survey_id ? ` <span style="color:#aaa">#${esc(r.survey_id)}</span>` : ''}` : '<span class="never">—</span>';
    html += `<tr>
      <td class="ts-cell">${fmtTs(r.ts)}</td>
      <td class="email">${esc(r.email)}</td>
      <td><span class="pill ${pillCls(r.action)}">${pillLbl(r.action)}</span></td>
      <td>${survey}</td>
    </tr>`;
  });
  html += '</tbody></table>';
  return html;
}

function filterRecent() {
  const act   = document.getElementById('actFilter')?.value || 'all';
  const email = document.getElementById('emailFilter')?.value || '';
  document.getElementById('recentWrap').innerHTML = buildRecentTable(window._recData || [], act, email);
}

document.getElementById('refreshBtn').addEventListener('click', load);
load();
