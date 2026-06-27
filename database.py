"""
SQLite persistence for the fECG backend. Plain stdlib sqlite3 with a small
helper layer (no ORM) to keep dependencies minimal.

Tables:
    users(id, phone_number, email, password, role, is_active, created_at, updated_at)
    staff_profile(id, user_id, full_name, specialization, degree)  # clinician details
    user_login(token, user_id, created_at)            # web auth sessions
    patients(id, full_name, gender, date_of_birth, mrn, citizen_id, address, notes, ...)
    sessions(id, patient_id, started_at, ended_at, sample_rate)
    samples(id, session_id, t, raw, fecg)            # downsampled waveform history
    metrics(id, session_id, t, fhr, mhr, sq, alarm)  # ~1 Hz derived-metric trend
    events(id, session_id, patient_id, t, kind, label)  # alarms / session markers

Only staff are accounts (rows in `users` + `staff_profile`). Patients are monitored
subjects, not logins, so they keep their own table keyed by a TEXT natural id.
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
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone_number TEXT UNIQUE,
            email TEXT UNIQUE,
            password TEXT NOT NULL,        -- PBKDF2 hash, format salt$hash
            role INTEGER DEFAULT 1,        -- 1=clinician (reserved for future RBAC)
            is_active INTEGER DEFAULT 1,
            created_at REAL,
            updated_at REAL
        );
        CREATE TABLE IF NOT EXISTS staff_profile(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE,
            full_name TEXT,
            specialization TEXT,
            degree TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_staff_user ON staff_profile(user_id);
        CREATE TABLE IF NOT EXISTS patients(
            id TEXT PRIMARY KEY,
            full_name TEXT,
            gender TEXT,
            date_of_birth TEXT,
            mrn TEXT,
            citizen_id TEXT,
            address TEXT,
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
        CREATE TABLE IF NOT EXISTS metrics(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            t REAL,
            fhr REAL,
            mhr REAL,
            sq INTEGER,
            alarm TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_metrics_session ON metrics(session_id);
        CREATE TABLE IF NOT EXISTS events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            patient_id TEXT,
            t REAL,
            kind TEXT,           -- 'alarm' | 'session' | 'note'
            label TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_events_patient ON events(patient_id);
        CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
        CREATE TABLE IF NOT EXISTS user_login(
            token TEXT PRIMARY KEY,
            user_id INTEGER,
            created_at REAL
        );
        """
    )
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


def create_staff(phone_number, email, password, full_name,
                  specialization=None, degree=None, role=1):
    """Create a staff account: a `users` row plus its `staff_profile`."""
    now = time.time()
    c = conn()
    cur = c.execute(
        "INSERT INTO users(phone_number,email,password,role,is_active,"
        "created_at,updated_at) VALUES(?,?,?,?,1,?,?)",
        (phone_number, email, make_password(password), role, now, now),
    )
    uid = cur.lastrowid
    c.execute(
        "INSERT INTO staff_profile(user_id,full_name,specialization,degree) "
        "VALUES(?,?,?,?)",
        (uid, full_name, specialization, degree),
    )
    c.commit()
    return uid


def get_user_by_login(identifier):
    """Look up an active user by phone number OR email (login identifier)."""
    return conn().execute(
        "SELECT * FROM users WHERE (phone_number=? OR email=?) AND is_active=1",
        (identifier, identifier),
    ).fetchone()


def create_login(user_id):
    token = secrets.token_urlsafe(24)
    c = conn()
    c.execute(
        "INSERT INTO user_login(token,user_id,created_at) VALUES(?,?,?)",
        (token, user_id, time.time()),
    )
    c.commit()
    return token


def user_for_token(token):
    """Resolve a session token to the user + their staff display name/role."""
    if not token:
        return None
    return conn().execute(
        "SELECT u.id, u.phone_number, u.email, u.role, u.is_active, "
        "       s.full_name AS name "
        "FROM user_login l "
        "JOIN users u ON u.id=l.user_id "
        "LEFT JOIN staff_profile s ON s.user_id=u.id "
        "WHERE l.token=? AND u.is_active=1",
        (token,),
    ).fetchone()


def delete_login(token):
    c = conn()
    c.execute("DELETE FROM user_login WHERE token=?", (token,))
    c.commit()


# ---------- patients ----------
PATIENT_FIELDS = ("full_name", "gender", "date_of_birth", "mrn",
                  "citizen_id", "address", "notes")


def upsert_patient(pid, full_name=None, gender=None, date_of_birth=None,
                   mrn=None, citizen_id=None, address=None, notes=None):
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
        "INSERT INTO patients(id,full_name,gender,date_of_birth,mrn,citizen_id,"
        "address,notes,created_at,archived) VALUES(?,?,?,?,?,?,?,?,?,0)",
        (pid, full_name or pid, gender, date_of_birth, mrn, citizen_id,
         address, notes, time.time()),
    )
    c.commit()


def create_patient(pid, full_name=None, gender=None, date_of_birth=None,
                   mrn=None, citizen_id=None, address=None, notes=None):
    """Explicit clinician-driven create. Raises ValueError if the id exists."""
    pid = (pid or "").strip()
    if not pid:
        raise ValueError("patient id is required")
    c = conn()
    if c.execute("SELECT id FROM patients WHERE id=?", (pid,)).fetchone():
        raise ValueError(f"patient '{pid}' already exists")
    c.execute(
        "INSERT INTO patients(id,full_name,gender,date_of_birth,mrn,citizen_id,"
        "address,notes,created_at,archived) VALUES(?,?,?,?,?,?,?,?,?,0)",
        (pid, (full_name or pid).strip(), gender, date_of_birth, mrn,
         citizen_id, address, notes, time.time()),
    )
    c.commit()
    return get_patient(pid)


def update_patient(pid, **fields):
    """Update any of the PATIENT_FIELDS. Returns the updated row or None."""
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


def get_session(session_id):
    r = conn().execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    return dict(r) if r else None


def session_samples(session_id, limit=20000):
    """All recorded (t, raw, fecg) for one session, oldest first (for replay)."""
    rows = conn().execute(
        "SELECT t, raw, fecg FROM samples WHERE session_id=? ORDER BY id ASC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    return [[r["t"], r["raw"], r["fecg"]] for r in rows]


# ---------- derived metrics (FHR/MHR/SQ/alarm trend) ----------
def insert_metrics(session_id, rows):
    """rows: list of (t, fhr, mhr, sq, alarm)."""
    if not rows:
        return
    c = conn()
    c.executemany(
        "INSERT INTO metrics(session_id,t,fhr,mhr,sq,alarm) VALUES(?,?,?,?,?,?)",
        [(session_id, t, fhr, mhr, sq, alarm) for (t, fhr, mhr, sq, alarm) in rows],
    )
    c.commit()


def session_metrics(session_id, limit=6000):
    """FHR/MHR/SQ/alarm trend for one session, oldest first."""
    rows = conn().execute(
        "SELECT t, fhr, mhr, sq, alarm FROM metrics WHERE session_id=? "
        "ORDER BY id ASC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    return [[r["t"], r["fhr"], r["mhr"], r["sq"], r["alarm"]] for r in rows]


# ---------- events (alarms / session markers / notes) ----------
def insert_event(session_id, patient_id, t, kind, label):
    c = conn()
    cur = c.execute(
        "INSERT INTO events(session_id,patient_id,t,kind,label) VALUES(?,?,?,?,?)",
        (session_id, patient_id, t, kind, label),
    )
    c.commit()
    return cur.lastrowid


def patient_events(pid, limit=200):
    rows = conn().execute(
        "SELECT * FROM events WHERE patient_id=? ORDER BY t DESC, id DESC LIMIT ?",
        (pid, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def session_events(session_id):
    rows = conn().execute(
        "SELECT * FROM events WHERE session_id=? ORDER BY t ASC, id ASC",
        (session_id,),
    ).fetchall()
    return [dict(r) for r in rows]
