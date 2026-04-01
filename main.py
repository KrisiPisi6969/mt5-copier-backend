from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime, timezone
import sqlite3
import os
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
# Public Routes
# =============================
@app.get("/")
def root():
    return {
        "ok": True,
        "service": "mt5 copier api",
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
    LATEST_SNAPSHOT["timestamp"] = int(datetime.now(timezone.utc).timestamp())
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

@app.get("/admin/dashboard")
def admin_dashboard(x_admin_token: Optional[str] = Header(None)):
    require_admin_token(x_admin_token)

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) AS cnt FROM licenses")
    total_licenses = cur.fetchone()["cnt"]

    cur.execute("SELECT COUNT(*) AS cnt FROM licenses WHERE status = 'active'")
    active_licenses = cur.fetchone()["cnt"]

    cur.execute("SELECT COUNT(*) AS cnt FROM activations")
    total_activations = cur.fetchone()["cnt"]

    cur.execute("""
    SELECT COUNT(*) AS cnt
    FROM activations
    WHERE last_seen_at >= datetime('now', '-2 minutes')
    """)
    online_clients = cur.fetchone()["cnt"]

    conn.close()

    return {
        "ok": True,
        "total_licenses": total_licenses,
        "active_licenses": active_licenses,
        "total_activations": total_activations,
        "online_clients": online_clients,
        "server_time": utc_now_str()
    }

@app.get("/admin/licenses")
def admin_list_licenses(x_admin_token: Optional[str] = Header(None)):
    require_admin_token(x_admin_token)

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
def admin_create_license(payload: AdminCreateLicenseRequest, x_admin_token: Optional[str] = Header(None)):
    require_admin_token(x_admin_token)

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
def admin_update_license(license_key: str, payload: AdminUpdateLicenseRequest, x_admin_token: Optional[str] = Header(None)):
    require_admin_token(x_admin_token)

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
def admin_list_activations(x_admin_token: Optional[str] = Header(None)):
    require_admin_token(x_admin_token)

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
        h1 { margin-top:0; }
        .card { background:white; border-radius:12px; padding:16px; margin-bottom:16px; box-shadow:0 2px 10px rgba(0,0,0,0.08); }
        input, button, textarea { padding:10px; margin:6px 0; font-size:14px; }
        input { width:100%; box-sizing:border-box; }
        button { cursor:pointer; border:none; border-radius:8px; background:#2563eb; color:white; padding:10px 14px; }
        button.red { background:#dc2626; }
        button.green { background:#16a34a; }
        button.gray { background:#6b7280; }
        table { width:100%; border-collapse:collapse; margin-top:10px; }
        th, td { border-bottom:1px solid #ddd; padding:10px; text-align:left; vertical-align:top; }
        .row { display:grid; grid-template-columns:1fr 1fr 1fr; gap:12px; }
        .small { color:#555; font-size:13px; }
        pre { background:#111827; color:#e5e7eb; padding:12px; border-radius:8px; overflow:auto; }
    </style>
</head>
<body>
    <h1>MT5 Copier Admin Panel</h1>

<div class="card">
    <h3>Admin Login</h3>
    <div class="row">
        <div>
            <label>Username</label>
           <input id="adminUsername" type="text" placeholder="Username">
        </div>
        <div>
            <label>Password</label>
            <input id="adminPassword" type="password" placeholder="Password">
        </div>
        <div>
            <label>&nbsp;</label><br>
            <button onclick="loginAdmin()">Login</button>
        </div>
    </div>
    <div class="small">Current API: same server</div>
    <pre id="loginResult"></pre>
</div>

    <div class="card">
        <h3>Create License</h3>
        <div class="row">
            <div>
                <label>Expires At</label>
                <input id="expiresAt" value="2026-12-31 23:59:59">
            </div>
            <div>
                <label>Max Accounts</label>
                <input id="maxAccounts" value="1">
            </div>
            <div>
                <label>Note</label>
                <input id="note" value="Test client">
            </div>
        </div>
        <button onclick="createLicense()">Create License</button>
        <pre id="createResult"></pre>
    </div>

    <div class="card">
        <h3>Licenses</h3>
        <button onclick="loadLicenses()">Refresh Licenses</button>
        <div id="licensesTable"></div>
    </div>

    <div class="card">
        <h3>Activations</h3>
        <button onclick="loadActivations()">Refresh Activations</button>
        <div id="activationsTable"></div>
    </div>

<script>
function getToken() {
    return localStorage.getItem("admin_token") || "";
}

async function loginAdmin() {
    const username = document.getElementById("adminUsername").value.trim();
    const password = document.getElementById("adminPassword").value.trim();

    const res = await fetch("/admin/login", {
        method: "POST",
        headers: {
            "Content-Type": "application/json"
        },
        body: JSON.stringify({
            username: username,
            password: password
        })
    });

    const result = await res.json();
    document.getElementById("loginResult").textContent = JSON.stringify(result, null, 2);

    if (result.ok && result.token) {
        localStorage.setItem("admin_token", result.token);
        alert("Login successful.");
    } else {
        alert("Login failed.");
    }
}

async function apiGet(url) {
    const token = getToken();
    const res = await fetch(url, {
        headers: {
            "x-admin-token": token
        }
    });
    return await res.json();
}

async function apiPost(url, data) {
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

async function createLicense() {
    const expiresAt = document.getElementById("expiresAt").value.trim();
    const maxAccounts = parseInt(document.getElementById("maxAccounts").value.trim() || "1");
    const note = document.getElementById("note").value.trim();

    const result = await apiPost("/admin/create-license", {
        expires_at: expiresAt,
        max_accounts: maxAccounts,
        note: note
    });

    document.getElementById("createResult").textContent = JSON.stringify(result, null, 2);
    loadLicenses();
}

async function loadLicenses() {
    const result = await apiGet("/admin/licenses");

    if (!result.ok) {
        document.getElementById("licensesTable").innerHTML = "<pre>" + JSON.stringify(result, null, 2) + "</pre>";
        return;
    }

    let html = "<table><tr><th>Key</th><th>Status</th><th>Expires</th><th>Max</th><th>Note</th><th>Actions</th></tr>";
    for (const lic of result.licenses) {
        html += `
        <tr>
            <td>${lic.license_key}</td>
            <td>${lic.status}</td>
            <td>${lic.expires_at || ""}</td>
            <td>${lic.max_accounts}</td>
            <td>${lic.note || ""}</td>
            <td>
                <button class="green" onclick="setLicenseStatus('${lic.license_key}', 'active')">Activate</button>
                <button class="red" onclick="setLicenseStatus('${lic.license_key}', 'inactive')">Deactivate</button>
            </td>
        </tr>`;
    }
    html += "</table>";
    document.getElementById("licensesTable").innerHTML = html;
}

async function setLicenseStatus(licenseKey, status) {
    const result = await apiPost(`/admin/license/${licenseKey}/update`, {
        status: status
    });
    alert(JSON.stringify(result, null, 2));
    loadLicenses();
}

async function loadActivations() {
    const result = await apiGet("/admin/activations");

    if (!result.ok) {
        document.getElementById("activationsTable").innerHTML = "<pre>" + JSON.stringify(result, null, 2) + "</pre>";
        return;
    }

    let html = "<table><tr><th>License</th><th>Account</th><th>Broker</th><th>Machine</th><th>Created</th><th>Last Seen</th></tr>";
    for (const row of result.activations) {
        html += `
        <tr>
            <td>${row.license_key}</td>
            <td>${row.account_login}</td>
            <td>${row.broker_server}</td>
            <td>${row.machine_id}</td>
            <td>${row.created_at}</td>
            <td>${row.last_seen_at}</td>
        </tr>`;
    }
    html += "</table>";
    document.getElementById("activationsTable").innerHTML = html;
}
</script>
</body>
</html>
    """
