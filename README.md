# fECG Server

Backend + clinician web dashboard for the fetal-ECG monitor. **Runs on the PC**
(or any machine on the LAN). The **Raspberry Pi is the client/sender**: it
acquires the abdominal ECG, extracts the fetal ECG with the on-device model, and
streams `(t, raw, fecg)` samples here over a WebSocket.

```
   Raspberry Pi (client/sender)                 PC (this repo — server)
 ┌────────────────────────────┐   ws://PC:8000/ws/device   ┌───────────────────────┐
 │ ADS1293 → infer.py (fECG)   │ ─────────────────────────▶ │ FastAPI + SQLite       │
 │ device/ app  (raspi-deploy) │                            │ • FHR/MHR/alarms       │
 └────────────────────────────┘                            │ • live WS fan-out      │
        browser on Pi LCD  ◀──── http://PC:8000  ──────────│ • web dashboard        │
        (480×320, kiosk)                                    └───────────────────────┘
```

This server is **self-contained** — it does not need `infer.py` or the model. It
computes fetal/maternal heart rate, signal quality, and alarms itself from the
incoming samples (`analysis.py`). See [`WEB_WORKFLOW.md`](WEB_WORKFLOW.md) for the
full workflow, screens, API/WebSocket surface, and data model.

> Research/educational software — **not a certified medical device.**

---

## Run on the PC

```bash
pip install -r requirements.txt        # fastapi, uvicorn, python-multipart, numpy
python seed.py                         # demo doctor/password + patients P001–P003
uvicorn app:app --host 0.0.0.0 --port 8000
```

`--host 0.0.0.0` is required so the Pi can reach it over the LAN. Open the
dashboard at `http://<PC-IP>:8000/login` (demo login: **doctor / password**).

Find the PC's LAN IP:
- Linux/macOS: `hostname -I` or `ipconfig getifaddr en0`
- Windows: `ipconfig` (look for the IPv4 address)

If you can't connect from the Pi, open TCP **8000** in the PC firewall
(Windows: *Windows Defender Firewall → Inbound Rules → New Rule → Port 8000*).

---

## Point the Pi at this server

On the **Pi** (in the `raspi-deploy` repo), tell the device where the server is.
`device/config.py` reads these environment variables (falling back to localhost):

```bash
export FECG_SERVER_URL="ws://<PC-IP>:8000/ws/device"
export FECG_DEVICE_TOKEN="device-secret-001"   # must match the server (below)
export FECG_PATIENT_ID="P001"
python3 device/main.py            # real monitor on the Pi LCD
# or, to test without hardware:
python3 device/stream_sim.py --patient P001 --url "ws://<PC-IP>:8000/ws/device"
```

The device token the Pi sends **must equal** the server's token. On the server set
it before launching:

```bash
export FECG_DEVICE_TOKEN="choose-a-strong-shared-secret"
uvicorn app:app --host 0.0.0.0 --port 8000
```

---

## Configuration (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `FECG_DEVICE_TOKEN` | `device-secret-001` | shared secret the device must present |
| `FECG_DB` | `./fecg.db` | SQLite database path |

Add clinicians from a Python shell: `import database as db;
db.create_doctor("user", "pass", name="Dr. Name")`.

---

## Before real use

- Change `FECG_DEVICE_TOKEN` from the default on **both** server and Pi.
- Put the server behind HTTPS/`wss` (e.g. a reverse proxy) — auth and samples are
  otherwise sent in clear text.
- Pairs with the device/offline-pipeline repo (`raspi-deploy`).
