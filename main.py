from fastapi import FastAPI
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime, timezone
import sqlite3
import os
import secrets


app = FastAPI(title="MT5 Copier Test API with Licenses")

DB_PATH = "licenses.db"

LATEST_SNAPSHOT = {
    "snapshot_id": "",
    "timestamp": "",
    "positions": [],
    "pending_orders": []
}

VALID_MASTER_TOKEN = "MASTER123"


# =============================
# Database helpers
# =============================
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def utc_now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS licenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        license_key TEXT UNIQUE NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        expires_at TEXT,
        max_accounts INTEGER NOT NULL DEFAULT 1,
        note TEXT DEFAULT '',
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS activations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        license_id INTEGER NOT NULL,
        account_login TEXT NOT NULL,
        broker_server TEXT NOT NULL,
        machine_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        UNIQUE(license_id, account_login, broker_server, machine_id),
        FOREIGN KEY (license_id) REFERENCES licenses(id)
    )
    """)

    conn.commit()
    conn.close()


def seed_test_license():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT id FROM licenses WHERE license_key = ?", ("TEST-001",))
    row = cur.fetchone()

    if row is None:
        cur.execute("""
        INSERT INTO licenses (license_key, status, expires_at, max_accounts, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """, (
            "TEST-001",
            "active",
            "2099-12-31 23:59:59",
            1,
            "Default test license",
            utc_now_str()
        ))
        conn.commit()

    conn.close()


def get_license_by_key(license_key: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM licenses WHERE license_key = ?", (license_key,))
    row = cur.fetchone()
    conn.close()
    return row


def is_license_expired(expires_at: Optional[str]) -> bool:
    if not expires_at:
        return False

    try:
        exp = datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S")
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        return exp < now
    except Exception:
        return True


def count_activations(license_id: int) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS cnt FROM activations WHERE license_id = ?", (license_id,))
    row = cur.fetchone()
    conn.close()
    return int(row["cnt"])


def get_activation(license_id: int, account_login: str, broker_server: str, machine_id: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT * FROM activations
    WHERE license_id = ? AND account_login = ? AND broker_server = ? AND machine_id = ?
    """, (license_id, account_login, broker_server, machine_id))
    row = cur.fetchone()
    conn.close()
    return row


def create_or_refresh_activation(license_id: int, account_login: str, broker_server: str, machine_id: str):
    existing = get_activation(license_id, account_login, broker_server, machine_id)
    conn = get_conn()
    cur = conn.cursor()

    if existing:
        cur.execute("""
        UPDATE activations
        SET last_seen_at = ?
        WHERE id = ?
        """, (utc_now_str(), existing["id"]))
    else:
        cur.execute("""
        INSERT INTO activations (license_id, account_login, broker_server, machine_id, created_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """, (license_id, account_login, broker_server, machine_id, utc_now_str(), utc_now_str()))

    conn.commit()
    conn.close()


def validate_license_for_activation(license_key: str, account_login: str, broker_server: str, machine_id: str):
    lic = get_license_by_key(license_key)
    if not lic:
        return False, "License not found", None

    if lic["status"] != "active":
        return False, "License inactive", None

    if is_license_expired(lic["expires_at"]):
        return False, "License expired", None

    existing = get_activation(lic["id"], account_login, broker_server, machine_id)
    if existing:
        create_or_refresh_activation(lic["id"], account_login, broker_server, machine_id)
        return True, "License is valid", lic

    activation_count = count_activations(lic["id"])
    if activation_count >= int(lic["max_accounts"]):
        return False, "Activation limit reached", None

    create_or_refresh_activation(lic["id"], account_login, broker_server, machine_id)
    return True, "License is valid", lic


def validate_license_simple(license_key: str):
    lic = get_license_by_key(license_key)
    if not lic:
        return False, "License not found", None

    if lic["status"] != "active":
        return False, "License inactive", None

    if is_license_expired(lic["expires_at"]):
        return False, "License expired", None

    return True, "License is valid", lic


# =============================
# Models
# =============================
class PositionItem(BaseModel):
    symbol: str
    type: str
    volume: float
    sl: float = 0.0
    tp: float = 0.0


class PendingOrderItem(BaseModel):
    symbol: str
    type: str
    volume: float
    price: float
    sl: float = 0.0
    tp: float = 0.0
    expiration: Optional[str] = None


class MasterPublishRequest(BaseModel):
    master_token: str
    snapshot_id: str
    positions: List[PositionItem] = []
    pending_orders: List[PendingOrderItem] = []


class SlaveActivateRequest(BaseModel):
    license_key: str
    account_login: str
    broker_server: str
    machine_id: str


class SlavePullRequest(BaseModel):
    license_key: str
    last_snapshot_id: str = ""


class AdminCreateLicenseRequest(BaseModel):
    expires_at: Optional[str] = "2099-12-31 23:59:59"
    max_accounts: int = 1
    note: Optional[str] = ""


class AdminUpdateLicenseRequest(BaseModel):
    status: Optional[str] = None
    expires_at: Optional[str] = None
    max_accounts: Optional[int] = None
    note: Optional[str] = None


# =============================
# Startup
# =============================
@app.on_event("startup")
def startup_event():
    init_db()
    seed_test_license()


# =============================
# Routes
# =============================
@app.get("/")
def root():
    return {
        "ok": True,
        "service": "mt5 copier api",
        "db": os.path.abspath(DB_PATH),
        "time": datetime.now(timezone.utc).isoformat()
    }


@app.post("/slave/activate")
def slave_activate(payload: SlaveActivateRequest):
    ok, message, lic = validate_license_for_activation(
        payload.license_key,
        payload.account_login,
        payload.broker_server,
        payload.machine_id
    )

    if not ok:
        return {
            "ok": False,
            "message": message
        }

    return {
        "ok": True,
        "message": message,
        "mode": "db",
        "poll_seconds": 1,
        "expires_at": lic["expires_at"],
        "max_accounts": lic["max_accounts"]
    }


@app.post("/master/publish")
def master_publish(payload: MasterPublishRequest):
    if payload.master_token != VALID_MASTER_TOKEN:
        return {
            "ok": False,
            "message": "Invalid master token"
        }

    LATEST_SNAPSHOT["snapshot_id"] = payload.snapshot_id
    LATEST_SNAPSHOT["timestamp"] = datetime.now(timezone.utc).isoformat()
    LATEST_SNAPSHOT["positions"] = [p.model_dump() for p in payload.positions]
    LATEST_SNAPSHOT["pending_orders"] = [o.model_dump() for o in payload.pending_orders]

    return {
        "ok": True,
        "message": "Snapshot saved",
        "snapshot_id": LATEST_SNAPSHOT["snapshot_id"]
    }


@app.post("/slave/pull")
def slave_pull(payload: SlavePullRequest):
    ok, message, lic = validate_license_simple(payload.license_key)

    if not ok:
        return {
            "ok": False,
            "message": message
        }

    if payload.last_snapshot_id == LATEST_SNAPSHOT["snapshot_id"]:
        return {
            "ok": True,
            "has_update": False,
            "snapshot_id": payload.last_snapshot_id
        }

    return {
        "ok": True,
        "has_update": True,
        "snapshot_id": LATEST_SNAPSHOT["snapshot_id"],
        "timestamp": LATEST_SNAPSHOT["timestamp"],
        "positions": LATEST_SNAPSHOT["positions"],
        "pending_orders": LATEST_SNAPSHOT["pending_orders"]
    }


@app.get("/admin/licenses")
def admin_list_licenses():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT id, license_key, status, expires_at, max_accounts, note, created_at
    FROM licenses
    ORDER BY id DESC
    """)
    rows = cur.fetchall()
    conn.close()

    return {
        "ok": True,
        "licenses": [dict(row) for row in rows]
    }


@app.post("/admin/create-license")
def admin_create_license(payload: AdminCreateLicenseRequest):
    new_key = secrets.token_hex(8).upper()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO licenses (license_key, status, expires_at, max_accounts, note, created_at)
    VALUES (?, ?, ?, ?, ?, ?)
    """, (
        new_key,
        "active",
        payload.expires_at,
        payload.max_accounts,
        payload.note or "",
        utc_now_str()
    ))
    conn.commit()
    conn.close()

    return {
        "ok": True,
        "license_key": new_key,
        "status": "active",
        "expires_at": payload.expires_at,
        "max_accounts": payload.max_accounts
    }


@app.post("/admin/license/{license_key}/update")
def admin_update_license(license_key: str, payload: AdminUpdateLicenseRequest):
    lic = get_license_by_key(license_key)
    if not lic:
        return {
            "ok": False,
            "message": "License not found"
        }

    status = payload.status if payload.status is not None else lic["status"]
    expires_at = payload.expires_at if payload.expires_at is not None else lic["expires_at"]
    max_accounts = payload.max_accounts if payload.max_accounts is not None else lic["max_accounts"]
    note = payload.note if payload.note is not None else lic["note"]

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    UPDATE licenses
    SET status = ?, expires_at = ?, max_accounts = ?, note = ?
    WHERE license_key = ?
    """, (status, expires_at, max_accounts, note, license_key))
    conn.commit()
    conn.close()

    return {
        "ok": True,
        "message": "License updated",
        "license_key": license_key,
        "status": status,
        "expires_at": expires_at,
        "max_accounts": max_accounts,
        "note": note
    }


@app.get("/admin/activations")
def admin_list_activations():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT
        a.id,
        l.license_key,
        a.account_login,
        a.broker_server,
        a.machine_id,
        a.created_at,
        a.last_seen_at
    FROM activations a
    JOIN licenses l ON l.id = a.license_id
    ORDER BY a.id DESC
    """)
    rows = cur.fetchall()
    conn.close()

    return {
        "ok": True,
        "activations": [dict(row) for row in rows]
    }