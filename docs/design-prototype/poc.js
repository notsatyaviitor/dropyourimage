/* ============================================================
   Dropyourimage — POC JavaScript
   Separate file for production reuse
   ============================================================ */

/*
 * ── Demo product images — feature-centric selection ─────────────
 *
 * Each pair is chosen to demonstrate the 4 POC features:
 *   1. Background removal  — "before" has a visible, distracting bg
 *   2. BG colour replace   — "after" sits on pure white ready for hex fill
 *   3. Resize to 500×500   — subject fills the frame consistently
 *   4. Auto-centre         — subject is centred after crop
 *
 * Product categories match dropyourimage's actual client verticals:
 *   Footwear · Jewellery · Beauty & Care · Fashion accessories
 */
const SAMPLES = [
  {
    id: 'S1',
    name: 'nike-sneaker-footwear.jpg',
    size: 2460000, isSample: true, status: 'Uploaded ✓',
    // FOOTWEAR — iconic bg-removal use case.
    // Before: dramatic red/blue studio splash — exactly the kind of
    // creative bg a brand shoots for social but needs stripped for PDP.
    // After: same shoe type isolated on pure white, centered.
    beforeUri: 'https://images.unsplash.com/photo-1542291026-7eec264c27ff?w=600&fit=crop&q=80',
    afterUri:  'https://images.unsplash.com/photo-1595950653106-6c9ebd614d3a?w=600&fit=crop&q=80',
  },
  {
    id: 'S2',
    name: 'diamond-ring-jewellery.jpg',
    size: 1740000, isSample: true, status: 'Uploaded ✓',
    // JEWELLERY — high-value products always need clean white-bg shots.
    // Before: ring on dark velvet — typical jeweller's prop shot that
    // hides the stone and kills the listing conversion rate.
    // After: ring isolated on white, stone catches the light perfectly.
    beforeUri: 'https://images.unsplash.com/photo-1515562141207-7a88fb7ce338?w=600&fit=crop&q=80',
    afterUri:  'https://images.unsplash.com/photo-1605100804763-247f67b3557e?w=600&fit=crop&q=80',
  },
  {
    id: 'S3',
    name: 'chanel-perfume-beauty.jpg',
    size: 2050000, isSample: true, status: 'Uploaded ✓',
    // BEAUTY & CARE — cosmetics brands need standardised white-bg shots
    // for retailers (Bol, Amazon, Sephora) who enforce strict PDP rules.
    // Before: artistic dark/moody bottle shot — beautiful for campaign,
    // rejected by every marketplace template.
    // After: bottle isolated on white, label clearly readable.
    beforeUri: 'https://images.unsplash.com/photo-1557683311-eac922347aa1?w=600&fit=crop&q=80',
    afterUri:  'https://images.unsplash.com/photo-1541643600914-78b084683702?w=600&fit=crop&q=80',
  },
  {
    id: 'S4',
    name: 'leather-handbag-fashion.jpg',
    size: 3120000, isSample: true, status: 'Uploaded ✓',
    // FASHION ACCESSORIES — bags photographed in styled shoots always
    // end up with busy backgrounds that need stripping before going live.
    // Before: styled prop shot — looks great in editorial, unusable on PDP.
    // After: bag on clean white, shape and stitching fully visible.
    beforeUri: 'https://images.unsplash.com/photo-1548036328-c9fa89d128fa?w=600&fit=crop&q=80',
    afterUri:  'https://images.unsplash.com/photo-1584917865442-de89df76afd3?w=600&fit=crop&q=80',
  },
];

/* ── App state ──────────────────────────────────────────────── */
const POC = {
  step: 1,
  spec: { bgRemoval:true, bgColor:'#ffffff', outputW:500, outputH:500, centerImage:true },
  method: 'web',
  files: [...SAMPLES],
  results: [],
};

/* ── Step navigation ────────────────────────────────────────── */
function goToStep(n) {
  if (n === POC.step) return;
  if (POC.step === 1) _collectSpec();
  POC.step = n;
  _renderStep();
}

function _renderStep() {
  document.querySelectorAll('.wizard-page').forEach(p => p.classList.remove('active'));
  const map = {1:'page-spec', 2:'page-method', 3:'page-upload', 4:'page-complete'};
  document.getElementById(map[POC.step]).classList.add('active');
  _updateProgressBar();
  if (POC.step === 3) { _renderFileList(); _refreshDetails(); }
  if (POC.step === 4) _runProcessing();
}

function _updateProgressBar() {
  for (let i = 1; i <= 4; i++) {
    const active = i <= POC.step;
    document.getElementById('sc'+i).classList.toggle('off', !active);
    document.getElementById('sl'+i).classList.toggle('off', !active);
    const ln = document.getElementById('ln'+i);
    if (ln) ln.classList.toggle('off', i >= POC.step);
  }
}

/* ── Spec form ──────────────────────────────────────────────── */
function _collectSpec() {
  const g = id => document.getElementById(id);
  POC.spec.bgRemoval   = g('tog-bg').checked;
  POC.spec.bgColor     = g('bg-hex').value || '#ffffff';
  POC.spec.outputW     = parseInt(g('out-w').value) || 500;
  POC.spec.outputH     = parseInt(g('out-h').value) || 500;
  POC.spec.centerImage = g('tog-center').checked;
}

function onToggle(toggleId, optionsId) {
  const checked = document.getElementById(toggleId).checked;
  document.getElementById(optionsId).classList.toggle('open', checked);
}

function onHexInput(hex, previewId, pickerId) {
  if (/^#[0-9A-Fa-f]{6}$/.test(hex)) {
    document.getElementById(previewId).style.backgroundColor = hex;
    document.getElementById(pickerId).value = hex;
  }
}

function onPickerChange(picker, hexId, previewId) {
  document.getElementById(hexId).value = picker.value;
  document.getElementById(previewId).style.backgroundColor = picker.value;
}

function triggerPicker(pickerId) { document.getElementById(pickerId).click(); }

/* ── Upload method ──────────────────────────────────────────── */
function selectMethod(method) {
  POC.method = method;
  document.querySelectorAll('.method-card').forEach(c => c.classList.remove('selected'));
  document.getElementById('mc-'+method).classList.add('selected');
  document.getElementById('sftp-fields').classList.toggle('open', method === 'sftp');
  _refreshDetails();
}

/* ── File handling ──────────────────────────────────────────── */
function initDropzone() {
  const dz = document.getElementById('upload-dz');
  if (!dz) return;
  dz.addEventListener('dragover',  e => { e.preventDefault(); dz.classList.add('drag-over'); });
  dz.addEventListener('dragleave', ()  => dz.classList.remove('drag-over'));
  dz.addEventListener('drop', e => {
    e.preventDefault(); dz.classList.remove('drag-over');
    _addFiles(Array.from(e.dataTransfer.files).filter(f => f.type.startsWith('image/')));
  });
  dz.addEventListener('click', () => document.getElementById('file-input').click());
}

function _addFiles(files) {
  files.forEach(file => {
    const url = URL.createObjectURL(file);
    POC.files.push({ id: Date.now()+Math.random(), file, url, beforeUri: url, afterUri: url, name: file.name, size: file.size, status: 'Ready', isSample: false });
  });
  _renderFileList(); _refreshDetails();
}

function removeFile(id) {
  const f = POC.files.find(x => String(x.id) === String(id));
  if (f && !f.isSample) URL.revokeObjectURL(f.url);
  POC.files = POC.files.filter(x => String(x.id) !== String(id));
  _renderFileList(); _refreshDetails();
}

function _renderFileList() {
  const list  = document.getElementById('file-list');
  const empty = document.getElementById('upload-empty');
  if (POC.files.length === 0) { empty.style.display='block'; list.innerHTML=''; return; }
  empty.style.display = 'none';
  list.innerHTML = POC.files.map(f => `
    <div class="file-item">
      <img class="file-thumb" src="${f.isSample ? f.beforeUri : f.url}" alt="">
      <div class="file-info">
        <div class="file-name">${_esc(f.name)}</div>
        <div class="file-size">${_fmtSize(f.size)}</div>
      </div>
      <div class="file-status">${_esc(f.status)}</div>
      <button class="file-remove" onclick="removeFile('${f.id}')" title="Remove">×</button>
    </div>`).join('');
}

function filterFiles(query) {
  document.querySelectorAll('.file-item').forEach((el, i) => {
    if (!POC.files[i]) return;
    el.style.display = POC.files[i].name.toLowerCase().includes(query.toLowerCase()) ? '' : 'none';
  });
}

function startUpload() {
  if (POC.files.length === 0) { alert('Please add at least one image first.'); return; }
  POC.files.forEach(f => { f.status = 'Uploading…'; });
  _renderFileList();
  setTimeout(() => { POC.files.forEach(f => { f.status = 'Uploaded ✓'; }); _renderFileList(); }, 1600);
}

/* ── Details panel ──────────────────────────────────────────── */
function _refreshDetails() {
  const set = (id,v) => { const el=document.getElementById(id); if(el) el.textContent=v; };
  set('d-method', POC.method === 'sftp' ? 'SFTP' : 'Web');
  set('d-count',  POC.files.length > 0 ? POC.files.length : '…');
  set('d-spec',   'POC Configuration');
  const now = new Date();
  set('d-date', `${String(now.getDate()).padStart(2,'0')}/${String(now.getMonth()+1).padStart(2,'0')}/${now.getFullYear()}`);
}

/* ── Processing ──────────────────────────────────────────────── */
function _runProcessing() {
  _collectSpec(); POC.results = [];
  const overlay = document.getElementById('proc-overlay');
  overlay.classList.add('open');

  const imgTotal = document.getElementById('proc-img-total');
  if (imgTotal) imgTotal.textContent = POC.files.length;

  _buildProcThumbs();
  _setProcProgress(0);
  _setProcText('Initialising AI engine…');

  const steps = [
    { id:1, label:'Removing backgrounds' },
    { id:2, label:'Applying background colour' },
    { id:3, label:`Resizing canvas to ${POC.spec.outputW}×${POC.spec.outputH} px` },
    { id:4, label:'Centring & exporting' },
  ];

  steps.forEach(s => {
    const icon  = document.getElementById('psr-icon-' + s.id);
    const badge = document.getElementById('psr-badge-' + s.id);
    const row   = document.getElementById('psr-' + s.id);
    if (icon)  icon.className  = 'psr-icon';
    if (badge) { badge.className = 'psr-badge'; badge.textContent = 'Queued'; }
    if (row)   row.classList.remove('active-step');
  });

  let i = 0;
  const tick = () => {
    if (i > 0) {
      const prev   = steps[i - 1];
      const pIcon  = document.getElementById('psr-icon-'  + prev.id);
      const pBadge = document.getElementById('psr-badge-' + prev.id);
      const pRow   = document.getElementById('psr-'       + prev.id);
      if (pIcon)  pIcon.className  = 'psr-icon done';
      if (pBadge) { pBadge.className = 'psr-badge done'; pBadge.textContent = 'Done'; }
      if (pRow)   pRow.classList.remove('active-step');
    }
    if (i < steps.length) {
      const cur    = steps[i];
      const cIcon  = document.getElementById('psr-icon-'  + cur.id);
      const cBadge = document.getElementById('psr-badge-' + cur.id);
      const cRow   = document.getElementById('psr-'       + cur.id);
      if (cIcon)  cIcon.className  = 'psr-icon running';
      if (cBadge) { cBadge.className = 'psr-badge running'; cBadge.textContent = 'Running'; }
      if (cRow)   cRow.classList.add('active-step');
      _setProcProgress((i / steps.length) * 100);
      _setProcText(cur.label + '…');
      _updateProcThumbs(i);
      i++;
      setTimeout(tick, 1100);
    } else {
      _setProcProgress(100);
      _setProcText('All images processed — packaging output files…');
      POC.files.forEach((_, idx) => _setThumbState(idx, 'done'));
      setTimeout(() => { overlay.classList.remove('open'); _buildResults(); _renderResults(); }, 900);
    }
  };
  setTimeout(tick, 500);
}

function _buildProcThumbs() {
  const row = document.getElementById('proc-thumb-row');
  if (!row) return;
  row.innerHTML = POC.files.map((f, idx) => `
    <div class="proc-thumb pending" id="proc-thumb-${idx}">
      <img src="${f.isSample ? f.beforeUri : (f.url || f.beforeUri)}" alt="">
      <div class="proc-thumb-overlay"></div>
    </div>`).join('');
}

function _setThumbState(idx, state) {
  const t = document.getElementById('proc-thumb-' + idx);
  if (t) t.className = 'proc-thumb ' + state;
}

function _updateProcThumbs(stepIdx) {
  const n = POC.files.length;
  if (stepIdx === 0) {
    _setThumbState(0, 'running');
  } else if (stepIdx === 1) {
    for (let k = 0; k < n; k++) _setThumbState(k, 'running');
  } else if (stepIdx === 2) {
    _setThumbState(0, 'done');
    if (n > 1) _setThumbState(1, 'running');
  } else {
    for (let k = 0; k < n; k++) _setThumbState(k, 'done');
  }
}

function _setProcProgress(pct) {
  const ring = document.getElementById('proc-ring-fill');
  const bar  = document.getElementById('proc-prog-fill');
  const num  = document.getElementById('proc-pct-num');
  const circ = 314.16;
  if (ring) ring.style.strokeDashoffset = circ - (pct / 100) * circ;
  if (bar)  bar.style.width = pct + '%';
  if (num)  num.textContent = Math.round(pct);
}

function _setProcText(text) {
  const el = document.getElementById('proc-prog-text');
  if (el) el.textContent = text;
}

function _buildResults() {
  const src = POC.files.length > 0 ? POC.files : SAMPLES;
  POC.results = src.map(f => ({
    id: f.id, name: f.name,
    beforeUri: f.beforeUri || f.url || null,
    afterUri:  f.afterUri  || f.url || null,
    size: `${POC.spec.outputW}×${POC.spec.outputH}`,
    bgR: POC.spec.bgRemoval, bgC: POC.spec.bgColor,
    cent: POC.spec.centerImage,
    status: 'auto',
  }));
}

function _renderResults() {
  _renderSummaryStrip();
  _renderBeforeAfter();
  _renderResultGrid();
}

function _renderSummaryStrip() {
  const el = document.getElementById('proc-summary');
  if (!el) return;
  const autoCount = POC.results.filter(r => r.status === 'auto').length;
  const items = [
    { label:'In Batch',       val: POC.results.length,                              cls:'',          sub:'images total' },
    { label:'Backgrounds',    val: POC.spec.bgRemoval ? 'Removed' : 'Preserved',   cls:'val-green val-text', sub:'AI segmentation' },
    { label:'Output Size',    val:`${POC.spec.outputW}×${POC.spec.outputH}`,        cls:'val-text',  sub:'px · JPEG & PNG' },
    { label:'Auto-processed', val: autoCount,                                       cls:'val-green', sub:`of ${POC.results.length} images` },
  ];
  el.innerHTML = items.map(it => `
    <div class="proc-summary-item">
      <span class="proc-summary-label">${it.label}</span>
      <span class="proc-summary-val ${it.cls}">${it.val}</span>
      <span class="proc-summary-sub">${it.sub}</span>
    </div>`).join('');
}

function _renderBeforeAfter() {
  const row = document.getElementById('ba-row');
  if (!row) return;
  const isCol = POC.spec.bgRemoval && POC.spec.bgColor && POC.spec.bgColor.toLowerCase() !== '#ffffff';
  const bgSt  = isCol ? ` style="background-color:${_esc(POC.spec.bgColor)}"` : '';
  row.innerHTML = POC.results.map(r => `
    <div class="ba-card">
      <div class="ba-card-label-row">
        <div class="ba-label before">Before</div>
        <div class="ba-label after">After</div>
      </div>
      <div class="ba-imgs">
        <div class="ba-img-wrap before"><img class="ba-img" src="${r.beforeUri||''}" alt=""></div>
        <div class="ba-img-wrap after${isCol?' colored':''}"${bgSt}><img class="ba-img" src="${r.afterUri||''}" alt=""></div>
      </div>
      <div class="ba-card-name">${_esc(r.name)}</div>
    </div>`).join('');
}

function _renderResultGrid() {
  const grid = document.getElementById('result-grid');
  if (!grid) return;

  const total     = POC.results.length;
  const autoCount = POC.results.filter(r => r.status === 'auto').length;

  const _set = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
  _set('batch-img-count', total);
  _set('fpn-all',   total);
  _set('fpn-auto',  autoCount);
  _set('ftn-jpeg',  total);
  _set('ftn-png',   total);

  const isCol = POC.spec.bgRemoval && POC.spec.bgColor && POC.spec.bgColor.toLowerCase() !== '#ffffff';
  const bgSt  = isCol ? ` style="background-color:${_esc(POC.spec.bgColor)}"` : '';

  grid.innerHTML = POC.results.map(r => {
    const isAuto    = r.status === 'auto';
    const shortName = r.name.replace(/\.[^.]+$/, '').substring(0, 20);
    return `
      <div class="result-card" data-status="${r.status}">
        <div class="result-card-status-badge ${isAuto ? 'badge-auto' : 'badge-review'}">${isAuto ? 'AUTO-PASS' : 'REVIEW'}</div>
        <div class="result-card-img-wrap${isCol ? ' colored' : ''}"${bgSt}>
          <img class="result-card-img" src="${r.afterUri || ''}" alt="${_esc(r.name)}">
        </div>
        <div class="result-card-footer">
          <span class="result-card-id" title="${_esc(r.name)}">${_esc(shortName)}</span>
          <span class="result-type-chip">JPG</span>
          <span class="result-type-chip">PNG</span>
        </div>
      </div>`;
  }).join('');
}

function setResultFilter(btn) {
  document.querySelectorAll('.fpill').forEach(p => p.classList.remove('active'));
  btn.classList.add('active');
  const filter = btn.dataset.filter;
  document.querySelectorAll('#result-grid .result-card').forEach(card => {
    card.style.display = (filter === 'all' || card.dataset.status === filter) ? '' : 'none';
  });
}

function setTypeFilter(btn) {
  document.querySelectorAll('.ftpill').forEach(p => p.classList.remove('active'));
  btn.classList.add('active');
}

function downloadResults() {
  alert('Download ready!\n\nIn the production platform this button streams a ZIP archive containing your processed JPEG and PNG files at '+POC.spec.outputW+'×'+POC.spec.outputH+' px.');
}

/* ── Helpers ────────────────────────────────────────────────── */
function _esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function _fmtSize(b){if(b<1024)return b+' B';if(b<1048576)return(b/1024).toFixed(1)+' KB';return(b/1048576).toFixed(1)+' MB';}

/* ── Init ───────────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  initDropzone();
  const fi = document.getElementById('file-input');
  if (fi) fi.addEventListener('change', e => { _addFiles(Array.from(e.target.files).filter(f=>f.type.startsWith('image/'))); fi.value=''; });
  const si = document.getElementById('search-input');
  if (si) si.addEventListener('input', e => filterFiles(e.target.value));
  const picker = document.getElementById('bg-picker');
  if (picker) picker.addEventListener('input', e => onPickerChange(e.target,'bg-hex','bg-preview'));
  _renderFileList();
  _refreshDetails();
  _updateProgressBar();
});
