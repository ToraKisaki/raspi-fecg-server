/* Shared helpers + components for the fECG touch UI. */
const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

function esc(s){
  return String(s ?? '').replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function fmtTime(ts){ return ts ? new Date(ts*1000).toLocaleString() : '—'; }
function fmtClock(ts){ return ts ? new Date(ts*1000).toLocaleTimeString() : '—'; }

async function api(path, opts = {}){
  const o = { headers: {}, ...opts };
  if (o.body && typeof o.body !== 'string'){ o.headers['Content-Type']='application/json'; o.body = JSON.stringify(o.body); }
  const r = await fetch(path, o);
  if (r.status === 401){ location.href = '/login'; throw new Error('unauthorized'); }
  let data = null; try { data = await r.json(); } catch(e){}
  return { ok: r.ok, status: r.status, data };
}

async function loadMe(){
  try{ const {ok, data} = await api('/api/me');
    if (ok) $$('[data-who]').forEach(el => el.textContent = data.name || '—');
  }catch(e){}
}

const ALARM = {
  ok:     {label:'Normal',            cls:'ok'},
  low:    {label:'Fetal bradycardia', cls:'low'},
  high:   {label:'Fetal tachycardia', cls:'high'},
  signal: {label:'Signal loss',       cls:'signal'},
};

/* ---- sparkline ---- */
function drawSpark(canvas, arr, color){
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 74, h = canvas.clientHeight || 34;
  canvas.width = w*dpr; canvas.height = h*dpr;
  const ctx = canvas.getContext('2d'); ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,w,h);
  if (!arr || arr.length < 2) return;
  let mx = 1e-6; for (const v of arr){ const a = Math.abs(v); if (a>mx) mx = a; }
  ctx.strokeStyle = color; ctx.lineWidth = 1.3; ctx.lineJoin='round'; ctx.beginPath();
  for (let i=0;i<arr.length;i++){
    const x = w*i/(arr.length-1);
    const y = h/2 - (arr[i]/mx)*(h/2*0.85);
    i ? ctx.lineTo(x,y) : ctx.moveTo(x,y);
  }
  ctx.stroke();
}

/* ---- duration helper ---- */
function fmtDur(sec){
  if (sec == null || !isFinite(sec) || sec < 0) return '—';
  sec = Math.round(sec);
  const h = Math.floor(sec/3600), m = Math.floor((sec%3600)/60), s = sec%60;
  return (h?`${h}h `:'') + (h||m?`${m}m `:'') + `${s}s`;
}

/* ---- FHR/MHR trend chart (vanilla canvas, used by patient view + report) ----
   metrics: [[t,fhr,mhr,sq,alarm], ...] (t seconds). events optional. */
function drawTrendChart(canvas, metrics, opts){
  opts = opts || {};
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.clientWidth || 600, H = canvas.clientHeight || 220;
  canvas.width = W*dpr; canvas.height = H*dpr;
  const ctx = canvas.getContext('2d'); ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,W,H);
  const cs = getComputedStyle(document.body);
  const colFe = (cs.getPropertyValue('--fecg').trim()||'#ff5c9d');
  const colRaw = (cs.getPropertyValue('--raw').trim()||'#22d3ee');
  if (!metrics || metrics.length < 2){
    ctx.fillStyle = cs.getPropertyValue('--muted').trim()||'#90a0ba';
    ctx.font = '13px system-ui'; ctx.textAlign='center';
    ctx.fillText('No trend data yet', W/2, H/2);
    return;
  }
  const padL=34, padR=10, padT=10, padB=18;
  const x0=padL, x1=W-padR, y0=padT, y1=H-padB;
  const t0 = metrics[0][0], tN = metrics[metrics.length-1][0];
  const span = Math.max(1e-3, tN - t0);
  const yLo = 50, yHi = 210;                       // bpm axis
  const X = t => x0 + (x1-x0)*((t-t0)/span);
  const Y = v => y1 - (y1-y0)*((v-yLo)/(yHi-yLo));
  // normal fetal band 110-160
  ctx.fillStyle = 'rgba(52,211,153,.08)';
  ctx.fillRect(x0, Y(160), x1-x0, Y(110)-Y(160));
  // gridlines + y labels
  ctx.strokeStyle='rgba(255,255,255,.06)'; ctx.fillStyle='rgba(144,160,186,.8)';
  ctx.font='9px system-ui'; ctx.textAlign='right'; ctx.lineWidth=1;
  for (let v=60; v<=200; v+=20){
    const y=Y(v); ctx.beginPath(); ctx.moveTo(x0,y); ctx.lineTo(x1,y); ctx.stroke();
    ctx.fillText(v, x0-4, y+3);
  }
  // alarm event markers (vertical lines)
  for (const e of (opts.events||[])){
    if (e.kind!=='alarm') continue;
    const x=X(e.t);
    ctx.strokeStyle = /brady|tachy/i.test(e.label) ? 'rgba(251,81,96,.5)' :
                      /signal/i.test(e.label) ? 'rgba(251,191,36,.5)' : 'rgba(91,140,255,.35)';
    ctx.beginPath(); ctx.moveTo(x,y0); ctx.lineTo(x,y1); ctx.stroke();
  }
  // line drawer that breaks on null
  const line = (idx, color) => {
    ctx.strokeStyle=color; ctx.lineWidth=1.6; ctx.lineJoin='round'; ctx.beginPath();
    let pen=false;
    for (const r of metrics){
      const v=r[idx];
      if (v==null){ pen=false; continue; }
      const x=X(r[0]), y=Y(v);
      pen ? ctx.lineTo(x,y) : ctx.moveTo(x,y); pen=true;
    }
    ctx.stroke();
  };
  line(2, colRaw);   // MHR
  line(1, colFe);    // FHR
  // x time labels (start / end relative minutes)
  ctx.fillStyle='rgba(144,160,186,.8)'; ctx.textAlign='left';
  ctx.fillText('0:00', x0, y1+12);
  ctx.textAlign='right';
  ctx.fillText(fmtDur(span).replace(/\s+/g,''), x1, y1+12);
}

/* ---- toast ---- */
let _toastT;
function toast(msg, isErr=false){
  let t = $('#toast');
  if (!t){ t = document.createElement('div'); t.id='toast'; t.className='toast'; document.body.appendChild(t); }
  t.textContent = msg; t.className = 'toast' + (isErr?' err':'');
  requestAnimationFrame(()=> t.classList.add('show'));
  clearTimeout(_toastT); _toastT = setTimeout(()=> t.classList.remove('show'), 2600);
}

/* ---- patient add/edit bottom sheet ---- */
function _ensureSheet(){
  if ($('#scrim')) return;
  const scrim = document.createElement('div'); scrim.id='scrim'; scrim.className='scrim';
  const sheet = document.createElement('div'); sheet.id='sheet'; sheet.className='sheet';
  document.body.append(scrim, sheet);
  scrim.addEventListener('click', closeSheet);
}
function closeSheet(){
  const s = $('#sheet'), sc = $('#scrim');
  if (s) s.classList.remove('show'); if (sc) sc.classList.remove('show');
}
/* p = existing patient (edit) or null (create). onSaved(patient) called on success. */
function openPatientSheet(p, onSaved){
  _ensureSheet();
  const editing = !!p; p = p || {};
  $('#sheet').innerHTML = `
    <h2>${editing ? 'Edit patient' : 'New patient'}</h2>
    <div class="field">
      <label>Patient ID${editing?'':' *'}</label>
      <input id="f_id" value="${esc(p.id||'')}" ${editing?'disabled':''} placeholder="e.g. P004" autocapitalize="characters">
      <div class="err" id="f_err"></div>
    </div>
    <div class="field"><label>Full name</label><input id="f_full_name" value="${esc(p.full_name||'')}" placeholder="Full name"></div>
    <div class="grid2">
      <div class="field"><label>MRN</label><input id="f_mrn" value="${esc(p.mrn||'')}"></div>
      <div class="field"><label>Gender</label>
        <select id="f_gender">
          ${['','F','M','Other'].map(o=>`<option ${(p.gender||'')===o?'selected':''}>${o}</option>`).join('')}
        </select></div>
    </div>
    <div class="grid2">
      <div class="field"><label>Date of birth</label><input id="f_date_of_birth" type="date" value="${esc(p.date_of_birth||'')}"></div>
      <div class="field"><label>Citizen ID</label><input id="f_citizen_id" value="${esc(p.citizen_id||'')}"></div>
    </div>
    <div class="field"><label>Address</label><input id="f_address" value="${esc(p.address||'')}" placeholder="Street, district, city"></div>
    <div class="field"><label>Notes / gestation</label><textarea id="f_notes" placeholder="e.g. 32 weeks gestation">${esc(p.notes||'')}</textarea></div>
    <div class="row">
      <button class="btn ghost" id="f_cancel">Cancel</button>
      <button class="btn" id="f_save">${editing?'Save':'Create'}</button>
    </div>`;
  $('#scrim').classList.add('show'); requestAnimationFrame(()=> $('#sheet').classList.add('show'));
  $('#f_cancel').onclick = closeSheet;
  $('#f_save').onclick = async () => {
    const body = {
      full_name: $('#f_full_name').value.trim(), mrn: $('#f_mrn').value.trim(),
      gender: $('#f_gender').value, date_of_birth: $('#f_date_of_birth').value,
      citizen_id: $('#f_citizen_id').value.trim(), address: $('#f_address').value.trim(),
      notes: $('#f_notes').value.trim(),
    };
    let res;
    if (editing){
      res = await api('/api/patients/'+encodeURIComponent(p.id), {method:'PATCH', body});
    } else {
      body.id = $('#f_id').value.trim();
      if (!body.id){ $('#f_err').textContent = 'Patient ID is required'; return; }
      res = await api('/api/patients', {method:'POST', body});
    }
    if (res.ok){ closeSheet(); toast(editing?'Patient updated':'Patient created'); onSaved && onSaved(res.data); }
    else if (res.status === 409){ $('#f_err').textContent = (res.data && res.data.detail) || 'ID already exists'; }
    else { toast('Could not save patient', true); }
  };
}
