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
    if (ok) $$('[data-who]').forEach(el => el.textContent = data.name || data.username);
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
    <div class="field"><label>Name</label><input id="f_name" value="${esc(p.name||'')}" placeholder="Full name"></div>
    <div class="grid2">
      <div class="field"><label>MRN</label><input id="f_mrn" value="${esc(p.mrn||'')}"></div>
      <div class="field"><label>Sex</label>
        <select id="f_sex">
          ${['','F','M','Other'].map(o=>`<option ${(p.sex||'')===o?'selected':''}>${o}</option>`).join('')}
        </select></div>
    </div>
    <div class="field"><label>Date of birth</label><input id="f_dob" type="date" value="${esc(p.dob||'')}"></div>
    <div class="field"><label>Notes / gestation</label><textarea id="f_notes" placeholder="e.g. 32 weeks gestation">${esc(p.notes||'')}</textarea></div>
    <div class="row">
      <button class="btn ghost" id="f_cancel">Cancel</button>
      <button class="btn" id="f_save">${editing?'Save':'Create'}</button>
    </div>`;
  $('#scrim').classList.add('show'); requestAnimationFrame(()=> $('#sheet').classList.add('show'));
  $('#f_cancel').onclick = closeSheet;
  $('#f_save').onclick = async () => {
    const body = {
      name: $('#f_name').value.trim(), mrn: $('#f_mrn').value.trim(),
      sex: $('#f_sex').value, dob: $('#f_dob').value, notes: $('#f_notes').value.trim(),
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
