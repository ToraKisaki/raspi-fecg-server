# fECG Web App — Workflow, Usage & UI

This document logs how the **clinician web app** (`server/`) works: who uses it,
the end-to-end workflow, every screen, the API/WebSocket surface, the data model,
and the device → server → dashboard dataflow. It also records the redesign that
made the UI run on the Pi's own **3.5" 480×320 touchscreen** and added live
fetal/maternal heart rate, alarms, and in-app patient management.

> Research/educational software — **not a certified medical device.**

---

## 1. Purpose & actors

The Raspberry Pi 5 reads a single abdominal ECG lead (ADS1293, 250 Hz), runs the
UNETR INT8 model (`infer.py`) to extract the **fetal ECG** from the maternal
abdominal signal, and streams `(t, raw, fecg)` to a FastAPI + SQLite backend. The
web app is the clinician's window onto that stream.

| Actor | Uses it for |
|-------|-------------|
| **Clinician / midwife** | Watch live fetal + maternal heart rate and ECG traces, get alarmed on abnormal FHR, manage the patient list. |
| **Bedside device (Pi)** | Authenticates and streams samples in; also *hosts* the web UI on its own LCD. |

**Display target:** the dashboard is shown in a kiosk browser on the Pi's 3.5"
**480×320** resistive touchscreen — the same panel the PyQt5 device monitor uses.
The whole UI is designed 480×320-first and touch-first (it still scales up if
opened from another browser on the LAN).

---

## 2. End-to-end workflow

```
            ┌─────────────── Raspberry Pi 5 ───────────────┐
 ADS1293 ──▶│ acquisition → infer.py (fECG) → Uploader (WS) │──┐
 250 Hz     └──────────────────────────────────────────────┘  │  ws://…/ws/device
                                                               ▼
                                   ┌──────── FastAPI server (app.py) ────────┐
                                   │ • auth (cookie sessions)                │
                                   │ • PatientAnalyzer → FHR / MHR / SQ /    │
                                   │   alarm  (analysis.py, server-side)     │
                                   │ • Hub fan-out + 1-in-4 persist (SQLite) │
                                   └───────┬───────────────────────┬─────────┘
                          ws://…/ws/live/{id}                REST /api/*
                                   ▼                               ▼
                         ┌──────── Web UI on the Pi's 480×320 touchscreen ────────┐
                         │ Login → Dashboard (patient list) → Patient (live view) │
                         └────────────────────────────────────────────────────────┘
```

1. **Sign in.** Clinician opens `/login` and authenticates (demo: `doctor` /
   `password`). A httponly session cookie is set.
2. **Dashboard (`/`).** A touch list of patients. Each row shows live status, the
   **fetal HR**, a fECG **sparkline**, the maternal HR, and turns red when an
   alarm is active. The list refreshes every 2 s. Tap **+** to add a patient;
   type in the search box to filter by name / ID / MRN.
3. **Patient view (`/patient/{id}`).** Two tabs:
   - **Monitor** — big **Fetal HR** and **Maternal HR** readouts, a signal-quality
     bar, an **alarm banner** (bradycardia / tachycardia / signal loss), and the
     scrolling **raw** + **fECG** traces with a grid. A **freeze** button holds the
     waveform for inspection.
   - **Info** — patient details, recorded-sample and session counts, the session
     history table, and **Edit** / **Archive** actions.
4. **Device side.** When a device connects (`/ws/device`) it auto-creates the
   patient if unknown, opens a session, and its samples immediately appear live.

---

## 3. Screens

### Login (`static/login.html`)
Centered card, large touch inputs, full-width **Sign in** button, inline error +
loading state, Enter-to-submit. Demo credentials hinted.

### Dashboard (`static/dashboard.html`)
- **Top bar:** title, signed-in clinician, **+ Add patient**, sign out.
- **Search:** filters the list client-side (name / ID / MRN).
- **Patient rows (touch cards):** status dot · name · `ID • MRN • mat HR` ·
  fECG sparkline · big **fetal BPM**. Alarming patients sort to the top and are
  tinted red (FHR out of range) or carry a warning accent (signal loss).
- **Add/Edit** opens a bottom **sheet** form (`app.js`): ID, name, MRN, sex, DOB,
  notes/gestation. Duplicate IDs are rejected.

### Patient view (`static/patient.html`)
- **Monitor tab:** alarm banner · Fetal HR tile (with `normal 110–160` and a
  signal-quality bar) · Maternal HR tile · dual scrolling traces (raw cyan, fECG
  pink) with grid and auto-scaling · **freeze**.
- **Info tab:** key/value details · sample & session stats · session table ·
  Edit / Archive.
- Live updates arrive over the patient WebSocket; metrics and waveform update
  together. Reconnects automatically; pings every 25 s to keep the socket alive.

---

## 4. API & WebSocket surface (`server/app.py`)

**Pages** (HTML, redirect to `/login` if unauthenticated): `/login`, `/`
(dashboard), `/patient/{id}`.

**Auth:** `POST /login` (form) → sets `session` cookie · `POST /logout`.

**REST (JSON, require login):**
| Method & path | Purpose |
|---|---|
| `GET /api/me` | current clinician |
| `GET /api/patients` | list (+ live `streaming, fhr, mhr, sq, alarm, spark`) |
| `POST /api/patients` | create (409 on duplicate id) |
| `PATCH /api/patients/{id}` | edit name/mrn/sex/dob/notes |
| `POST /api/patients/{id}/archive` | archive / un-archive (`{archived: bool}`) |
| `GET /api/patients/{id}` | detail + `stats` + `recent` history + `live` metrics |

**WebSockets:**
- `/ws/device` — device ingest. `hello` `{patient_id, token, sample_rate}` →
  token checked against `DEVICE_TOKEN`, bad token closes; replies `ack
  {session_id}`. Then `samples {data:[[t,raw,fecg],…]}`. The server runs each
  batch through the analyzer, broadcasts, and persists 1-in-4 samples.
- `/ws/live/{id}` — dashboard subscriber. Receives
  `{type:"samples", data:[[t,raw,fecg],…], metrics:{fhr,mhr,sq,alarm,label}}`.
  On connect it is sent the latest known metrics immediately.

---

## 5. Heart-rate, signal quality & alarms (`server/analysis.py`)

Derived metrics are computed **server-side** — the server sees every sample, so it
is the single source of truth for both the dashboard list and the patient view
(no duplicated math in the browser).

- **FHR** from the AI-extracted `fecg` channel; **MHR** from the dominant QRS in
  `raw`. Detection is a numpy-only Pan-Tompkins-style pipeline (baseline removal →
  derivative → squaring → moving-window integration → adaptive threshold with a
  physiologic refractory period) over a rolling 6 s window; the reported BPM is an
  EMA-smoothed median of recent R-R intervals.
- **Signal quality (0–100)** blends R-R regularity with beat coverage.
- **Alarms** (singleton/term thresholds, NICHD/ACOG/FIGO):
  normal fetal **110–160 bpm**; **< 110** bradycardia (`low`), **> 160**
  tachycardia (`high`); quality below ~30 → **signal loss** (`signal`).

Validated against a synthetic maternal-75 / fetal-140 signal: FHR ≈ 140, MHR ≈ 75,
SQ ≈ 97, alarm `ok` (`python3 server/analysis.py`).

---

## 6. Data model (`server/database.py`, SQLite/WAL)

```
doctors(id, username, password_hash, name, created_at)
patients(id, name, mrn, sex, dob, notes, created_at, archived)
sessions(id, patient_id, started_at, ended_at, sample_rate)
samples(id, session_id, t, raw, fecg)          # 1-in-4 downsampled history
doctor_login(token, doctor_id, created_at)      # cookie sessions
```

Passwords are PBKDF2-HMAC-SHA256 (salted). `archived` was added in this revision
(auto-migrated on startup); reconnecting a device un-archives its patient.

---

## 7. Run

```bash
cd server
pip install -r requirements.txt          # fastapi, uvicorn, python-multipart, numpy
python seed.py                           # demo doctor/password + patients P001–P003
uvicorn app:app --host 0.0.0.0 --port 8000
# open http://<pi>:8000/login  (on the Pi's own 480×320 screen / kiosk browser)
```

Test the live path without hardware (streams like the real device):
```bash
cd device && python3 stream_sim.py --patient P001 --url ws://localhost:8000/ws/device
```

Before real use: change `DEVICE_TOKEN` / `FECG_DEVICE_TOKEN` and put the server
behind HTTPS/`wss`.

---

## 8. What changed in this revision

| Before | After |
|---|---|
| Desktop layout (`max-width:1100px`, wide tables) | **480×320 touch-first** app shell, bottom-sheet forms, tab bar |
| No heart rate shown | **Server-side FHR + MHR** readouts everywhere |
| No alarms | **Bradycardia / tachycardia / signal-loss** alarms + banner + red list rows |
| Dashboard: status only, buggy "live" flag | Live **FHR + sparkline + signal quality** per row, recency-based streaming, search & alarm-first sort |
| Patients only via `seed.py` / device auto-create | **Add / edit / archive** patients in-app |
| Single dense patient page | **Monitor / Info** tabs, **freeze**, grid traces, auto-reconnect + WS keepalive |
| Static signal status (`bool(last)`) | Recency-gated `streaming` (stale after 6 s); analyzer torn down on disconnect |
```
