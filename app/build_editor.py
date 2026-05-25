"""One-time script to build app/static/editor.html from the surveyflow source.

Usage:
    python build_editor.py <src_html> <dst_html>

Example:
    python build_editor.py ../surveyflow/skills/surveyflow/datatable-editor.html static/editor.html
"""
import sys
from pathlib import Path

if len(sys.argv) != 3:
    print(__doc__)
    sys.exit(1)

SRC = Path(sys.argv[1])
DST = Path(sys.argv[2])

html = SRC.read_text(encoding="utf-8")

# 1. Replace header
old_hdr = '''<div class="hdr">
  <span class="hdr-logo">📊 SurveyFlow Editor</span>
  <div class="sep"></div>
  <span class="fbadge none" id="metaBadge">metadata: —</span>
  <button class="btn btn-gho" onclick="openMeta()">📂 Metadata</button>
  <div class="sep"></div>
  <span class="fbadge none" id="dtBadge">datatable: —</span>
  <button class="btn btn-gho" onclick="openDT()">📂 Datatable</button>
  <div class="sep"></div>
  <span class="fbadge none" id="rdBadge">csv: —</span>
  <button class="btn btn-gho" onclick="openRd()">📂 CSV</button>
  <button class="btn" id="runBtn" disabled onclick="showPreview()"
    style="background:#7c3aed;color:white;opacity:.45;margin-left:2px">▶ Preview</button>
  <div class="gap"></div>
  <button class="btn btn-gho" onclick="toggleQP()" id="qpToggle">◀ Questions</button>
  <button class="btn btn-suc" onclick="saveFile()" id="saveBtn" disabled>💾 Save</button>
  <button class="btn btn-gho" onclick="toggleRaw()" id="rawBtn">{} Raw</button>
</div>'''

new_hdr = '''<div class="hdr">
  <span class="hdr-logo">📊 SurveyFlow Editor</span>
  <div class="sep"></div>
  <button class="btn btn-pri btn-sm" onclick="openSurveyPanel()">🔍 Survey</button>
  <span class="fbadge none" id="surveyBadge" style="max-width:200px">Chưa chọn survey</span>
  <div class="sep"></div>
  <span class="fbadge none" id="fetchBadge">fetch: —</span>
  <span class="fbadge none" id="ingestBadge" style="margin-left:4px">ingest: —</span>
  <div class="sep"></div>
  <span class="fbadge none" id="dtBadge">datatable: —</span>
  <button class="btn btn-gho btn-sm" onclick="openDT()" id="loadDtBtn" disabled>📂 Load DT</button>
  <button class="btn" id="runBtn" disabled onclick="showPreview()"
    style="background:#7c3aed;color:white;opacity:.45;margin-left:2px">▶ Preview</button>
  <div class="gap"></div>
  <button class="btn btn-gho" onclick="toggleQP()" id="qpToggle">◀ Questions</button>
  <button class="btn btn-suc" onclick="saveFile()" id="saveBtn" disabled>💾 Save</button>
  <button class="btn btn-pri btn-sm" onclick="runPipeline()" id="pipelineBtn" disabled>⚙ Run</button>
  <button class="btn btn-grn btn-sm" onclick="downloadServerXlsx()" id="dlServerBtn" disabled>⬇ xlsx</button>
  <button class="btn btn-gho" onclick="toggleRaw()" id="rawBtn">{} Raw</button>
</div>'''

assert old_hdr in html, "header not found"
html = html.replace(old_hdr, new_hdr, 1)

# 2. Add CSS for survey panel before </style>
survey_css = """
/* Survey panel */
.sp-ov{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:2000;display:flex;align-items:center;justify-content:center}
.sp-dlg{background:white;border-radius:10px;width:640px;max-width:95vw;max-height:85vh;display:flex;flex-direction:column;box-shadow:0 8px 40px rgba(0,0,0,.25)}
.sp-hd{padding:14px 18px;border-bottom:1px solid #e0e4ea;display:flex;align-items:center;justify-content:space-between}
.sp-hd strong{font-size:14px;color:#1a1a2e}
.sp-body{padding:16px 18px;display:flex;flex-direction:column;gap:12px;overflow:hidden;flex:1}
.sp-row{display:flex;gap:8px}
.sp-inp{flex:1;padding:7px 10px;border:1px solid #dde1e7;border-radius:5px;font-size:13px;outline:none;font-family:inherit}
.sp-inp:focus{border-color:#1a73e8}
.sp-res{flex:1;overflow-y:auto;border:1px solid #e0e4ea;border-radius:6px;min-height:180px;max-height:320px}
.sp-item{padding:9px 12px;cursor:pointer;border-bottom:1px solid #f0f2f5;transition:background .1s}
.sp-item:hover{background:#eef4ff}
.sp-item.sel{background:#e8f0fe;border-left:3px solid #1a73e8}
.sp-item-title{font-size:12px;font-weight:600;color:#1a1a2e}
.sp-item-sub{font-size:10px;color:#888;margin-top:2px}
.sp-empty{padding:30px;text-align:center;color:#bbb;font-size:12px}
.sp-act{display:flex;gap:8px;flex-wrap:wrap;padding:12px 18px;border-top:1px solid #e0e4ea;align-items:center}
.sp-log{font-size:11px;color:#555;padding:6px 10px;background:#f8f9fb;border-radius:4px;max-height:80px;overflow-y:auto;font-family:monospace;white-space:pre-wrap;flex:1}
"""
assert "</style>" in html
html = html.replace("</style>", survey_css + "</style>", 1)

# 3. Update welcome message open buttons
old_btns = """        <button class="btn btn-pri" onclick="openMeta()">📂 Mở Metadata</button>
        <button class="btn btn-sec" onclick="openDT()">📂 Mở Datatable</button>"""
new_btns = """        <button class="btn btn-pri" onclick="openSurveyPanel()">🔍 Chọn Survey</button>
        <button class="btn btn-sec" onclick="openDT()">📂 Load Datatable</button>"""
if old_btns in html:
    html = html.replace(old_btns, new_btns, 1)

# 4. Add survey modal HTML before preview panel
survey_modal = """<!-- Survey panel modal -->
<div class="sp-ov" id="surveyPanel" style="display:none">
  <div class="sp-dlg">
    <div class="sp-hd">
      <strong>🔍 Chọn Survey</strong>
      <button class="mbtn-close" onclick="closeSurveyPanel()">&#x2715;</button>
    </div>
    <div class="sp-body">
      <div class="sp-row">
        <input class="sp-inp" id="spSearch" placeholder="Tìm tên survey…" onkeydown="if(event.key==='Enter')doSurveySearch()">
        <button class="btn btn-pri btn-sm" onclick="doSurveySearch()">🔍 Tìm</button>
      </div>
      <div class="sp-res" id="spResults">
        <div class="sp-empty">Nhập từ khóa và nhấn Tìm</div>
      </div>
    </div>
    <div class="sp-act" id="spActions" style="display:none">
      <button class="btn btn-pri btn-sm" onclick="doFetch()" id="spFetchBtn">📥 Fetch từ QMe</button>
      <button class="btn btn-sec btn-sm" onclick="doIngest()" id="spIngestBtn" disabled>&#x2699; Ingest</button>
      <button class="btn btn-suc btn-sm" onclick="doSelectSurvey()" id="spSelectBtn" disabled>&#x2713; Load &amp; Đóng</button>
      <div class="sp-log" id="spLog" style="display:none"></div>
    </div>
  </div>
</div>

"""
assert "<!-- Preview panel -->" in html
html = html.replace("<!-- Preview panel -->", survey_modal + "<!-- Preview panel -->", 1)

# 5. Remove hidden metaIn file input (keep dtIn and rdIn)
old_inputs = """<!-- Hidden file inputs -->
<input type="file" id="metaIn" accept=".json" style="display:none" onchange="onMetaIn(event)">
<input type="file" id="dtIn"   accept=".json" style="display:none" onchange="onDTIn(event)">
<input type="file" id="rdIn"   accept=".csv"  style="display:none" onchange="onRdIn(event)">"""
new_inputs = """<!-- Hidden file inputs (local fallback for datatable/csv) -->
<input type="file" id="dtIn"   accept=".json" style="display:none" onchange="onDTIn(event)">
<input type="file" id="rdIn"   accept=".csv"  style="display:none" onchange="onRdIn(event)">"""
if old_inputs in html:
    html = html.replace(old_inputs, new_inputs, 1)

# 6. Add API JS at end of script
api_js = r"""

// ════════════════════════════════════════════════════
// API INTEGRATION
// ════════════════════════════════════════════════════
let currentSurveyId   = null;
let currentSurveyName = null;
let lastVersion       = null;
let spSelected        = null;

// ── survey panel ──────────────────────────────────

function openSurveyPanel() {
  document.getElementById('surveyPanel').style.display = 'flex';
  const inp = document.getElementById('spSearch');
  inp.value = '';
  inp.focus();
}
function closeSurveyPanel() {
  document.getElementById('surveyPanel').style.display = 'none';
}

async function doSurveySearch() {
  const q = document.getElementById('spSearch').value.trim();
  const res = document.getElementById('spResults');
  res.innerHTML = '<div class="sp-empty">Đang tìm…</div>';
  try {
    const data = await fetch(`/api/surveys/search?q=${encodeURIComponent(q)}&limit=50`).then(r => r.json());
    const surveys = data.surveys || data.results || (Array.isArray(data) ? data : []);
    if (!surveys.length) { res.innerHTML = '<div class="sp-empty">Không tìm thấy survey nào</div>'; return; }
    res.innerHTML = surveys.map((s) => {
      const id    = s.survey_id || s.id;
      const title = s.display_title || s.title || s.name || `Survey ${id}`;
      const sub   = [s.status, s.question_count ? `${s.question_count} câu` : ''].filter(Boolean).join(' · ');
      return `<div class="sp-item" data-id="${id}" data-title="${title.replace(/"/g,'&quot;')}" onclick="selectSurveyItem(this)">
        <div class="sp-item-title">[${id}] ${title}</div>
        ${sub ? `<div class="sp-item-sub">${sub}</div>` : ''}
      </div>`;
    }).join('');
    document.getElementById('spActions').style.display = 'flex';
    document.getElementById('spFetchBtn').disabled = true;
    document.getElementById('spIngestBtn').disabled = true;
    document.getElementById('spSelectBtn').disabled = true;
    document.getElementById('spLog').style.display = 'none';
  } catch(e) {
    res.innerHTML = `<div class="sp-empty">Lỗi: ${e.message}</div>`;
  }
}

function selectSurveyItem(el) {
  document.querySelectorAll('.sp-item').forEach(x => x.classList.remove('sel'));
  el.classList.add('sel');
  spSelected = { id: parseInt(el.dataset.id), title: el.dataset.title };
  document.getElementById('spActions').style.display = 'flex';
  document.getElementById('spFetchBtn').disabled = false;
  checkSpStatus();
}

async function checkSpStatus() {
  if (!spSelected) return;
  try {
    const st = await fetch(`/api/surveys/${spSelected.id}/status`).then(r => r.json());
    document.getElementById('spIngestBtn').disabled = !st.has_mcp;
    document.getElementById('spSelectBtn').disabled = !st.has_data;
    _spLog(
      `fetch: ${st.has_mcp ? '✓' : '—'}  ingest: ${st.has_data ? '✓' : '—'}  datatable: ${st.has_datatable ? '✓' : '—'}  versions: ${st.versions.join(', ') || '—'}`
    );
  } catch(e) {}
}

function _spLog(msg) {
  const el = document.getElementById('spLog');
  el.style.display = 'block';
  el.textContent = msg;
}

async function doFetch() {
  if (!spSelected) return;
  document.getElementById('spFetchBtn').disabled = true;
  _spLog(`Đang fetch survey ${spSelected.id} từ QMe…`);
  try {
    const r = await fetch(`/api/surveys/${spSelected.id}/fetch`, {method:'POST'});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'fetch failed');
    _spLog(`✓ Fetch xong: ${d.pages} trang · ${d.total_rows} rows`);
    document.getElementById('spIngestBtn').disabled = false;
  } catch(e) {
    _spLog(`✗ Lỗi fetch: ${e.message}`);
  } finally {
    document.getElementById('spFetchBtn').disabled = false;
  }
}

async function doIngest() {
  if (!spSelected) return;
  document.getElementById('spIngestBtn').disabled = true;
  _spLog(`Đang ingest survey ${spSelected.id}…`);
  try {
    const r = await fetch(`/api/surveys/${spSelected.id}/ingest`, {method:'POST'});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'ingest failed');
    _spLog(`✓ Ingest xong: ${d.n_rows} rows`);
    document.getElementById('spSelectBtn').disabled = false;
  } catch(e) {
    _spLog(`✗ Lỗi ingest: ${e.message}`);
  } finally {
    document.getElementById('spIngestBtn').disabled = false;
  }
}

async function doSelectSurvey() {
  if (!spSelected) return;
  closeSurveyPanel();
  await activateSurvey(spSelected.id, spSelected.title);
}

async function activateSurvey(id, title) {
  currentSurveyId   = id;
  currentSurveyName = title;

  const badge = document.getElementById('surveyBadge');
  badge.textContent = `[${id}] ${title}`;
  badge.className = 'fbadge ok';

  const st = await fetch(`/api/surveys/${id}/status`).then(r => r.json());

  if (st.has_mcp) {
    document.getElementById('fetchBadge').textContent = 'fetch: ✓';
    document.getElementById('fetchBadge').className = 'fbadge ok';
  }
  if (st.has_data) {
    document.getElementById('ingestBadge').textContent = 'ingest: ✓';
    document.getElementById('ingestBadge').className = 'fbadge ok';
  }

  if (st.has_data) {
    await apiLoadMeta(id);
    await apiLoadRd(id);
  }
  if (st.has_datatable) {
    await apiLoadDT(id);
  } else {
    document.getElementById('loadDtBtn').disabled = false;
  }
  if (st.versions.length) {
    lastVersion = st.versions[st.versions.length - 1];
    const dlBtn = document.getElementById('dlServerBtn');
    dlBtn.disabled = false;
    dlBtn.dataset.version = lastVersion;
  }
  document.getElementById('pipelineBtn').disabled = !st.has_data;
  setStatus(`Survey [${id}] đã load`, 'saved');
}

// ── API loaders ───────────────────────────────────

async function apiLoadMeta(id) {
  try {
    const res = await fetch(`/api/surveys/${id}/metadata`);
    if (!res.ok) return;
    parseMeta(JSON.stringify(await res.json()), `${id}_metadata.json`);
    document.getElementById('fetchBadge').textContent = 'meta: ✓';
    document.getElementById('fetchBadge').className = 'fbadge ok';
  } catch(e) { setStatus('⚠ Metadata lỗi: ' + e.message, ''); }
}

async function apiLoadRd(id) {
  try {
    const res = await fetch(`/api/surveys/${id}/rawdata`);
    if (!res.ok) return;
    loadRd(await res.text(), `${id}_rawdata.csv`);
    document.getElementById('ingestBadge').textContent = 'csv: ✓';
    document.getElementById('ingestBadge').className = 'fbadge ok';
  } catch(e) { setStatus('⚠ Rawdata lỗi: ' + e.message, ''); }
}

async function apiLoadDT(id) {
  try {
    const res = await fetch(`/api/surveys/${id}/datatable`);
    if (!res.ok) return;
    loadDT(JSON.stringify(await res.json()), `${id}_datatable.json`);
    document.getElementById('loadDtBtn').disabled = false;
  } catch(e) {}
}

// ── overrides ─────────────────────────────────────

function openMeta() { openSurveyPanel(); }

async function openDT() {
  if (currentSurveyId) { await apiLoadDT(currentSurveyId); }
  else { document.getElementById('dtIn').click(); }
}

function openRd() {
  if (currentSurveyId) { apiLoadRd(currentSurveyId); }
  else { document.getElementById('rdIn').click(); }
}

async function saveFile() {
  if (showRaw) syncRaw();
  const text = JSON.stringify(data, null, 2);
  if (!currentSurveyId) { dlFallback(text); return; }
  try {
    const res = await fetch(`/api/surveys/${currentSurveyId}/datatable`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: text,
    });
    if (!res.ok) throw new Error((await res.json()).detail || 'save failed');
    dirty = false;
    setStatus('Đ\xe3 lưu datatable.json', 'saved');
  } catch(e) {
    setStatus('⚠ Save lỗi: ' + e.message, '');
    dlFallback(text);
  }
}

// ── pipeline ──────────────────────────────────────

async function runPipeline() {
  if (!currentSurveyId) return;
  const btn = document.getElementById('pipelineBtn');
  btn.disabled = true;
  btn.textContent = '⏳ Running…';
  setStatus('Đang chạy pipeline…', '');
  try {
    await saveFile();
    const res = await fetch(`/api/surveys/${currentSurveyId}/run`, {method:'POST'});
    const d = await res.json();
    if (!res.ok) throw new Error(d.detail || 'run failed');
    lastVersion = d.version;
    const dlBtn = document.getElementById('dlServerBtn');
    dlBtn.disabled = false;
    dlBtn.dataset.version = d.version;
    setStatus(`✓ Pipeline xong: ${d.version}`, 'saved');
  } catch(e) {
    setStatus('⚠ Pipeline lỗi: ' + e.message, '');
  } finally {
    btn.disabled = false;
    btn.textContent = '⚙ Run';
  }
}

function downloadServerXlsx() {
  const btn = document.getElementById('dlServerBtn');
  const version = btn.dataset.version || lastVersion;
  if (currentSurveyId && version) {
    window.location = `/api/surveys/${currentSurveyId}/download/${version}`;
  }
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeSurveyPanel();
});
"""

assert "</script>" in html
# Replace only the LAST </script>
idx = html.rfind("</script>")
html = html[:idx] + api_js + "\n</script>" + html[idx + len("</script>"):]

DST.write_text(html, encoding="utf-8")
print(f"Written {len(html)} chars, {html.count(chr(10))} lines -> {DST}")
