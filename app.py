#!/usr/bin/env python3
"""
fECG backend — FastAPI + SQLite.

Responsibilities:
  * accept a device WebSocket (/ws/device) that streams (t, raw, fecg) batches,
    derives live FHR/MHR/signal-quality/alarm, persists a downsampled copy, and
    fans the samples + metrics out live
  * serve doctors a session-cookie login and a touch web dashboard
  * expose a live WebSocket (/ws/live/{patient_id}) the dashboard subscribes to
  * REST: patient list (+ live metrics), patient detail/stats, create/edit/archive

Run:
    pip install -r requirements.txt
    python seed.py            # creates demo doctor + patients (first time)
    uvicorn app:app --host 0.0.0.0 --port 8000
"""

import os
import json
import asyncio
import time
from collections import defaultdict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import database as db
from analysis import PatientAnalyzer

DEVICE_TOKEN = os.environ.get("FECG_DEVICE_TOKEN", "device-secret-001")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
# keep DB writes light: store ~1 of every N samples (still fine for review)
PERSIST_EVERY = 4
# a patient is "streaming" if we received a batch within this many seconds
STALE_SEC = 6.0

app = FastAPI(title="fECG Backend")
db.init_db()


# ---------------- live pub/sub ----------------
class Hub:
    """Fan-out of live samples + derived metrics to dashboard subscribers."""

    def __init__(self):
        self.subs = defaultdict(set)          # patient_id -> set[WebSocket]
        self.last = {}                        # patient_id -> last sample row
        self.last_ts = {}                     # patient_id -> wall time of last batch
        self.metrics = {}                     # patient_id -> latest metrics dict
        self.analyzers = {}                   # patient_id -> PatientAnalyzer
        self.lock = asyncio.Lock()

    async def subscribe(self, pid, ws):
        async with self.lock:
            self.subs[pid].add(ws)

    async def unsubscribe(self, pid, ws):
        async with self.lock:
            self.subs[pid].discard(ws)

    def streaming(self, pid):
        ts = self.last_ts.get(pid)
        return bool(ts) and (time.time() - ts) < STALE_SEC

    async def publish(self, pid, batch, metrics=None):
        if batch:
            self.last[pid] = batch[-1]
            self.last_ts[pid] = time.time()
        if metrics is not None:
            self.metrics[pid] = metrics
        payload = json.dumps({"type": "samples", "data": batch, "metrics": metrics})
        dead = []
        for ws in list(self.subs.get(pid, ())):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.unsubscribe(pid, ws)


hub = Hub()


# ---------------- auth helpers ----------------
def current_doctor(request: Request):
    token = request.cookies.get("session")
    return db.doctor_for_token(token)


def require_doctor(request: Request):
    doc = current_doctor(request)
    if not doc:
        raise HTTPException(status_code=401, detail="login required")
    return doc


# ---------------- device ingest ----------------
@app.websocket("/ws/device")
async def ws_device(ws: WebSocket):
    await ws.accept()
    session_id = None
    patient_id = None
    persist_buf = []
    counter = 0
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            mtype = msg.get("type")

            if mtype == "hello":
                if msg.get("token") != DEVICE_TOKEN:
                    await ws.send_text(json.dumps({"type": "error",
                                                   "detail": "bad token"}))
                    await ws.close()
                    return
                patient_id = msg["patient_id"]
                rate = int(msg.get("sample_rate", 250))
                db.upsert_patient(patient_id)
                session_id = db.start_session(patient_id, rate)
                hub.analyzers[patient_id] = PatientAnalyzer(rate)
                await ws.send_text(json.dumps({"type": "ack",
                                               "session_id": session_id}))

            elif mtype == "samples" and session_id is not None:
                batch = msg["data"]               # [[t,raw,fecg], ...]
                analyzer = hub.analyzers.get(patient_id)
                metrics = analyzer.push(batch) if analyzer else None
                await hub.publish(patient_id, batch, metrics)
                for row in batch:
                    counter += 1
                    if counter % PERSIST_EVERY == 0:
                        persist_buf.append((row[0], row[1], row[2]))
                if len(persist_buf) >= 50:
                    db.insert_samples(session_id, persist_buf)
                    persist_buf = []
    except WebSocketDisconnect:
        pass
    finally:
        if persist_buf and session_id is not None:
            db.insert_samples(session_id, persist_buf)
        if session_id is not None:
            db.end_session(session_id)
        if patient_id is not None:
            hub.analyzers.pop(patient_id, None)
            hub.metrics[patient_id] = None
            await hub.publish(patient_id, [], None)   # nudge dashboards to idle


# ---------------- dashboard live feed ----------------
@app.websocket("/ws/live/{patient_id}")
async def ws_live(ws: WebSocket, patient_id: str):
    await ws.accept()
    await hub.subscribe(patient_id, ws)
    # send the latest known metrics immediately so the view isn't blank
    await ws.send_text(json.dumps({"type": "samples", "data": [],
                                   "metrics": hub.metrics.get(patient_id)}))
    try:
        while True:
            await ws.receive_text()   # keepalive pings / ignore
    except WebSocketDisconnect:
        pass
    finally:
        await hub.unsubscribe(patient_id, ws)


# ---------------- auth routes ----------------
@app.get("/login", response_class=HTMLResponse)
async def login_page():
    with open(os.path.join(STATIC_DIR, "login.html")) as f:
        return f.read()


@app.post("/login")
async def login(username: str = Form(...), password: str = Form(...)):
    doc = db.get_doctor_by_username(username)
    if not doc or not db.check_password(password, doc["password_hash"]):
        return JSONResponse({"ok": False, "detail": "invalid credentials"},
                            status_code=401)
    token = db.create_login(doc["id"])
    resp = JSONResponse({"ok": True})
    resp.set_cookie("session", token, httponly=True, samesite="lax")
    return resp


@app.post("/logout")
async def logout(request: Request):
    token = request.cookies.get("session")
    db.delete_login(token)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("session")
    return resp


# ---------------- pages ----------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not current_doctor(request):
        return RedirectResponse("/login")
    with open(os.path.join(STATIC_DIR, "dashboard.html")) as f:
        return f.read()


@app.get("/patient/{patient_id}", response_class=HTMLResponse)
async def patient_page(request: Request, patient_id: str):
    if not current_doctor(request):
        return RedirectResponse("/login")
    with open(os.path.join(STATIC_DIR, "patient.html")) as f:
        return f.read()


# ---------------- REST API ----------------
def _live_view(pid):
    """Live status + metrics for a patient, gated on recency."""
    streaming = hub.streaming(pid)
    m = hub.metrics.get(pid) if streaming else None
    analyzer = hub.analyzers.get(pid)
    return {
        "streaming": streaming,
        "fhr": (m or {}).get("fhr"),
        "mhr": (m or {}).get("mhr"),
        "sq": (m or {}).get("sq"),
        "alarm": (m or {}).get("alarm", "ok") if streaming else "ok",
        "label": (m or {}).get("label") if streaming else None,
        "spark": analyzer.spark() if (streaming and analyzer) else [],
    }


@app.get("/api/me")
async def api_me(request: Request):
    doc = require_doctor(request)
    return {"id": doc["id"], "username": doc["username"], "name": doc["name"]}


@app.get("/api/patients")
async def api_patients(request: Request):
    require_doctor(request)
    pts = db.list_patients()
    for p in pts:
        p.update(_live_view(p["id"]))
    return {"patients": pts}


@app.post("/api/patients")
async def api_create_patient(request: Request):
    require_doctor(request)
    body = await request.json()
    try:
        p = db.create_patient(
            body.get("id"),
            name=body.get("name"),
            mrn=body.get("mrn"),
            sex=body.get("sex"),
            dob=body.get("dob"),
            notes=body.get("notes"),
        )
    except ValueError as e:
        raise HTTPException(409, str(e))
    return p


@app.patch("/api/patients/{patient_id}")
async def api_update_patient(request: Request, patient_id: str):
    require_doctor(request)
    if not db.get_patient(patient_id):
        raise HTTPException(404, "no such patient")
    body = await request.json()
    return db.update_patient(
        patient_id,
        name=body.get("name"),
        mrn=body.get("mrn"),
        sex=body.get("sex"),
        dob=body.get("dob"),
        notes=body.get("notes"),
    )


@app.post("/api/patients/{patient_id}/archive")
async def api_archive_patient(request: Request, patient_id: str):
    require_doctor(request)
    if not db.get_patient(patient_id):
        raise HTTPException(404, "no such patient")
    archived = True
    try:
        body = await request.json()
        if isinstance(body, dict) and "archived" in body:
            archived = bool(body["archived"])
    except Exception:
        pass
    return db.set_archived(patient_id, archived)


@app.get("/api/patients/{patient_id}")
async def api_patient(request: Request, patient_id: str):
    require_doctor(request)
    p = db.get_patient(patient_id)
    if not p:
        raise HTTPException(404, "no such patient")
    p["stats"] = db.patient_stats(patient_id)
    p["recent"] = db.recent_samples(patient_id, limit=2000)
    p["live"] = _live_view(patient_id)
    return p


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
