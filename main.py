from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime, timezone, timedelta
import sqlite3
import secrets


app = FastAPI(
    title="MT5 Copier API + Admin Panel",
    docs_url=None,
    redoc_url=None,
    openapi_url=None
)

DB_PATH = "licenses.db"

ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "rcx123"
ADMIN_SESSIONS = {}

LATEST_SNAPSHOT = {
    "snapshot_id": "",
    "timestamp": 0,
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


def utc_now():
    return datetime.now(timezone.utc)


def utc_now_str():
    return utc_now().strftime("%Y-%m-%d %H:%M:%S")


def parse_utc_db_time(dt_str: Optional[str]) -> Optional[datetime]:
    if not dt_str:
        return None
    try:
        return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def seconds_ago_from_str(dt_str: Optional[str]) -> int:
    dt = parse_utc_db_time(dt_str)
    if dt is None:
        return -1
    return max(0, int((utc_now() - dt).total_seconds()))


def add_days_to_dt_str(dt_str: Optional[str], days: int) -> str:
    base = parse_utc_db_time(dt_str)
    if base is None:
        base = utc_now()
    return (base + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def has_column(conn, table_name: str, column_name: str) -> bool:
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table_name})")
    rows = cur.fetchall()
    for row in rows:
        if row["name"] == column_name:
            return True
    return False


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS licenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        license_key TEXT UNIQUE NOT NULL,
        name TEXT DEFAULT '',
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
        balance REAL DEFAULT 0,
        created_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        UNIQUE(license_id, account_login, broker_server, machine_id),
        FOREIGN KEY (license_id) REFERENCES licenses(id)
    )
    """)

    # migrations for older DBs
    if not has_column(conn, "licenses", "name"):
        cur.execute("ALTER TABLE licenses ADD COLUMN name TEXT DEFAULT ''")

    if not has_column(conn, "activations", "balance"):
        cur.execute("ALTER TABLE activations ADD COLUMN balance REAL DEFAULT 0")

    conn.commit()
    conn.close()


def seed_test_license():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT id FROM licenses WHERE license_key = ?", ("TEST-001",))
    row = cur.fetchone()

    if row is None:
        cur.execute("""
        INSERT INTO licenses (license_key, name, status, expires_at, max_accounts, note, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            "TEST-001",
            "Default Test Client",
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


def get_license_by_id(license_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM licenses WHERE id = ?", (license_id,))
    row = cur.fetchone()
    conn.close()
    return row


def is_license_expired(expires_at: Optional[str]) -> bool:
    if not expires_at:
        return False
    try:
        exp = datetime.strptime(expires_at, "%Y-%m-%d %H:%M:%S")
        now = utc_now().replace(tzinfo=None)
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


def get_activation_by_login(license_id: int, account_login: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT * FROM activations
    WHERE license_id = ? AND account_login = ?
    """, (license_id, account_login))
    row = cur.fetchone()
    conn.close()
    return row


def get_activation_exact(license_id: int, account_login: str, broker_server: str, machine_id: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT * FROM activations
    WHERE license_id = ? AND account_login = ? AND broker_server = ? AND machine_id = ?
    """, (license_id, account_login, broker_server, machine_id))
    row = cur.fetchone()
    conn.close()
    return row


def refresh_activation_seen(license_id: int, account_login: str, broker_server: str, machine_id: str, balance: float = 0.0):
    conn = get_conn()
    cur = conn.cursor()

    exact = get_activation_exact(license_id, account_login, broker_server, machine_id)
    if exact:
        cur.execute("""
        UPDATE activations
        SET last_seen_at = ?, balance = ?
        WHERE id = ?
        """, (utc_now_str(), balance, exact["id"]))
    else:
        same_login = get_activation_by_login(license_id, account_login)
        if same_login:
            cur.execute("""
            UPDATE activations
            SET broker_server = ?, machine_id = ?, last_seen_at = ?, balance = ?
            WHERE id = ?
            """, (broker_server, machine_id, utc_now_str(), balance, same_login["id"]))
        else:
            cur.execute("""
            INSERT INTO activations (license_id, account_login, broker_server, machine_id, balance, created_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (license_id, account_login, broker_server, machine_id, balance, utc_now_str(), utc_now_str()))

    conn.commit()
    conn.close()


def refresh_last_seen_by_license(license_key: str, account_login: Optional[str] = None, broker_server: Optional[str] = None,
                                 machine_id: Optional[str] = None, balance: Optional[float] = None):
    lic = get_license_by_key(license_key)
    if not lic:
        return

    conn = get_conn()
    cur = conn.cursor()

    if account_login:
        cur.execute("""
        SELECT * FROM activations
        WHERE license_id = ? AND account_login = ?
        """, (lic["id"], account_login))
        row = cur.fetchone()

        if row:
            cur.execute("""
            UPDATE activations
            SET last_seen_at = ?,
                broker_server = COALESCE(?, broker_server),
                machine_id = COALESCE(?, machine_id),
                balance = COALESCE(?, balance)
            WHERE id = ?
            """, (utc_now_str(), broker_server, machine_id, balance, row["id"]))
            conn.commit()
            conn.close()
            return

    cur.execute("""
    UPDATE activations
    SET last_seen_at = ?
    WHERE license_id = ?
    """, (utc_now_str(), lic["id"]))
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

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    SELECT * FROM activations
    WHERE license_id = ? AND account_login = ?
    """, (lic["id"], account_login))
    same_login = cur.fetchone()

    if same_login:
        cur.execute("""
        UPDATE activations
        SET last_seen_at = ?, broker_server = ?, machine_id = ?
        WHERE id = ?
        """, (utc_now_str(), broker_server, machine_id, same_login["id"]))
        conn.commit()
        conn.close()
        return True, "License is valid", lic

    cur.execute("""
    SELECT COUNT(*) AS cnt
    FROM activations
    WHERE license_id = ?
    """, (lic["id"],))
    cnt = int(cur.fetchone()["cnt"])

    if cnt >= int(lic["max_accounts"]):
        conn.close()
        return False, "Max accounts reached", None

    cur.execute("""
    INSERT INTO activations (license_id, account_login, broker_server, machine_id, balance, created_at, last_seen_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (lic["id"], account_login, broker_server, machine_id, 0.0, utc_now_str(), utc_now_str()))
    conn.commit()
    conn.close()

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


def create_admin_session_token():
    return secrets.token_hex(24)


def require_admin_token(x_admin_token: Optional[str]):
    if not x_admin_token:
        raise HTTPException(status_code=401, detail="Missing admin token")

    if x_admin_token not in ADMIN_SESSIONS:
        raise HTTPException(status_code=401, detail="Invalid admin token")


# =============================
# Models
# =============================
class AdminLoginRequest(BaseModel):
    username: str
    password: str


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
    account_balance: Optional[float] = 0.0


class SlavePullRequest(BaseModel):
    license_key: str
    last_snapshot_id: str = ""
    account_login: Optional[str] = None
    broker_server: Optional[str] = None
    machine_id: Optional[str] = None
    account_balance: Optional[float] = None


class AdminCreateLicenseRequest(BaseModel):
    license_key: Optional[str] = None
    name: Optional[str] = ""
    expires_at: Optional[str] = "2099-12-31 23:59:59"
    max_accounts: int = 1
    note: Optional[str] = ""


class AdminUpdateLicenseRequest(BaseModel):
    new_license_key: Optional[str] = None
    name: Optional[str] = None
    status: Optional[str] = None
    expires_at: Optional[str] = None
    max_accounts: Optional[int] = None
    note: Optional[str] = None


class AdminExtendLicenseRequest(BaseModel):
    days: int


# =============================
# Startup
# =============================
@app.on_event("startup")
def startup_event():
    init_db()
    seed_test_license()


# =============================
# Public Routes
# =============================
@app.get("/")
def root():
    return {
        "ok": True,
        "service": "mt5 copier api",
        "time": utc_now().isoformat()
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

    refresh_activation_seen(
        lic["id"],
        payload.account_login,
        payload.broker_server,
        payload.machine_id,
        float(payload.account_balance or 0.0)
    )

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
    LATEST_SNAPSHOT["timestamp"] = int(utc_now().timestamp())
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

    refresh_last_seen_by_license(
        payload.license_key,
        payload.account_login,
        payload.broker_server,
        payload.machine_id,
        payload.account_balance
    )

    if payload.last_snapshot_id == LATEST_SNAPSHOT["snapshot_id"]:
        return {
            "ok": True,
            "has_update": False,
            "snapshot_id": payload.last_snapshot_id,
            "timestamp": LATEST_SNAPSHOT["timestamp"]
        }

    return {
        "ok": True,
        "has_update": True,
        "snapshot_id": LATEST_SNAPSHOT["snapshot_id"],
        "timestamp": LATEST_SNAPSHOT["timestamp"],
        "positions": LATEST_SNAPSHOT["positions"],
        "pending_orders": LATEST_SNAPSHOT["pending_orders"]
    }


# =============================
# Protected Admin API
# =============================
@app.post("/admin/login")
def admin_login(payload: AdminLoginRequest):
    if payload.username != ADMIN_USERNAME or payload.password != ADMIN_PASSWORD:
        return {
            "ok": False,
            "message": "Invalid username or password"
        }

    token = create_admin_session_token()
    ADMIN_SESSIONS[token] = {
        "username": payload.username,
        "created_at": utc_now_str()
    }

    return {
        "ok": True,
        "token": token,
        "username": payload.username
    }


@app.get("/admin/me")
def admin_me(x_admin_token: Optional[str] = Header(None)):
    require_admin_token(x_admin_token)
    sess = ADMIN_SESSIONS.get(x_admin_token, {})
    return {
        "ok": True,
        "username": sess.get("username", "admin"),
        "created_at": sess.get("created_at", "")
    }


@app.post("/admin/logout")
def admin_logout(x_admin_token: Optional[str] = Header(None)):
    require_admin_token(x_admin_token)
    if x_admin_token in ADMIN_SESSIONS:
        del ADMIN_SESSIONS[x_admin_token]
    return {"ok": True}


@app.get("/admin/dashboard")
def admin_dashboard(x_admin_token: Optional[str] = Header(None)):
    require_admin_token(x_admin_token)

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) AS cnt FROM licenses")
    total_licenses = int(cur.fetchone()["cnt"])

    cur.execute("SELECT COUNT(*) AS cnt FROM licenses WHERE status = 'active'")
    active_licenses = int(cur.fetchone()["cnt"])

    cur.execute("SELECT COUNT(*) AS cnt FROM activations")
    total_activations = int(cur.fetchone()["cnt"])

    cur.execute("""
    SELECT COUNT(*) AS cnt
    FROM activations
    WHERE last_seen_at >= datetime('now', '-2 minutes')
    """)
    online_clients = int(cur.fetchone()["cnt"])

    conn.close()

    return {
        "ok": True,
        "total_licenses": total_licenses,
        "active_licenses": active_licenses,
        "total_activations": total_activations,
        "online_clients": online_clients,
        "server_time": utc_now_str()
    }


@app.get("/admin/live-clients-text")
def admin_live_clients_text(x_admin_token: Optional[str] = Header(None)):
    require_admin_token(x_admin_token)

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    SELECT
        l.license_key,
        l.name,
        a.account_login,
        a.broker_server,
        a.balance,
        a.last_seen_at
    FROM activations a
    JOIN licenses l ON l.id = a.license_id
    WHERE a.last_seen_at >= datetime('now', '-2 minutes')
    ORDER BY a.last_seen_at DESC
    LIMIT 5
    """)
    rows = cur.fetchall()
    conn.close()

    lines = []
    for row in rows:
        age = seconds_ago_from_str(row["last_seen_at"])
        label = row["name"] if (row["name"] or "").strip() else row["license_key"]
        balance_text = f" | bal {float(row['balance'] or 0):.2f}" if row["balance"] is not None else ""
        if age >= 0:
            lines.append(
                f'{label} | {row["account_login"]} | {row["broker_server"]}{balance_text} | {age}s ago'
            )
        else:
            lines.append(
                f'{label} | {row["account_login"]} | {row["broker_server"]}{balance_text}'
            )

    while len(lines) < 5:
        lines.append("-")

    return {
        "ok": True,
        "lines": lines
    }


@app.get("/admin/licenses")
def admin_list_licenses(
    q: str = "",
    x_admin_token: Optional[str] = Header(None)
):
    require_admin_token(x_admin_token)

    q = (q or "").strip()
    like_q = f"%{q}%"

    conn = get_conn()
    cur = conn.cursor()

    if q:
        cur.execute("""
        SELECT
            l.id,
            l.license_key,
            l.name,
            l.status,
            l.expires_at,
            l.max_accounts,
            l.note,
            l.created_at,
            (
                SELECT COUNT(*)
                FROM activations a
                WHERE a.license_id = l.id
            ) AS activations_count,
            (
                SELECT MAX(a.last_seen_at)
                FROM activations a
                WHERE a.license_id = l.id
            ) AS last_seen_at,
            (
                SELECT a.account_login
                FROM activations a
                WHERE a.license_id = l.id
                ORDER BY a.last_seen_at DESC
                LIMIT 1
            ) AS latest_account_login,
            (
                SELECT a.balance
                FROM activations a
                WHERE a.license_id = l.id
                ORDER BY a.last_seen_at DESC
                LIMIT 1
            ) AS latest_balance
        FROM licenses l
        WHERE
            l.license_key LIKE ?
            OR l.name LIKE ?
            OR l.note LIKE ?
            OR EXISTS (
                SELECT 1
                FROM activations a2
                WHERE a2.license_id = l.id
                  AND a2.account_login LIKE ?
            )
        ORDER BY l.id DESC
        """, (like_q, like_q, like_q, like_q))
    else:
        cur.execute("""
        SELECT
            l.id,
            l.license_key,
            l.name,
            l.status,
            l.expires_at,
            l.max_accounts,
            l.note,
            l.created_at,
            (
                SELECT COUNT(*)
                FROM activations a
                WHERE a.license_id = l.id
            ) AS activations_count,
            (
                SELECT MAX(a.last_seen_at)
                FROM activations a
                WHERE a.license_id = l.id
            ) AS last_seen_at,
            (
                SELECT a.account_login
                FROM activations a
                WHERE a.license_id = l.id
                ORDER BY a.last_seen_at DESC
                LIMIT 1
            ) AS latest_account_login,
            (
                SELECT a.balance
                FROM activations a
                WHERE a.license_id = l.id
                ORDER BY a.last_seen_at DESC
                LIMIT 1
            ) AS latest_balance
        FROM licenses l
        ORDER BY l.id DESC
        """)

    rows = cur.fetchall()
    conn.close()

    return {
        "ok": True,
        "licenses": [dict(row) for row in rows]
    }


@app.get("/admin/license/{license_key}")
def admin_get_license(license_key: str, x_admin_token: Optional[str] = Header(None)):
    require_admin_token(x_admin_token)

    lic = get_license_by_key(license_key)
    if not lic:
        return {"ok": False, "message": "License not found"}

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT
        id,
        account_login,
        broker_server,
        machine_id,
        balance,
        created_at,
        last_seen_at
    FROM activations
    WHERE license_id = ?
    ORDER BY last_seen_at DESC
    """, (lic["id"],))
    activations = [dict(r) for r in cur.fetchall()]
    conn.close()

    return {
        "ok": True,
        "license": dict(lic),
        "activations": activations
    }


@app.post("/admin/create-license")
def admin_create_license(payload: AdminCreateLicenseRequest, x_admin_token: Optional[str] = Header(None)):
    require_admin_token(x_admin_token)

    license_key = (payload.license_key or "").strip().upper()
    if not license_key:
        license_key = secrets.token_hex(8).upper()

    existing = get_license_by_key(license_key)
    if existing:
        return {
            "ok": False,
            "message": "License key already exists"
        }

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO licenses (license_key, name, status, expires_at, max_accounts, note, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        license_key,
        payload.name or "",
        "active",
        payload.expires_at,
        max(1, int(payload.max_accounts)),
        payload.note or "",
        utc_now_str()
    ))
    conn.commit()
    conn.close()

    return {
        "ok": True,
        "license_key": license_key,
        "name": payload.name or "",
        "status": "active",
        "expires_at": payload.expires_at,
        "max_accounts": max(1, int(payload.max_accounts)),
        "note": payload.note or ""
    }


@app.post("/admin/license/{license_key}/update")
def admin_update_license(license_key: str, payload: AdminUpdateLicenseRequest, x_admin_token: Optional[str] = Header(None)):
    require_admin_token(x_admin_token)

    lic = get_license_by_key(license_key)
    if not lic:
        return {
            "ok": False,
            "message": "License not found"
        }

    new_license_key = (payload.new_license_key if payload.new_license_key is not None else lic["license_key"]).strip().upper()
    name = payload.name if payload.name is not None else lic["name"]
    status = payload.status if payload.status is not None else lic["status"]
    expires_at = payload.expires_at if payload.expires_at is not None else lic["expires_at"]
    max_accounts = payload.max_accounts if payload.max_accounts is not None else lic["max_accounts"]
    note = payload.note if payload.note is not None else lic["note"]

    if not new_license_key:
        return {
            "ok": False,
            "message": "License key cannot be empty"
        }

    if status not in ["active", "inactive"]:
        return {
            "ok": False,
            "message": "Invalid status"
        }

    max_accounts = max(1, int(max_accounts))

    if new_license_key != lic["license_key"]:
        existing = get_license_by_key(new_license_key)
        if existing:
            return {
                "ok": False,
                "message": "New license key already exists"
            }

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    UPDATE licenses
    SET license_key = ?, name = ?, status = ?, expires_at = ?, max_accounts = ?, note = ?
    WHERE id = ?
    """, (new_license_key, name or "", status, expires_at, max_accounts, note or "", lic["id"]))
    conn.commit()
    conn.close()

    return {
        "ok": True,
        "message": "License updated",
        "license_key": new_license_key,
        "name": name or "",
        "status": status,
        "expires_at": expires_at,
        "max_accounts": max_accounts,
        "note": note or ""
    }


@app.post("/admin/license/{license_key}/extend")
def admin_extend_license(license_key: str, payload: AdminExtendLicenseRequest, x_admin_token: Optional[str] = Header(None)):
    require_admin_token(x_admin_token)

    lic = get_license_by_key(license_key)
    if not lic:
        return {
            "ok": False,
            "message": "License not found"
        }

    days = int(payload.days)
    if days <= 0:
        return {
            "ok": False,
            "message": "Days must be positive"
        }

    new_expires_at = add_days_to_dt_str(lic["expires_at"], days)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    UPDATE licenses
    SET expires_at = ?
    WHERE id = ?
    """, (new_expires_at, lic["id"]))
    conn.commit()
    conn.close()

    return {
        "ok": True,
        "message": f"License extended by {days} days",
        "license_key": license_key,
        "expires_at": new_expires_at
    }


@app.post("/admin/license/{license_key}/reset-activations")
def admin_reset_activations(license_key: str, x_admin_token: Optional[str] = Header(None)):
    require_admin_token(x_admin_token)

    lic = get_license_by_key(license_key)
    if not lic:
        return {
            "ok": False,
            "message": "License not found"
        }

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM activations WHERE license_id = ?", (lic["id"],))
    deleted = cur.rowcount
    conn.commit()
    conn.close()

    return {
        "ok": True,
        "message": "Activations reset",
        "deleted": deleted
    }


@app.delete("/admin/license/{license_key}")
def admin_delete_license(license_key: str, x_admin_token: Optional[str] = Header(None)):
    require_admin_token(x_admin_token)

    lic = get_license_by_key(license_key)
    if not lic:
        return {
            "ok": False,
            "message": "License not found"
        }

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM activations WHERE license_id = ?", (lic["id"],))
    cur.execute("DELETE FROM licenses WHERE id = ?", (lic["id"],))
    conn.commit()
    conn.close()

    return {
        "ok": True,
        "message": "License deleted",
        "license_key": license_key
    }


@app.get("/admin/activations")
def admin_list_activations(x_admin_token: Optional[str] = Header(None)):
    require_admin_token(x_admin_token)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT
        a.id,
        l.license_key,
        l.name,
        a.account_login,
        a.broker_server,
        a.machine_id,
        a.balance,
        a.created_at,
        a.last_seen_at
    FROM activations a
    JOIN licenses l ON l.id = a.license_id
    ORDER BY a.last_seen_at DESC
    """)
    rows = cur.fetchall()
    conn.close()

    return {
        "ok": True,
        "activations": [dict(row) for row in rows]
    }


@app.get("/admin/online-clients")
def admin_online_clients(x_admin_token: Optional[str] = Header(None)):
    require_admin_token(x_admin_token)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT
        l.license_key,
        l.name,
        a.account_login,
        a.broker_server,
        a.machine_id,
        a.balance,
        a.last_seen_at
    FROM activations a
    JOIN licenses l ON l.id = a.license_id
    WHERE a.last_seen_at >= datetime('now', '-2 minutes')
    ORDER BY a.last_seen_at DESC
    """)
    rows = cur.fetchall()
    conn.close()

    items = []
    for row in rows:
        item = dict(row)
        item["age_sec"] = seconds_ago_from_str(row["last_seen_at"])
        items.append(item)

    return {
        "ok": True,
        "clients": items
    }


# =============================
# Admin Panel HTML
# =============================
@app.get("/admin-panel", response_class=HTMLResponse)
def admin_panel():
    return """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>MT5 Copier Admin Panel</title>
    <style>
        body { font-family: Arial, sans-serif; background:#f5f7fb; margin:0; padding:24px; }
        h1,h2,h3 { margin-top:0; }
        .card { background:white; border-radius:12px; padding:16px; margin-bottom:16px; box-shadow:0 2px 10px rgba(0,0,0,0.08); }
        input, button, textarea, select { padding:10px; margin:6px 0; font-size:14px; }
        input, textarea, select { width:100%; box-sizing:border-box; }
        textarea { min-height:90px; resize:vertical; }
        button { cursor:pointer; border:none; border-radius:8px; background:#2563eb; color:white; padding:10px 14px; }
        button.red { background:#dc2626; }
        button.green { background:#16a34a; }
        button.gray { background:#6b7280; }
        button.orange { background:#ea580c; }
        table { width:100%; border-collapse:collapse; margin-top:10px; }
        th, td { border-bottom:1px solid #ddd; padding:10px; text-align:left; vertical-align:top; font-size:13px; }
        .row { display:grid; grid-template-columns:1fr 1fr 1fr; gap:12px; }
        .row2 { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
        .small { color:#555; font-size:13px; }
        .muted { color:#666; }
        .hidden { display:none; }
        pre { background:#111827; color:#e5e7eb; padding:12px; border-radius:8px; overflow:auto; }
        .topbar { display:flex; justify-content:space-between; align-items:center; margin-bottom:16px; }
        .badge { display:inline-block; padding:4px 8px; border-radius:999px; font-size:12px; color:white; }
        .active { background:#16a34a; }
        .inactive { background:#dc2626; }
        .toolbar { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
        .actions button { margin-right:6px; margin-bottom:6px; }
        #editModalWrap {
            position:fixed; inset:0; background:rgba(0,0,0,0.45);
            display:none; align-items:center; justify-content:center; padding:20px; z-index:9999;
        }
        #editModal {
            width:min(900px, 96vw); max-height:92vh; overflow:auto;
            background:white; border-radius:12px; padding:18px;
            box-shadow:0 10px 40px rgba(0,0,0,0.25);
        }
    </style>
</head>
<body>
    <div id="loginView" class="card" style="max-width:520px;margin:80px auto;">
        <h2>Admin Login</h2>
        <div>
            <label>Username</label>
            <input id="adminUsername" type="text" placeholder="Username">
        </div>
        <div>
            <label>Password</label>
            <input id="adminPassword" type="password" placeholder="Password">
        </div>
        <button onclick="loginAdmin()">Login</button>
        <pre id="loginResult"></pre>
    </div>

    <div id="appView" class="hidden">
        <div class="topbar">
            <div>
                <h1>MT5 Copier Admin Panel</h1>
                <div class="small" id="welcomeBar"></div>
            </div>
            <div class="toolbar">
                <button class="gray" onclick="loadAll()">Refresh</button>
                <button class="red" onclick="logoutAdmin()">Logout</button>
            </div>
        </div>

        <div class="card">
            <h3>Dashboard</h3>
            <div id="dashboardStats" class="small">Loading...</div>
        </div>

        <div class="card">
            <h3>Create License</h3>
            <div class="row">
                <div>
                    <label>License Key (optional)</label>
                    <input id="createLicenseKey" placeholder="Leave empty for auto-generated">
                </div>
                <div>
                    <label>Name</label>
                    <input id="createName" placeholder="Client / Desk / Strategy">
                </div>
                <div>
                    <label>Expires At</label>
                    <input id="createExpiresAt" value="2026-12-31 23:59:59">
                </div>
            </div>
            <div class="row">
                <div>
                    <label>Max Accounts</label>
                    <input id="createMaxAccounts" value="1">
                </div>
                <div style="grid-column: span 2;">
                    <label>Note</label>
                    <input id="createNote" placeholder="Internal note">
                </div>
            </div>
            <button onclick="createLicense()">Create License</button>
            <pre id="createResult"></pre>
        </div>

        <div class="card">
            <h3>Licenses</h3>
            <div class="row">
                <div>
                    <label>Search by Name / License / Account Login</label>
                    <input id="searchInput" placeholder="e.g. Ivan, TEST-001, 123456" onkeydown="if(event.key==='Enter'){loadLicenses();}">
                </div>
                <div style="display:flex;align-items:end;">
                    <button onclick="loadLicenses()">Search</button>
                </div>
                <div style="display:flex;align-items:end;">
                    <button class="gray" onclick="clearSearch()">Clear</button>
                </div>
            </div>
            <div id="licensesTable"></div>
        </div>

        <div class="card">
            <h3>Online Clients</h3>
            <div id="onlineClientsTable"></div>
        </div>

        <div class="card">
            <h3>Activations</h3>
            <div id="activationsTable"></div>
        </div>
    </div>

    <div id="editModalWrap">
        <div id="editModal">
            <div class="topbar">
                <h3>Edit License</h3>
                <button class="gray" onclick="closeEditModal()">Close</button>
            </div>
            <input type="hidden" id="editOriginalKey">

            <div class="row">
                <div>
                    <label>License Key</label>
                    <input id="editLicenseKey">
                </div>
                <div>
                    <label>Name</label>
                    <input id="editName">
                </div>
                <div>
                    <label>Status</label>
                    <select id="editStatus">
                        <option value="active">active</option>
                        <option value="inactive">inactive</option>
                    </select>
                </div>
            </div>

            <div class="row">
                <div>
                    <label>Expires At</label>
                    <input id="editExpiresAt">
                </div>
                <div>
                    <label>Max Accounts</label>
                    <input id="editMaxAccounts">
                </div>
                <div>
                    <label>Quick Extend</label>
                    <div class="toolbar">
                        <button onclick="extendCurrentLicense(7)">+7 days</button>
                        <button onclick="extendCurrentLicense(30)">+30 days</button>
                        <button onclick="extendCurrentLicense(90)">+90 days</button>
                    </div>
                </div>
            </div>

            <div>
                <label>Note</label>
                <textarea id="editNote"></textarea>
            </div>

            <div class="toolbar" style="margin-top:12px;">
                <button class="green" onclick="saveLicenseEdit()">Save Changes</button>
                <button class="orange" onclick="resetActivationsCurrent()">Reset Activations</button>
                <button class="red" onclick="deleteCurrentLicense()">Delete License</button>
            </div>

            <h3 style="margin-top:24px;">License Activations</h3>
            <div id="editActivations"></div>

            <pre id="editResult"></pre>
        </div>
    </div>

<script>
function getToken() {
    return localStorage.getItem("admin_token") || "";
}

function setToken(token) {
    localStorage.setItem("admin_token", token);
}

function clearToken() {
    localStorage.removeItem("admin_token");
}

async function apiGet(url) {
    const token = getToken();
    const res = await fetch(url, {
        headers: { "x-admin-token": token }
    });
    return await res.json();
}

async function apiPost(url, data = {}) {
    const token = getToken();
    const res = await fetch(url, {
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            "x-admin-token": token
        },
        body: JSON.stringify(data)
    });
    return await res.json();
}

async function apiDelete(url) {
    const token = getToken();
    const res = await fetch(url, {
        method: "DELETE",
        headers: {
            "x-admin-token": token
        }
    });
    return await res.json();
}

function showLoginOnly() {
    document.getElementById("loginView").classList.remove("hidden");
    document.getElementById("appView").classList.add("hidden");
}

function showAppOnly() {
    document.getElementById("loginView").classList.add("hidden");
    document.getElementById("appView").classList.remove("hidden");
}

async function verifySessionAndLoad() {
    const token = getToken();
    if (!token) {
        showLoginOnly();
        return;
    }

    const me = await apiGet("/admin/me");
    if (!me.ok) {
        clearToken();
        showLoginOnly();
        return;
    }

    document.getElementById("welcomeBar").textContent =
        "Logged in as " + me.username + " | Session created at " + (me.created_at || "-");

    showAppOnly();
    await loadAll();
}

async function loginAdmin() {
    const username = document.getElementById("adminUsername").value.trim();
    const password = document.getElementById("adminPassword").value.trim();

    const res = await fetch("/admin/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password })
    });

    const result = await res.json();
    document.getElementById("loginResult").textContent = JSON.stringify(result, null, 2);

    if (result.ok && result.token) {
        setToken(result.token);
        await verifySessionAndLoad();
    } else {
        showLoginOnly();
    }
}

async function logoutAdmin() {
    const token = getToken();
    if (token) {
        await apiPost("/admin/logout", {});
    }
    clearToken();
    showLoginOnly();
}

async function loadDashboard() {
    const result = await apiGet("/admin/dashboard");
    if (!result.ok) {
        document.getElementById("dashboardStats").innerHTML = "<pre>" + JSON.stringify(result, null, 2) + "</pre>";
        return;
    }

    document.getElementById("dashboardStats").innerHTML = `
        Total licenses: <b>${result.total_licenses}</b> |
        Active licenses: <b>${result.active_licenses}</b> |
        Total activations: <b>${result.total_activations}</b> |
        Online clients: <b>${result.online_clients}</b> |
        Server time: <b>${result.server_time}</b>
    `;
}

async function createLicense() {
    const license_key = document.getElementById("createLicenseKey").value.trim();
    const name = document.getElementById("createName").value.trim();
    const expires_at = document.getElementById("createExpiresAt").value.trim();
    const max_accounts = parseInt(document.getElementById("createMaxAccounts").value.trim() || "1");
    const note = document.getElementById("createNote").value.trim();

    const result = await apiPost("/admin/create-license", {
        license_key,
        name,
        expires_at,
        max_accounts,
        note
    });

    document.getElementById("createResult").textContent = JSON.stringify(result, null, 2);

    if (result.ok) {
        document.getElementById("createLicenseKey").value = "";
        document.getElementById("createName").value = "";
        document.getElementById("createNote").value = "";
    }

    await loadAll();
}

function clearSearch() {
    document.getElementById("searchInput").value = "";
    loadLicenses();
}

function statusBadge(status) {
    const cls = status === "active" ? "active" : "inactive";
    return `<span class="badge ${cls}">${status}</span>`;
}

async function loadLicenses() {
    const q = document.getElementById("searchInput").value.trim();
    const result = await apiGet("/admin/licenses?q=" + encodeURIComponent(q));

    if (!result.ok) {
        document.getElementById("licensesTable").innerHTML = "<pre>" + JSON.stringify(result, null, 2) + "</pre>";
        return;
    }

    let html = `
    <table>
      <tr>
        <th>Name</th>
        <th>License Key</th>
        <th>Status</th>
        <th>Expires</th>
        <th>Max Acc</th>
        <th>Latest Login</th>
        <th>Balance</th>
        <th>Last Seen</th>
        <th>Note</th>
        <th>Actions</th>
      </tr>
    `;

    for (const lic of result.licenses) {
        html += `
        <tr>
            <td>${escapeHtml(lic.name || "")}</td>
            <td>${escapeHtml(lic.license_key)}</td>
            <td>${statusBadge(lic.status)}</td>
            <td>${escapeHtml(lic.expires_at || "")}</td>
            <td>${lic.max_accounts}</td>
            <td>${escapeHtml(lic.latest_account_login || "")}</td>
            <td>${lic.latest_balance != null ? Number(lic.latest_balance).toFixed(2) : ""}</td>
            <td>${escapeHtml(lic.last_seen_at || "")}</td>
            <td>${escapeHtml(lic.note || "")}</td>
            <td class="actions">
                <button onclick="openEditModal('${jsq(lic.license_key)}')">Edit</button>
                <button class="green" onclick="quickStatus('${jsq(lic.license_key)}','active')">Activate</button>
                <button class="red" onclick="quickStatus('${jsq(lic.license_key)}','inactive')">Deactivate</button>
                <button class="orange" onclick="quickExtend('${jsq(lic.license_key)}',30)">+30d</button>
            </td>
        </tr>
        `;
    }

    html += "</table>";
    document.getElementById("licensesTable").innerHTML = html;
}

async function quickStatus(licenseKey, status) {
    const result = await apiPost(`/admin/license/${encodeURIComponent(licenseKey)}/update`, {
        status: status
    });
    if (!result.ok) alert(result.message || "Update failed");
    await loadAll();
}

async function quickExtend(licenseKey, days) {
    const result = await apiPost(`/admin/license/${encodeURIComponent(licenseKey)}/extend`, {
        days: days
    });
    if (!result.ok) alert(result.message || "Extend failed");
    await loadAll();
}

async function loadOnlineClients() {
    const result = await apiGet("/admin/online-clients");

    if (!result.ok) {
        document.getElementById("onlineClientsTable").innerHTML = "<pre>" + JSON.stringify(result, null, 2) + "</pre>";
        return;
    }

    let html = `
    <table>
      <tr>
        <th>Name</th>
        <th>License</th>
        <th>Account Login</th>
        <th>Broker</th>
        <th>Balance</th>
        <th>Last Seen</th>
        <th>Age</th>
      </tr>
    `;
    for (const row of result.clients) {
        html += `
        <tr>
          <td>${escapeHtml(row.name || "")}</td>
          <td>${escapeHtml(row.license_key)}</td>
          <td>${escapeHtml(row.account_login || "")}</td>
          <td>${escapeHtml(row.broker_server || "")}</td>
          <td>${row.balance != null ? Number(row.balance).toFixed(2) : ""}</td>
          <td>${escapeHtml(row.last_seen_at || "")}</td>
          <td>${row.age_sec >= 0 ? row.age_sec + "s" : ""}</td>
        </tr>
        `;
    }
    html += "</table>";
    document.getElementById("onlineClientsTable").innerHTML = html;
}

async function loadActivations() {
    const result = await apiGet("/admin/activations");

    if (!result.ok) {
        document.getElementById("activationsTable").innerHTML = "<pre>" + JSON.stringify(result, null, 2) + "</pre>";
        return;
    }

    let html = `
    <table>
      <tr>
        <th>Name</th>
        <th>License</th>
        <th>Account</th>
        <th>Broker</th>
        <th>Machine</th>
        <th>Balance</th>
        <th>Created</th>
        <th>Last Seen</th>
      </tr>
    `;
    for (const row of result.activations) {
        html += `
        <tr>
            <td>${escapeHtml(row.name || "")}</td>
            <td>${escapeHtml(row.license_key)}</td>
            <td>${escapeHtml(row.account_login)}</td>
            <td>${escapeHtml(row.broker_server)}</td>
            <td>${escapeHtml(row.machine_id)}</td>
            <td>${row.balance != null ? Number(row.balance).toFixed(2) : ""}</td>
            <td>${escapeHtml(row.created_at)}</td>
            <td>${escapeHtml(row.last_seen_at)}</td>
        </tr>
        `;
    }
    html += "</table>";
    document.getElementById("activationsTable").innerHTML = html;
}

async function loadAll() {
    await loadDashboard();
    await loadLicenses();
    await loadOnlineClients();
    await loadActivations();
}

async function openEditModal(licenseKey) {
    const result = await apiGet(`/admin/license/${encodeURIComponent(licenseKey)}`);
    if (!result.ok) {
        alert(result.message || "Failed to load license");
        return;
    }

    const lic = result.license;
    document.getElementById("editOriginalKey").value = lic.license_key;
    document.getElementById("editLicenseKey").value = lic.license_key || "";
    document.getElementById("editName").value = lic.name || "";
    document.getElementById("editStatus").value = lic.status || "active";
    document.getElementById("editExpiresAt").value = lic.expires_at || "";
    document.getElementById("editMaxAccounts").value = lic.max_accounts || 1;
    document.getElementById("editNote").value = lic.note || "";
    document.getElementById("editResult").textContent = "";

    let html = "<table><tr><th>Account</th><th>Broker</th><th>Machine</th><th>Balance</th><th>Created</th><th>Last Seen</th></tr>";
    for (const a of result.activations) {
        html += `
        <tr>
          <td>${escapeHtml(a.account_login || "")}</td>
          <td>${escapeHtml(a.broker_server || "")}</td>
          <td>${escapeHtml(a.machine_id || "")}</td>
          <td>${a.balance != null ? Number(a.balance).toFixed(2) : ""}</td>
          <td>${escapeHtml(a.created_at || "")}</td>
          <td>${escapeHtml(a.last_seen_at || "")}</td>
        </tr>`;
    }
    html += "</table>";
    document.getElementById("editActivations").innerHTML = html;

    document.getElementById("editModalWrap").style.display = "flex";
}

function closeEditModal() {
    document.getElementById("editModalWrap").style.display = "none";
}

function currentEditKey() {
    return document.getElementById("editOriginalKey").value.trim();
}

async function saveLicenseEdit() {
    const originalKey = currentEditKey();

    const payload = {
        new_license_key: document.getElementById("editLicenseKey").value.trim(),
        name: document.getElementById("editName").value.trim(),
        status: document.getElementById("editStatus").value,
        expires_at: document.getElementById("editExpiresAt").value.trim(),
        max_accounts: parseInt(document.getElementById("editMaxAccounts").value.trim() || "1"),
        note: document.getElementById("editNote").value.trim()
    };

    const result = await apiPost(`/admin/license/${encodeURIComponent(originalKey)}/update`, payload);
    document.getElementById("editResult").textContent = JSON.stringify(result, null, 2);

    if (result.ok) {
        document.getElementById("editOriginalKey").value = result.license_key;
        await loadAll();
        await openEditModal(result.license_key);
    }
}

async function extendCurrentLicense(days) {
    const key = currentEditKey();
    const result = await apiPost(`/admin/license/${encodeURIComponent(key)}/extend`, { days });
    document.getElementById("editResult").textContent = JSON.stringify(result, null, 2);
    if (result.ok) {
        document.getElementById("editExpiresAt").value = result.expires_at;
        await loadAll();
    }
}

async function resetActivationsCurrent() {
    const key = currentEditKey();
    if (!confirm("Reset all activations for this license?")) return;

    const result = await apiPost(`/admin/license/${encodeURIComponent(key)}/reset-activations`, {});
    document.getElementById("editResult").textContent = JSON.stringify(result, null, 2);
    if (result.ok) {
        await loadAll();
        await openEditModal(key);
    }
}

async function deleteCurrentLicense() {
    const key = currentEditKey();
    if (!confirm("Delete this license permanently?")) return;

    const result = await apiDelete(`/admin/license/${encodeURIComponent(key)}`);
    document.getElementById("editResult").textContent = JSON.stringify(result, null, 2);

    if (result.ok) {
        closeEditModal();
        await loadAll();
    }
}

function escapeHtml(str) {
    return String(str ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
}

function jsq(str) {
    return String(str ?? "").replaceAll("'", "\\\\'");
}

verifySessionAndLoad();
</script>
</body>
</html>
    """
