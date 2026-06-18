"""
SQLite persistence for the fECG backend. Plain stdlib sqlite3 with a small
helper layer (no ORM) to keep dependencies minimal.

Tables:
    doctors(id, username, password_hash, name, created_at)
    patients(id, name, mrn, sex, dob, notes, created_at)
    sessions(id, patient_id, started_at, ended_at, sample_rate)
    samples(id, session_id, t, raw, fecg)            # downsampled history
    doctor_login(token, doctor_id, created_at)        # web auth sessions
"""

import os
import time
import sqlite3
import hashlib
import secrets
import threading

DB_PATH = os.environ.get("FECG_DB", os.path.join(os.path.dirname(__file__), "fecg.db"))
_local = threading.local()


def conn():
    if not hasattr(_local, "c"):
        _local.c = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.c.row_factory = sqlite3.Row
        _local.c.execute("PRAGMA journal_mode=WAL")
    return _local.c


def init_db():
    c = conn()
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS doctors(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            name TEXT,
            created_at REAL
        );
        CREATE TABLE IF NOT EXISTS patients(
            id TEXT PRIMARY KEY,
            name TEXT,
            mrn TEXT,
            sex TEXT,
            dob TEXT,
            notes TEXT,
            created_at REAL,
            archived INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS sessions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id TEXT,
            started_at REAL,
            ended_at REAL,
            sample_rate INTEGER
        );
        CREATE TABLE IF NOT EXISTS samples(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            t REAL,
            raw REAL,
            fecg REAL
        );
        CREATE INDEX IF NOT EXISTS idx_samples_session ON samples(session_id);
        CREATE TABLE IF NOT EXISTS doctor_login(
            token TEXT PRIMARY KEY,
            doctor_id INTEGER,
            created_at REAL
        );
        """
    )
    # migration: add patients.archived to databases created before it existed
    cols = {r["name"] for r in c.execute("PRAGMA table_info(patients)").fetchall()}
    if "archived" not in cols:
        c.execute("ALTER TABLE patients ADD COLUMN archived INTEGER DEFAULT 0")
    c.commit()


# ---------- auth helpers ----------
def _hash(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()


def make_password(pw):
    salt = secrets.token_hex(8)
    return f"{salt}${_hash(pw, salt)}"


def check_password(pw, stored):
    try:
        salt, h = stored.split("$", 1)
        return secrets.compare_digest(_hash(pw, salt), h)
    except Exception:
        return False


def create_doctor(username, password, name=None):
    c = conn()
    c.execute(
        "INSERT INTO doctors(username,password_hash,name,created_at) VALUES(?,?,?,?)",
        (username, make_password(password), name or username, time.time()),
    )
    c.commit()


def get_doctor_by_username(username):
    return conn().execute(
        "SELECT * FROM doctors WHERE username=?", (username,)
    ).fetchone()


def create_login(doctor_id):
    token = secrets.token_urlsafe(24)
    c = conn()
    c.execute(
        "INSERT INTO doctor_login(token,doctor_id,created_at) VALUES(?,?,?)",
        (token, doctor_id, time.time()),
    )
    c.commit()
    return token


def doctor_for_token(token):
    if not token:
        return None
    row = conn().execute(
        "SELECT d.* FROM doctor_login l JOIN doctors d ON d.id=l.doctor_id "
        "WHERE l.token=?",
        (token,),
    ).fetchone()
    return row


def delete_login(token):
    c = conn()
    c.execute("DELETE FROM doctor_login WHERE token=?", (token,))
    c.commit()


# ---------- patients ----------
PATIENT_FIELDS = ("name", "mrn", "sex", "dob", "notes")


def upsert_patient(pid, name=None, mrn=None, sex=None, dob=None, notes=None):
    """Create a patient if missing (used by the device on first connect).

    Existing patients are left untouched (a reconnect must not clobber details
    a clinician has edited). Reconnecting un-archives the patient.
    """
    c = conn()
    existing = c.execute("SELECT id FROM patients WHERE id=?", (pid,)).fetchone()
    if existing:
        c.execute("UPDATE patients SET archived=0 WHERE id=?", (pid,))
        c.commit()
        return
    c.execute(
        "INSERT INTO patients(id,name,mrn,sex,dob,notes,created_at,archived) "
        "VALUES(?,?,?,?,?,?,?,0)",
        (pid, name or pid, mrn, sex, dob, notes, time.time()),
    )
    c.commit()


def create_patient(pid, name=None, mrn=None, sex=None, dob=None, notes=None):
    """Explicit clinician-driven create. Raises ValueError if the id exists."""
    pid = (pid or "").strip()
    if not pid:
        raise ValueError("patient id is required")
    c = conn()
    if c.execute("SELECT id FROM patients WHERE id=?", (pid,)).fetchone():
        raise ValueError(f"patient '{pid}' already exists")
    c.execute(
        "INSERT INTO patients(id,name,mrn,sex,dob,notes,created_at,archived) "
        "VALUES(?,?,?,?,?,?,?,0)",
        (pid, (name or pid).strip(), mrn, sex, dob, notes, time.time()),
    )
    c.commit()
    return get_patient(pid)


def update_patient(pid, **fields):
    """Update any of name/mrn/sex/dob/notes. Returns the updated row or None."""
    sets, vals = [], []
    for k in PATIENT_FIELDS:
        if k in fields and fields[k] is not None:
            sets.append(f"{k}=?")
            vals.append(fields[k])
    if not sets:
        return get_patient(pid)
    vals.append(pid)
    c = conn()
    c.execute(f"UPDATE patients SET {','.join(sets)} WHERE id=?", vals)
    c.commit()
    return get_patient(pid)


def set_archived(pid, archived=True):
    c = conn()
    c.execute("UPDATE patients SET archived=? WHERE id=?", (1 if archived else 0, pid))
    c.commit()
    return get_patient(pid)


def list_patients(include_archived=False):
    where = "" if include_archived else "WHERE COALESCE(p.archived,0)=0"
    rows = conn().execute(
        f"""
        SELECT p.*,
          (SELECT COUNT(*) FROM sessions s WHERE s.patient_id=p.id) AS n_sessions,
          (SELECT MAX(s.started_at) FROM sessions s WHERE s.patient_id=p.id) AS last_seen
        FROM patients p {where}
        ORDER BY last_seen DESC NULLS LAST, p.created_at DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def get_patient(pid):
    r = conn().execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
    return dict(r) if r else None


# ---------- sessions & samples ----------
def start_session(patient_id, sample_rate):
    c = conn()
    cur = c.execute(
        "INSERT INTO sessions(patient_id,started_at,sample_rate) VALUES(?,?,?)",
        (patient_id, time.time(), sample_rate),
    )
    c.commit()
    return cur.lastrowid


def end_session(session_id):
    c = conn()
    c.execute("UPDATE sessions SET ended_at=? WHERE id=?", (time.time(), session_id))
    c.commit()


def insert_samples(session_id, rows):
    """rows: list of (t, raw, fecg)."""
    c = conn()
    c.executemany(
        "INSERT INTO samples(session_id,t,raw,fecg) VALUES(?,?,?,?)",
        [(session_id, t, raw, fecg) for (t, raw, fecg) in rows],
    )
    c.commit()


def patient_stats(pid):
    c = conn()
    sess = c.execute(
        "SELECT * FROM sessions WHERE patient_id=? ORDER BY started_at DESC",
        (pid,),
    ).fetchall()
    total = c.execute(
        "SELECT COUNT(*) n FROM samples s JOIN sessions e ON e.id=s.session_id "
        "WHERE e.patient_id=?",
        (pid,),
    ).fetchone()["n"]
    return {
        "sessions": [dict(s) for s in sess],
        "total_samples": total,
    }


def recent_samples(pid, limit=2000):
    rows = conn().execute(
        """
        SELECT s.t, s.raw, s.fecg FROM samples s
        JOIN sessions e ON e.id=s.session_id
        WHERE e.patient_id=?
        ORDER BY s.id DESC LIMIT ?
        """,
        (pid, limit),
    ).fetchall()
    rows = [[r["t"], r["raw"], r["fecg"]] for r in rows][::-1]
    return rows
