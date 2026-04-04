from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime, timezone, timedelta
import psycopg
from psycopg.rows import dict_row
import secrets
import os
import smtplib
from email.mime.text import MIMEText


app = FastAPI(
    title="MT5 Copier API + Admin Panel + Logs",
    docs_url=None,
    redoc_url=None,
    openapi_url=None
)

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

LATEST_SNAPSHOT = {
    "snapshot_id": "",
    "timestamp": 0,
    "positions": [],
    "pending_orders": []
}

VALID_MASTER_TOKEN = "MASTER123"

ADMIN_SESSIONS = {}
ADMIN_LOGIN_CHALLENGES = {}

ADMIN_SESSION_TTL_MINUTES = 30
ADMIN_OTP_TTL_MINUTES = 5
ADMIN_OTP_MAX_ATTEMPTS = 3


# =============================
# ENV helpers
# =============================
def get_admin_otp_enabled() -> bool:
    return env_bool("ADMIN_OTP_ENABLED", False)


def env_str(name: str, default: str = "") -> str:
    value = os.getenv(name, default)
    if value is None:
        return default
    return str(value).strip()


def env_bool(name: str, default: bool = False) -> bool:
    value = env_str(name, "")
    if value == "":
        return default
    return value.lower() in ("1", "true", "yes", "on")


def env_int(name: str, default: int) -> int:
    value = env_str(name, "")
    if value == "":
        return default
    try:
        return int(value)
    except Exception:
        return default


def get_admin_username() -> str:
    return env_str("ADMIN_USERNAME", "admin")


def get_admin_password() -> str:
    return env_str("ADMIN_PASSWORD", "rcx123")


def get_admin_otp_email() -> str:
    return env_str("ADMIN_OTP_EMAIL", "")


def get_smtp_host() -> str:
    return env_str("SMTP_HOST", "")


def get_smtp_port() -> int:
    return env_int("SMTP_PORT", 587)


def get_smtp_username() -> str:
    return env_str("SMTP_USERNAME", "")


def get_smtp_password() -> str:
    return env_str("SMTP_PASSWORD", "")


def get_smtp_from() -> str:
    smtp_from = env_str("SMTP_FROM", "")
    if smtp_from != "":
        return smtp_from
    return get_smtp_username()


def get_smtp_use_tls() -> bool:
    return env_bool("SMTP_USE_TLS", True)


# =============================
# Helpers
# =============================
def utc_now():
    return datetime.now(timezone.utc)


def utc_now_ts() -> int:
    return int(utc_now().timestamp())


def utc_now_str():
    return utc_now().strftime("%Y-%m-%d %H:%M:%S")


def parse_datetime_to_utc(dt_value: Optional[str]) -> Optional[datetime]:
    """
    Normalizes several incoming datetime formats to timezone-aware UTC datetime.

    Accepted examples:
    - 2026-04-04 15:00:00        (already UTC DB/admin payload)
    - 2026-04-04T15:00:00
    - 2026-04-04T15:00:00Z
    - 2026-04-04T18:00:00+03:00
    """
    if not dt_value:
        return None

    raw = str(dt_value).strip()
    if raw == "":
        return None

    try:
        # Handle trailing Z explicitly
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"

        # ISO datetime with offset
        if "T" in raw or "+" in raw[10:] or raw.count("-") > 2:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                # Treat naive admin/db strings as UTC
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)

        # Standard DB format: UTC string
        dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def normalize_utc_db_str(dt_value: Optional[str]) -> Optional[str]:
    dt = parse_datetime_to_utc(dt_value)
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def dt_str_to_unix(expires_at: Optional[str]) -> int:
    dt = parse_datetime_to_utc(expires_at)
    if dt is None:
        return 0
    return int(dt.timestamp())


def parse_utc_db_time(dt_str: Optional[str]) -> Optional[datetime]:
    return parse_datetime_to_utc(dt_str)


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


def time_left_seconds(expires_at: Optional[str]) -> Optional[int]:
    dt = parse_utc_db_time(expires_at)
    if dt is None:
        return None
    return int((dt - utc_now()).total_seconds())


def format_time_left_human(expires_at: Optional[str]) -> str:
    secs = time_left_seconds(expires_at)
    if secs is None:
        return "-"
    if secs <= 0:
        return "Expired"

    days = secs // 86400
    hours = (secs % 86400) // 3600
    mins = (secs % 3600) // 60

    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def online_status_from_last_seen(last_seen_at: Optional[str]) -> str:
    age = seconds_ago_from_str(last_seen_at)
    if age < 0:
        return "offline"
    if age <= 120:
        return "online"
    if age <= 600:
        return "stale"
    return "offline"


def effective_license_status(db_status: str, expires_at: Optional[str]) -> str:
    if db_status != "active":
        return db_status
    secs = time_left_seconds(expires_at)
    if secs is not None and secs <= 0:
        return "expired"
    return "active"


def smtp_config_debug() -> dict:
    return {
        "ADMIN_OTP_EMAIL": get_admin_otp_email(),
        "SMTP_HOST": get_smtp_host(),
        "SMTP_PORT": get_smtp_port(),
        "SMTP_USERNAME": get_smtp_username(),
        "SMTP_FROM": get_smtp_from(),
        "SMTP_USE_TLS": get_smtp_use_tls(),
        "HAS_SMTP_PASSWORD": get_smtp_password() != "",
    }


def write_log(event_type: str,
              license_key: str = "",
              account_login: str = "",
              broker_server: str = "",
              machine_id: str = "",
              status: str = "",
              message: str = "",
              actor: str = ""):
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
        INSERT INTO license_logs (
            created_at, event_type, license_key, account_login, broker_server,
            machine_id, status, message, actor
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            utc_now_str(),
            event_type,
            license_key,
            account_login,
            broker_server,
            machine_id,
            status,
            message,
            actor
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        print("write_log failed:", str(e))


def should_write_log(event_type: str,
                     license_key: str = "",
                     account_login: str = "",
                     broker_server: str = "",
                     machine_id: str = "",
                     status: str = "",
                     message: str = "",
                     cooldown_seconds: int = 60) -> bool:
    try:
        conn = get_conn()
        cur = conn.cursor()
        threshold = (utc_now() - timedelta(seconds=cooldown_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        cur.execute("""
        SELECT id
        FROM license_logs
        WHERE event_type = %s
          AND license_key = %s
          AND account_login = %s
          AND broker_server = %s
          AND machine_id = %s
          AND status = %s
          AND message = %s
          AND created_at >= %s
        ORDER BY id DESC
        LIMIT 1
        """, (
            event_type,
            license_key,
            account_login,
            broker_server,
            machine_id,
            status,
            message,
            threshold
        ))
        row = cur.fetchone()
        conn.close()
        return row is None
    except Exception as e:
        print("should_write_log failed:", str(e))
        return True


def cleanup_old_logs(days: int = 7) -> int:
    try:
        cutoff = (utc_now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM license_logs WHERE created_at < %s", (cutoff,))
        deleted = cur.rowcount
        conn.commit()
        conn.close()
        return int(deleted or 0)
    except Exception as e:
        print("cleanup_old_logs failed:", str(e))
        return 0


def clear_all_logs() -> int:
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM license_logs")
        deleted = cur.rowcount
        conn.commit()
        conn.close()
        return int(deleted or 0)
    except Exception as e:
        print("clear_all_logs failed:", str(e))
        return 0


def cleanup_old_errors(days: int = 7) -> int:
    try:
        cutoff = (utc_now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM slave_errors WHERE created_at < %s", (cutoff,))
        deleted = cur.rowcount
        conn.commit()
        conn.close()
        return int(deleted or 0)
    except Exception as e:
        print("cleanup_old_errors failed:", str(e))
        return 0


def clear_all_errors() -> int:
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM slave_errors")
        deleted = cur.rowcount
        conn.commit()
        conn.close()
        return int(deleted or 0)
    except Exception as e:
        print("clear_all_errors failed:", str(e))
        return 0


def should_write_slave_error(license_key: str = "",
                             account_login: str = "",
                             broker_server: str = "",
                             machine_id: str = "",
                             category: str = "",
                             severity: str = "",
                             symbol: str = "",
                             code: str = "",
                             message: str = "",
                             cooldown_seconds: int = 60) -> bool:
    try:
        conn = get_conn()
        cur = conn.cursor()
        threshold = (utc_now() - timedelta(seconds=cooldown_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        cur.execute("""
        SELECT id
        FROM slave_errors
        WHERE license_key = %s
          AND account_login = %s
          AND broker_server = %s
          AND machine_id = %s
          AND category = %s
          AND severity = %s
          AND symbol = %s
          AND code = %s
          AND message = %s
          AND created_at >= %s
        ORDER BY id DESC
        LIMIT 1
        """, (
            license_key,
            account_login,
            broker_server,
            machine_id,
            category,
            severity,
            symbol,
            code,
            message,
            threshold
        ))
        row = cur.fetchone()
        conn.close()
        return row is None
    except Exception as e:
        print("should_write_slave_error failed:", str(e))
        return True


def write_slave_error(license_key: str = "",
                      account_login: str = "",
                      broker_server: str = "",
                      machine_id: str = "",
                      category: str = "",
                      severity: str = "error",
                      symbol: str = "",
                      code: str = "",
                      message: str = "",
                      details: str = "",
                      snapshot_id: str = ""):
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
        INSERT INTO slave_errors (
            created_at, license_key, account_login, broker_server, machine_id,
            category, severity, symbol, code, message, details, snapshot_id
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            utc_now_str(),
            license_key,
            account_login,
            broker_server,
            machine_id,
            category,
            severity,
            symbol,
            code,
            message,
            details,
            snapshot_id
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        print("write_slave_error failed:", str(e))


def send_email_code(to_email: str, code: str) -> tuple[bool, str]:
    smtp_host = get_smtp_host()
    smtp_port = get_smtp_port()
    smtp_username = get_smtp_username()
    smtp_password = get_smtp_password()
    smtp_from = get_smtp_from()
    smtp_use_tls = get_smtp_use_tls()

    if not to_email:
        return False, "Missing ADMIN_OTP_EMAIL"

    if not smtp_host:
        return False, "SMTP is not configured: missing SMTP_HOST"

    if not smtp_from:
        return False, "SMTP is not configured: missing SMTP_FROM or SMTP_USERNAME"

    if not smtp_username:
        return False, "SMTP is not configured: missing SMTP_USERNAME"

    if not smtp_password:
        return False, "SMTP is not configured: missing SMTP_PASSWORD"

    subject = "Your admin verification code"
    body = f"Your verification code is: {code}\n\nThis code expires in {ADMIN_OTP_TTL_MINUTES} minutes."

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = smtp_from
    msg["To"] = to_email

    server = None
    try:
        print(f"SMTP DEBUG: connecting to {smtp_host}:{smtp_port} tls={smtp_use_tls}")

        server = smtplib.SMTP(smtp_host, smtp_port, timeout=10)
        server.ehlo()

        if smtp_use_tls:
            print("SMTP DEBUG: starting TLS")
            server.starttls()
            server.ehlo()

        print(f"SMTP DEBUG: logging in as {smtp_username}")
        server.login(smtp_username, smtp_password)

        print(f"SMTP DEBUG: sending mail to {to_email}")
        server.sendmail(smtp_from, [to_email], msg.as_string())
        print("SMTP DEBUG: mail sent OK")

        return True, "Verification code sent"

    except smtplib.SMTPAuthenticationError as e:
        return False, f"SMTP auth failed: {str(e)}"
    except smtplib.SMTPConnectError as e:
        return False, f"SMTP connect failed: {str(e)}"
    except smtplib.SMTPServerDisconnected as e:
        return False, f"SMTP server disconnected: {str(e)}"
    except TimeoutError:
        return False, "SMTP timeout while connecting"
    except Exception as e:
        return False, f"Failed to send verification email: {str(e)}"
    finally:
        try:
            if server is not None:
                server.quit()
        except Exception:
            pass


def create_admin_session_token():
    return secrets.token_hex(24)


def create_login_challenge_id():
    return secrets.token_hex(16)


def cleanup_expired_login_challenges():
    now = utc_now()
    expired_ids = []
    for cid, item in ADMIN_LOGIN_CHALLENGES.items():
        exp = item.get("expires_at")
        if exp is None or exp < now:
            expired_ids.append(cid)
    for cid in expired_ids:
        del ADMIN_LOGIN_CHALLENGES[cid]


def cleanup_expired_admin_sessions():
    now = utc_now()
    expired_tokens = []
    for token, sess in ADMIN_SESSIONS.items():
        last_seen = sess.get("last_seen_at")
        if last_seen is None:
            expired_tokens.append(token)
            continue
        if last_seen + timedelta(minutes=ADMIN_SESSION_TTL_MINUTES) < now:
            expired_tokens.append(token)

    for token in expired_tokens:
        del ADMIN_SESSIONS[token]


def require_admin_token(x_admin_token: Optional[str]):
    cleanup_expired_admin_sessions()

    if not x_admin_token:
        raise HTTPException(status_code=401, detail="Missing admin token")

    sess = ADMIN_SESSIONS.get(x_admin_token)
    if not sess:
        raise HTTPException(status_code=401, detail="Invalid or expired admin token")

    sess["last_seen_at"] = utc_now()


def license_public_payload(lic) -> dict:
    expires_at = lic["expires_at"]
    return {
        "license_key": lic["license_key"],
        "name": lic["name"],
        "status": lic["status"],
        "effective_status": effective_license_status(lic["status"], expires_at),
        "expires_at": expires_at,
        "expires_at_ts": dt_str_to_unix(expires_at),
        "time_left_text": format_time_left_human(expires_at),
        "time_left_seconds": time_left_seconds(expires_at),
        "max_accounts": lic["max_accounts"],
        "note": lic["note"],
        "created_at": lic["created_at"],
        "locked_account_login": lic.get("locked_account_login"),
        "locked_broker_server": lic.get("locked_broker_server"),
        "locked_at": lic.get("locked_at"),
    }


# =============================
# Database helpers
# =============================
def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("Missing DATABASE_URL environment variable")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def ensure_column(cur, table_name: str, column_name: str, column_type: str):
    cur.execute("""
    SELECT 1
    FROM information_schema.columns
    WHERE table_name = %s AND column_name = %s
    """, (table_name, column_name))
    if not cur.fetchone():
        cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS licenses (
                id BIGSERIAL PRIMARY KEY,
                license_key TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                expires_at TEXT,
                max_accounts INTEGER NOT NULL DEFAULT 1,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """)

            cur.execute("""
            CREATE TABLE IF NOT EXISTS activations (
                id BIGSERIAL PRIMARY KEY,
                license_id BIGINT NOT NULL REFERENCES licenses(id) ON DELETE CASCADE,
                account_login TEXT NOT NULL,
                broker_server TEXT NOT NULL,
                machine_id TEXT NOT NULL,
                balance DOUBLE PRECISION NOT NULL DEFAULT 0,
                equity DOUBLE PRECISION NOT NULL DEFAULT 0,
                open_positions_count INTEGER NOT NULL DEFAULT 0,
                floating_pnl DOUBLE PRECISION NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                UNIQUE(license_id, account_login, broker_server, machine_id)
            )
            """)

            cur.execute("""
            CREATE TABLE IF NOT EXISTS license_logs (
                id BIGSERIAL PRIMARY KEY,
                created_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                license_key TEXT NOT NULL DEFAULT '',
                account_login TEXT NOT NULL DEFAULT '',
                broker_server TEXT NOT NULL DEFAULT '',
                machine_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT '',
                actor TEXT NOT NULL DEFAULT ''
            )
            """)

            cur.execute("""
            CREATE TABLE IF NOT EXISTS slave_errors (
                id BIGSERIAL PRIMARY KEY,
                created_at TEXT NOT NULL,
                license_key TEXT NOT NULL DEFAULT '',
                account_login TEXT NOT NULL DEFAULT '',
                broker_server TEXT NOT NULL DEFAULT '',
                machine_id TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT '',
                severity TEXT NOT NULL DEFAULT 'error',
                symbol TEXT NOT NULL DEFAULT '',
                code TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT '',
                details TEXT NOT NULL DEFAULT '',
                snapshot_id TEXT NOT NULL DEFAULT ''
            )
            """)

            ensure_column(cur, "licenses", "locked_account_login", "TEXT")
            ensure_column(cur, "licenses", "locked_broker_server", "TEXT")
            ensure_column(cur, "licenses", "locked_at", "TEXT")

            ensure_column(cur, "activations", "balance", "DOUBLE PRECISION NOT NULL DEFAULT 0")
            ensure_column(cur, "activations", "equity", "DOUBLE PRECISION NOT NULL DEFAULT 0")
            ensure_column(cur, "activations", "open_positions_count", "INTEGER NOT NULL DEFAULT 0")
            ensure_column(cur, "activations", "floating_pnl", "DOUBLE PRECISION NOT NULL DEFAULT 0")
            
            cur.execute("CREATE INDEX IF NOT EXISTS idx_licenses_license_key ON licenses(license_key)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_activations_license_id ON activations(license_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_activations_last_seen_at ON activations(last_seen_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_license_logs_created_at ON license_logs(created_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_license_logs_license_key ON license_logs(license_key)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_slave_errors_created_at ON slave_errors(created_at)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_slave_errors_license_key ON slave_errors(license_key)")

        conn.commit()


def seed_test_license():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT id FROM licenses WHERE license_key = %s", ("TEST-001",))
    row = cur.fetchone()

    if row is None:
        cur.execute("""
        INSERT INTO licenses (license_key, name, status, expires_at, max_accounts, note, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
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
    cur.execute("SELECT * FROM licenses WHERE license_key = %s", (license_key,))
    row = cur.fetchone()
    conn.close()
    return row


def is_license_expired(expires_at: Optional[str]) -> bool:
    secs = time_left_seconds(expires_at)
    return secs is not None and secs <= 0


def get_activation_by_login(license_id: int, account_login: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT * FROM activations
    WHERE license_id = %s AND account_login = %s
    """, (license_id, account_login))
    row = cur.fetchone()
    conn.close()
    return row


def get_activation_exact(license_id: int, account_login: str, broker_server: str, machine_id: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT * FROM activations
    WHERE license_id = %s AND account_login = %s AND broker_server = %s AND machine_id = %s
    """, (license_id, account_login, broker_server, machine_id))
    row = cur.fetchone()
    conn.close()
    return row


def refresh_activation_seen(license_id: int, account_login: str, broker_server: str, machine_id: str,
                            balance: float = 0.0, equity: float = 0.0,
                            open_positions_count: int = 0, floating_pnl: float = 0.0):
    conn = get_conn()
    cur = conn.cursor()

    exact = get_activation_exact(license_id, account_login, broker_server, machine_id)
    if exact:
        cur.execute("""
        UPDATE activations
        SET last_seen_at = %s, balance = %s, equity = %s, open_positions_count = %s, floating_pnl = %s
        WHERE id = %s
        """, (utc_now_str(), balance, equity, int(open_positions_count or 0), float(floating_pnl or 0.0), exact["id"]))
    else:
        same_login = get_activation_by_login(license_id, account_login)
        if same_login:
            cur.execute("""
            UPDATE activations
            SET broker_server = %s, machine_id = %s, last_seen_at = %s, balance = %s, equity = %s,
                open_positions_count = %s, floating_pnl = %s
            WHERE id = %s
            """, (broker_server, machine_id, utc_now_str(), balance, equity, int(open_positions_count or 0), float(floating_pnl or 0.0), same_login["id"]))
        else:
            cur.execute("""
            INSERT INTO activations (
                license_id, account_login, broker_server, machine_id,
                balance, equity, open_positions_count, floating_pnl, created_at, last_seen_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                license_id, account_login, broker_server, machine_id,
                balance, equity, int(open_positions_count or 0), float(floating_pnl or 0.0),
                utc_now_str(), utc_now_str()
            ))

    conn.commit()
    conn.close()


def refresh_last_seen_by_license(license_key: str,
                                 account_login: Optional[str] = None,
                                 broker_server: Optional[str] = None,
                                 machine_id: Optional[str] = None,
                                 balance: Optional[float] = None,
                                 equity: Optional[float] = None,
                                 open_positions_count: Optional[int] = None,
                                 floating_pnl: Optional[float] = None):
    lic = get_license_by_key(license_key)
    if not lic:
        return

    conn = get_conn()
    cur = conn.cursor()

    if account_login:
        cur.execute("""
        SELECT * FROM activations
        WHERE license_id = %s AND account_login = %s
        """, (lic["id"], account_login))
        row = cur.fetchone()

        if row:
            cur.execute("""
            UPDATE activations
            SET last_seen_at = %s,
                broker_server = COALESCE(%s, broker_server),
                machine_id = COALESCE(%s, machine_id),
                balance = COALESCE(%s, balance),
                equity = COALESCE(%s, equity),
                open_positions_count = COALESCE(%s, open_positions_count),
                floating_pnl = COALESCE(%s, floating_pnl)
            WHERE id = %s
            """, (utc_now_str(), broker_server, machine_id, balance, equity, open_positions_count, floating_pnl, row["id"]))
            conn.commit()
            conn.close()
            return

    cur.execute("""
    UPDATE activations
    SET last_seen_at = %s
    WHERE license_id = %s
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

    locked_account_login = (lic.get("locked_account_login") or "").strip()
    locked_broker_server = (lic.get("locked_broker_server") or "").strip()

    # First successful activation locks the license to this MT5 account + broker
    if locked_account_login == "":
        cur.execute("""
        UPDATE licenses
        SET locked_account_login = %s,
            locked_broker_server = %s,
            locked_at = %s
        WHERE id = %s
        """, (account_login, broker_server, utc_now_str(), lic["id"]))
        conn.commit()

        cur.execute("SELECT * FROM licenses WHERE id = %s", (lic["id"],))
        lic = cur.fetchone()
        locked_account_login = (lic.get("locked_account_login") or "").strip()
        locked_broker_server = (lic.get("locked_broker_server") or "").strip()
    else:
        if str(locked_account_login) != str(account_login):
            conn.close()
            return False, "License locked to another account", None

        if locked_broker_server != "" and str(locked_broker_server) != str(broker_server):
            conn.close()
            return False, "License locked to another broker", None

    cur.execute("""
    SELECT * FROM activations
    WHERE license_id = %s AND account_login = %s
    """, (lic["id"], account_login))
    same_login = cur.fetchone()

    if same_login:
        cur.execute("""
        UPDATE activations
        SET last_seen_at = %s, broker_server = %s, machine_id = %s
        WHERE id = %s
        """, (utc_now_str(), broker_server, machine_id, same_login["id"]))
        conn.commit()
        conn.close()
        return True, "License is valid", lic

    cur.execute("""
    SELECT COUNT(*) AS cnt
    FROM activations
    WHERE license_id = %s
    """, (lic["id"],))
    cnt = int(cur.fetchone()["cnt"])

    if cnt >= int(lic["max_accounts"]):
        conn.close()
        return False, "Max accounts reached", None

    cur.execute("""
    INSERT INTO activations (
        license_id, account_login, broker_server, machine_id,
        balance, equity, open_positions_count, floating_pnl, created_at, last_seen_at
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (lic["id"], account_login, broker_server, machine_id, 0.0, 0.0, 0, 0.0, utc_now_str(), utc_now_str()))
    conn.commit()
    conn.close()

    return True, "License is valid", lic



def validate_license_access_for_pull(license_key: str,
                                     account_login: Optional[str],
                                     broker_server: Optional[str],
                                     machine_id: Optional[str]):
    lic = get_license_by_key(license_key)
    if not lic:
        return False, "License not found", None

    if lic["status"] != "active":
        return False, "License inactive", None

    if is_license_expired(lic["expires_at"]):
        return False, "License expired", None

    locked_account_login = (lic.get("locked_account_login") or "").strip()
    locked_broker_server = (lic.get("locked_broker_server") or "").strip()

    req_login = (account_login or "").strip()
    req_broker = (broker_server or "").strip()

    if locked_account_login != "" and req_login != "" and locked_account_login != req_login:
        return False, "License locked to another account", None

    if locked_broker_server != "" and req_broker != "" and locked_broker_server != req_broker:
        return False, "License locked to another broker", None

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
class AdminLoginStartRequest(BaseModel):
    username: str
    password: str


class AdminLoginVerifyRequest(BaseModel):
    challenge_id: str
    code: str


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
    account_equity: Optional[float] = 0.0
    account_open_positions_count: Optional[int] = 0
    account_floating_pnl: Optional[float] = 0.0


class SlavePullRequest(BaseModel):
    license_key: str
    last_snapshot_id: str = ""
    account_login: Optional[str] = None
    broker_server: Optional[str] = None
    machine_id: Optional[str] = None
    account_balance: Optional[float] = None
    account_equity: Optional[float] = None
    account_open_positions_count: Optional[int] = None
    account_floating_pnl: Optional[float] = None


class SlaveErrorReportRequest(BaseModel):
    license_key: str
    account_login: Optional[str] = None
    broker_server: Optional[str] = None
    machine_id: Optional[str] = None
    category: str = "copy_error"
    severity: str = "error"
    symbol: Optional[str] = ""
    code: Optional[str] = ""
    message: str
    details: Optional[str] = ""
    snapshot_id: Optional[str] = ""


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
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing. Set it in Render Environment.")
    init_db()
    seed_test_license()
    deleted = cleanup_old_logs(7)
    deleted_errors = cleanup_old_errors(7)
    print("STARTUP ENV DEBUG:", smtp_config_debug())
    print("DATABASE_URL configured =", DATABASE_URL != "")
    print("Old logs cleaned:", deleted)
    print("Old slave errors cleaned:", deleted_errors)


# =============================
# Public Routes
# =============================
@app.get("/")
def root():
    return {
        "ok": True,
        "service": "mt5 copier api",
        "time": utc_now().isoformat(),
        "server_now_utc_ts": utc_now_ts()
    }


@app.get("/debug/env")
def debug_env():
    return {
        "ok": True,
        "env": smtp_config_debug(),
        "server_now_utc_ts": utc_now_ts(),
        "has_database_url": DATABASE_URL != ""
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
        write_log(
            event_type="slave_activate",
            license_key=payload.license_key,
            account_login=payload.account_login,
            broker_server=payload.broker_server,
            machine_id=payload.machine_id,
            status="fail",
            message=message
        )
        return {
            "ok": False,
            "message": message,
            "server_now_utc_ts": utc_now_ts()
        }

    refresh_activation_seen(
        lic["id"],
        payload.account_login,
        payload.broker_server,
        payload.machine_id,
        float(payload.account_balance or 0.0),
        float(payload.account_equity or 0.0),
        int(payload.account_open_positions_count or 0),
        float(payload.account_floating_pnl or 0.0)
    )

    expires_at_ts = dt_str_to_unix(lic["expires_at"])

    write_log(
        event_type="slave_activate",
        license_key=payload.license_key,
        account_login=payload.account_login,
        broker_server=payload.broker_server,
        machine_id=payload.machine_id,
        status="ok",
        message=message
    )

    return {
        "ok": True,
        "message": message,
        "mode": "db",
        "poll_seconds": 1,
        "expires_at": lic["expires_at"],
        "expires_at_ts": expires_at_ts,
        "locked_account_login": lic.get("locked_account_login"),
        "locked_broker_server": lic.get("locked_broker_server"),
        "server_time": utc_now_ts(),
        "server_now_utc_ts": utc_now_ts(),
        "time_left_seconds": time_left_seconds(lic["expires_at"]),
        "max_accounts": lic["max_accounts"]
    }


@app.post("/master/publish")
def master_publish(payload: MasterPublishRequest):
    if payload.master_token != VALID_MASTER_TOKEN:
        return {
            "ok": False,
            "message": "Invalid master token",
            "server_now_utc_ts": utc_now_ts()
        }

    LATEST_SNAPSHOT["snapshot_id"] = payload.snapshot_id
    LATEST_SNAPSHOT["timestamp"] = utc_now_ts()
    LATEST_SNAPSHOT["positions"] = [p.model_dump() for p in payload.positions]
    LATEST_SNAPSHOT["pending_orders"] = [o.model_dump() for o in payload.pending_orders]

    return {
        "ok": True,
        "message": "Snapshot saved",
        "snapshot_id": LATEST_SNAPSHOT["snapshot_id"],
        "timestamp": LATEST_SNAPSHOT["timestamp"],
        "server_now_utc_ts": utc_now_ts()
    }


@app.post("/slave/pull")
def slave_pull(payload: SlavePullRequest):
    ok, message, lic = validate_license_access_for_pull(
        payload.license_key,
        payload.account_login,
        payload.broker_server,
        payload.machine_id
    )

    if not ok:
        if should_write_log(
            event_type="slave_pull",
            license_key=payload.license_key,
            account_login=payload.account_login or "",
            broker_server=payload.broker_server or "",
            machine_id=payload.machine_id or "",
            status="fail",
            message=message,
            cooldown_seconds=60
        ):
            write_log(
                event_type="slave_pull",
                license_key=payload.license_key,
                account_login=payload.account_login or "",
                broker_server=payload.broker_server or "",
                machine_id=payload.machine_id or "",
                status="fail",
                message=message
            )
        return {
            "ok": False,
            "message": message,
            "server_now_utc_ts": utc_now_ts()
        }

    refresh_last_seen_by_license(
        payload.license_key,
        payload.account_login,
        payload.broker_server,
        payload.machine_id,
        payload.account_balance,
        payload.account_equity,
        payload.account_open_positions_count,
        payload.account_floating_pnl
    )

    expires_at_ts = dt_str_to_unix(lic["expires_at"])
    common = {
        "expires_at": lic["expires_at"],
        "expires_at_ts": expires_at_ts,
        "locked_account_login": lic.get("locked_account_login"),
        "locked_broker_server": lic.get("locked_broker_server"),
        "server_now_utc_ts": utc_now_ts(),
        "time_left_seconds": time_left_seconds(lic["expires_at"])
    }

    if payload.last_snapshot_id == LATEST_SNAPSHOT["snapshot_id"]:
        return {
            "ok": True,
            "has_update": False,
            "snapshot_id": payload.last_snapshot_id,
            "timestamp": LATEST_SNAPSHOT["timestamp"],
            **common
        }

    return {
        "ok": True,
        "has_update": True,
        "snapshot_id": LATEST_SNAPSHOT["snapshot_id"],
        "timestamp": LATEST_SNAPSHOT["timestamp"],
        "positions": LATEST_SNAPSHOT["positions"],
        "pending_orders": LATEST_SNAPSHOT["pending_orders"],
        **common
    }


@app.post("/slave/report-error")
def slave_report_error(payload: SlaveErrorReportRequest):
    ok, message, lic = validate_license_access_for_pull(
        payload.license_key,
        payload.account_login,
        payload.broker_server,
        payload.machine_id
    )

    if not ok:
        return {
            "ok": False,
            "message": message,
            "server_now_utc_ts": utc_now_ts()
        }

    if should_write_slave_error(
        license_key=payload.license_key,
        account_login=payload.account_login or "",
        broker_server=payload.broker_server or "",
        machine_id=payload.machine_id or "",
        category=payload.category or "",
        severity=payload.severity or "error",
        symbol=payload.symbol or "",
        code=payload.code or "",
        message=payload.message or "",
        cooldown_seconds=60
    ):
        write_slave_error(
            license_key=payload.license_key,
            account_login=payload.account_login or "",
            broker_server=payload.broker_server or "",
            machine_id=payload.machine_id or "",
            category=payload.category or "",
            severity=payload.severity or "error",
            symbol=payload.symbol or "",
            code=payload.code or "",
            message=payload.message or "",
            details=payload.details or "",
            snapshot_id=payload.snapshot_id or ""
        )

    return {
        "ok": True,
        "message": "Error recorded",
        "server_now_utc_ts": utc_now_ts()
    }


# =============================
# Admin Auth
# =============================
@app.post("/admin/login/start")
def admin_login_start(payload: AdminLoginStartRequest):
    cleanup_expired_login_challenges()

    if payload.username != get_admin_username() or payload.password != get_admin_password():
        return {
            "ok": False,
            "message": "Invalid username or password"
        }

    if not get_admin_otp_enabled():
        token = create_admin_session_token()
        ADMIN_SESSIONS[token] = {
            "username": payload.username,
            "created_at": utc_now(),
            "last_seen_at": utc_now()
        }

        return {
            "ok": True,
            "message": "Login successful (OTP disabled)",
            "token": token,
            "username": payload.username,
            "otp_required": False
        }

    otp_email = get_admin_otp_email()
    if not otp_email:
        return {
            "ok": False,
            "message": "Missing ADMIN_OTP_EMAIL"
        }

    code = f"{secrets.randbelow(1000000):06d}"
    challenge_id = create_login_challenge_id()
    expires_at = utc_now() + timedelta(minutes=ADMIN_OTP_TTL_MINUTES)

    ADMIN_LOGIN_CHALLENGES[challenge_id] = {
        "username": payload.username,
        "code": code,
        "expires_at": expires_at,
        "attempts": 0
    }

    sent_ok, sent_message = send_email_code(otp_email, code)
    if not sent_ok:
        del ADMIN_LOGIN_CHALLENGES[challenge_id]
        return {
            "ok": False,
            "message": sent_message,
            "debug": smtp_config_debug()
        }

    return {
        "ok": True,
        "message": "Verification code sent",
        "challenge_id": challenge_id,
        "email_hint": otp_email,
        "otp_required": True
    }


@app.post("/admin/login/verify")
def admin_login_verify(payload: AdminLoginVerifyRequest):
    cleanup_expired_login_challenges()
    cleanup_expired_admin_sessions()

    item = ADMIN_LOGIN_CHALLENGES.get(payload.challenge_id)
    if not item:
        return {
            "ok": False,
            "message": "Invalid or expired challenge"
        }

    if item["attempts"] >= ADMIN_OTP_MAX_ATTEMPTS:
        del ADMIN_LOGIN_CHALLENGES[payload.challenge_id]
        return {
            "ok": False,
            "message": "Too many invalid code attempts"
        }

    if utc_now() > item["expires_at"]:
        del ADMIN_LOGIN_CHALLENGES[payload.challenge_id]
        return {
            "ok": False,
            "message": "Verification code expired"
        }

    if payload.code.strip() != item["code"]:
        item["attempts"] += 1
        return {
            "ok": False,
            "message": "Invalid verification code"
        }

    username = item["username"]
    token = create_admin_session_token()
    ADMIN_SESSIONS[token] = {
        "username": username,
        "created_at": utc_now(),
        "last_seen_at": utc_now()
    }

    del ADMIN_LOGIN_CHALLENGES[payload.challenge_id]

    return {
        "ok": True,
        "token": token,
        "username": username
    }


@app.get("/admin/me")
def admin_me(x_admin_token: Optional[str] = Header(None)):
    require_admin_token(x_admin_token)
    sess = ADMIN_SESSIONS.get(x_admin_token, {})
    return {
        "ok": True,
        "username": sess.get("username", "admin"),
        "created_at": sess.get("created_at", utc_now()).strftime("%Y-%m-%d %H:%M:%S"),
        "last_seen_at": sess.get("last_seen_at", utc_now()).strftime("%Y-%m-%d %H:%M:%S")
    }


@app.post("/admin/logout")
def admin_logout(x_admin_token: Optional[str] = Header(None)):
    cleanup_expired_admin_sessions()

    if x_admin_token and x_admin_token in ADMIN_SESSIONS:
        del ADMIN_SESSIONS[x_admin_token]

    return {"ok": True}


# =============================
# Admin API
# =============================
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
    WHERE last_seen_at >= %s
    """, (((utc_now() - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")),))
    online_clients = int(cur.fetchone()["cnt"])

    cur.execute("SELECT COALESCE(SUM(balance),0) AS total_balance FROM activations")
    total_balance = float(cur.fetchone()["total_balance"] or 0)

    cur.execute("SELECT COALESCE(SUM(equity),0) AS total_equity FROM activations")
    total_equity = float(cur.fetchone()["total_equity"] or 0)

    conn.close()

    return {
        "ok": True,
        "total_licenses": total_licenses,
        "active_licenses": active_licenses,
        "total_activations": total_activations,
        "online_clients": online_clients,
        "total_balance": total_balance,
        "total_equity": total_equity,
        "server_time": utc_now_ts(),
        "server_now_utc_ts": utc_now_ts()
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
        a.equity,
        a.open_positions_count,
        a.floating_pnl,
        a.last_seen_at
    FROM activations a
    JOIN licenses l ON l.id = a.license_id
    WHERE a.last_seen_at >= %s
    ORDER BY a.last_seen_at DESC
    LIMIT 5
    """, (((utc_now() - timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S")),))
    rows = cur.fetchall()
    conn.close()

    lines = []
    for row in rows:
        age = seconds_ago_from_str(row["last_seen_at"])
        label = row["name"] if (row["name"] or "").strip() else row["license_key"]
        bal = float(row["balance"] or 0)
        eq = float(row["equity"] or 0)
        if age >= 0:
            lines.append(f'{label} | {row["account_login"]} | bal {bal:.2f} | eq {eq:.2f} | {age}s ago')
        else:
            lines.append(f'{label} | {row["account_login"]} | bal {bal:.2f} | eq {eq:.2f}')

    while len(lines) < 5:
        lines.append("-")

    return {"ok": True, "lines": lines, "server_now_utc_ts": utc_now_ts()}


@app.get("/admin/licenses")
def admin_list_licenses(q: str = "", x_admin_token: Optional[str] = Header(None)):
    require_admin_token(x_admin_token)

    q = (q or "").strip()
    like_q = f"%{q}%"

    conn = get_conn()
    cur = conn.cursor()

    base_query = """
    SELECT
        l.id,
        l.license_key,
        l.name,
        l.status,
        l.expires_at,
        l.max_accounts,
        l.note,
        l.created_at,
        l.locked_account_login,
        l.locked_broker_server,
        l.locked_at,
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
            SELECT a.broker_server
            FROM activations a
            WHERE a.license_id = l.id
            ORDER BY a.last_seen_at DESC
            LIMIT 1
        ) AS latest_broker_server,
        (
            SELECT a.balance
            FROM activations a
            WHERE a.license_id = l.id
            ORDER BY a.last_seen_at DESC
            LIMIT 1
        ) AS latest_balance,
        (
            SELECT a.equity
            FROM activations a
            WHERE a.license_id = l.id
            ORDER BY a.last_seen_at DESC
            LIMIT 1
        ) AS latest_equity,
        (
            SELECT a.open_positions_count
            FROM activations a
            WHERE a.license_id = l.id
            ORDER BY a.last_seen_at DESC
            LIMIT 1
        ) AS latest_open_positions_count,
        (
            SELECT a.floating_pnl
            FROM activations a
            WHERE a.license_id = l.id
            ORDER BY a.last_seen_at DESC
            LIMIT 1
        ) AS latest_floating_pnl
    FROM licenses l
    """

    if q:
        cur.execute(base_query + """
        WHERE
            l.license_key ILIKE %s
            OR l.name ILIKE %s
            OR l.note ILIKE %s
            OR EXISTS (
                SELECT 1
                FROM activations a2
                WHERE a2.license_id = l.id
                  AND a2.account_login ILIKE %s
            )
        ORDER BY l.id DESC
        """, (like_q, like_q, like_q, like_q))
    else:
        cur.execute(base_query + " ORDER BY l.id DESC")

    rows = cur.fetchall()
    conn.close()

    items = []
    for row in rows:
        item = dict(row)
        item["effective_status"] = effective_license_status(item["status"], item["expires_at"])
        item["client_status"] = online_status_from_last_seen(item["last_seen_at"])
        item["time_left_text"] = format_time_left_human(item["expires_at"])
        item["time_left_seconds"] = time_left_seconds(item["expires_at"])
        item["expires_at_ts"] = dt_str_to_unix(item["expires_at"])
        items.append(item)

    return {"ok": True, "licenses": items, "server_now_utc_ts": utc_now_ts()}


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
        equity,
        open_positions_count,
        floating_pnl,
        created_at,
        last_seen_at
    FROM activations
    WHERE license_id = %s
    ORDER BY last_seen_at DESC
    """, (lic["id"],))
    activations = [dict(r) for r in cur.fetchall()]
    conn.close()

    return {
        "ok": True,
        "license": license_public_payload(lic),
        "activations": activations,
        "server_now_utc_ts": utc_now_ts()
    }


@app.post("/admin/create-license")
def admin_create_license(payload: AdminCreateLicenseRequest, x_admin_token: Optional[str] = Header(None)):
    require_admin_token(x_admin_token)

    license_key = (payload.license_key or "").strip().upper()
    if not license_key:
        license_key = secrets.token_hex(8).upper()

    existing = get_license_by_key(license_key)
    if existing:
        return {"ok": False, "message": "License key already exists"}

    normalized_expires_at = normalize_utc_db_str(payload.expires_at)
    if payload.expires_at and normalized_expires_at is None:
        return {"ok": False, "message": "Invalid expires_at format"}

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO licenses (license_key, name, status, expires_at, max_accounts, note, created_at)
    VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (
        license_key,
        payload.name or "",
        "active",
        normalized_expires_at,
        max(1, int(payload.max_accounts)),
        payload.note or "",
        utc_now_str()
    ))
    conn.commit()
    conn.close()

    write_log(
        event_type="admin_create_license",
        license_key=license_key,
        status="ok",
        message="License created",
        actor="admin"
    )

    return {
        "ok": True,
        "license_key": license_key,
        "name": payload.name or "",
        "status": "active",
        "expires_at": normalized_expires_at,
        "expires_at_ts": dt_str_to_unix(normalized_expires_at),
        "max_accounts": max(1, int(payload.max_accounts)),
        "note": payload.note or "",
        "server_now_utc_ts": utc_now_ts()
    }


@app.post("/admin/license/{license_key}/update")
def admin_update_license(license_key: str, payload: AdminUpdateLicenseRequest, x_admin_token: Optional[str] = Header(None)):
    require_admin_token(x_admin_token)

    lic = get_license_by_key(license_key)
    if not lic:
        return {"ok": False, "message": "License not found"}

    new_license_key = (payload.new_license_key if payload.new_license_key is not None else lic["license_key"]).strip().upper()
    name = payload.name if payload.name is not None else lic["name"]
    status = payload.status if payload.status is not None else lic["status"]
    expires_at_raw = payload.expires_at if payload.expires_at is not None else lic["expires_at"]
    expires_at = normalize_utc_db_str(expires_at_raw)
    max_accounts = payload.max_accounts if payload.max_accounts is not None else lic["max_accounts"]
    note = payload.note if payload.note is not None else lic["note"]

    if not new_license_key:
        return {"ok": False, "message": "License key cannot be empty"}

    if status not in ["active", "inactive"]:
        return {"ok": False, "message": "Invalid status"}

    if expires_at_raw is not None and expires_at is None:
        return {"ok": False, "message": "Invalid expires_at format"}

    max_accounts = max(1, int(max_accounts))

    if new_license_key != lic["license_key"]:
        existing = get_license_by_key(new_license_key)
        if existing:
            return {"ok": False, "message": "New license key already exists"}

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    UPDATE licenses
    SET license_key = %s, name = %s, status = %s, expires_at = %s, max_accounts = %s, note = %s
    WHERE id = %s
    """, (new_license_key, name or "", status, expires_at, max_accounts, note or "", lic["id"]))
    conn.commit()
    conn.close()

    write_log(
        event_type="admin_update_license",
        license_key=new_license_key,
        status="ok",
        message="License updated",
        actor="admin"
    )

    return {
        "ok": True,
        "message": "License updated",
        "license_key": new_license_key,
        "name": name or "",
        "status": status,
        "expires_at": expires_at,
        "expires_at_ts": dt_str_to_unix(expires_at),
        "max_accounts": max_accounts,
        "note": note or "",
        "server_now_utc_ts": utc_now_ts()
    }


@app.post("/admin/license/{license_key}/extend")
def admin_extend_license(license_key: str, payload: AdminExtendLicenseRequest, x_admin_token: Optional[str] = Header(None)):
    require_admin_token(x_admin_token)

    lic = get_license_by_key(license_key)
    if not lic:
        return {"ok": False, "message": "License not found"}

    days = int(payload.days)
    if days <= 0:
        return {"ok": False, "message": "Days must be positive"}

    new_expires_at = add_days_to_dt_str(lic["expires_at"], days)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    UPDATE licenses
    SET expires_at = %s
    WHERE id = %s
    """, (new_expires_at, lic["id"]))
    conn.commit()
    conn.close()

    write_log(
        event_type="admin_extend_license",
        license_key=license_key,
        status="ok",
        message=f"Extended by {days} days",
        actor="admin"
    )

    return {
        "ok": True,
        "message": f"License extended by {days} days",
        "license_key": license_key,
        "expires_at": new_expires_at,
        "expires_at_ts": dt_str_to_unix(new_expires_at),
        "server_now_utc_ts": utc_now_ts()
    }


@app.post("/admin/license/{license_key}/reset-lock")
def admin_reset_lock(license_key: str, x_admin_token: Optional[str] = Header(None)):
    require_admin_token(x_admin_token)

    lic = get_license_by_key(license_key)
    if not lic:
        return {"ok": False, "message": "License not found"}

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    UPDATE licenses
    SET locked_account_login = NULL,
        locked_broker_server = NULL,
        locked_at = NULL
    WHERE id = %s
    """, (lic["id"],))
    conn.commit()
    conn.close()

    write_log(
        event_type="admin_reset_lock",
        license_key=license_key,
        status="ok",
        message="License lock reset",
        actor="admin"
    )

    return {
        "ok": True,
        "message": "License lock reset",
        "license_key": license_key,
        "server_now_utc_ts": utc_now_ts()
    }


@app.post("/admin/license/{license_key}/reset-activations")
def admin_reset_activations(license_key: str, x_admin_token: Optional[str] = Header(None)):
    require_admin_token(x_admin_token)

    lic = get_license_by_key(license_key)
    if not lic:
        return {"ok": False, "message": "License not found"}

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM activations WHERE license_id = %s", (lic["id"],))
    deleted = cur.rowcount
    conn.commit()
    conn.close()

    write_log(
        event_type="admin_reset_activations",
        license_key=license_key,
        status="ok",
        message=f"Activations reset: {deleted}",
        actor="admin"
    )

    return {"ok": True, "message": "Activations reset", "deleted": deleted, "server_now_utc_ts": utc_now_ts()}


@app.delete("/admin/license/{license_key}")
def admin_delete_license(license_key: str, x_admin_token: Optional[str] = Header(None)):
    require_admin_token(x_admin_token)

    lic = get_license_by_key(license_key)
    if not lic:
        return {"ok": False, "message": "License not found"}

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM activations WHERE license_id = %s", (lic["id"],))
    cur.execute("DELETE FROM licenses WHERE id = %s", (lic["id"],))
    conn.commit()
    conn.close()

    write_log(
        event_type="admin_delete_license",
        license_key=license_key,
        status="ok",
        message="License deleted",
        actor="admin"
    )

    return {"ok": True, "message": "License deleted", "license_key": license_key, "server_now_utc_ts": utc_now_ts()}


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
        a.equity,
        a.open_positions_count,
        a.floating_pnl,
        a.created_at,
        a.last_seen_at
    FROM activations a
    JOIN licenses l ON l.id = a.license_id
    ORDER BY a.last_seen_at DESC
    """)
    rows = cur.fetchall()
    conn.close()

    items = []
    for row in rows:
        item = dict(row)
        item["client_status"] = online_status_from_last_seen(item["last_seen_at"])
        items.append(item)

    return {"ok": True, "activations": items, "server_now_utc_ts": utc_now_ts()}


@app.get("/admin/online-clients")
def admin_online_clients(x_admin_token: Optional[str] = Header(None)):
    require_admin_token(x_admin_token)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT
        l.license_key,
        l.name,
        l.note,
        l.status,
        l.expires_at,
        l.locked_account_login,
        l.locked_broker_server,
        l.locked_at,
        a.account_login,
        a.broker_server,
        a.machine_id,
        a.balance,
        a.equity,
        a.last_seen_at
    FROM activations a
    JOIN licenses l ON l.id = a.license_id
    WHERE a.last_seen_at >= %s
    ORDER BY a.last_seen_at DESC
    """, (((utc_now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")),))
    rows = cur.fetchall()
    conn.close()

    items = []
    for row in rows:
        item = dict(row)
        item["age_sec"] = seconds_ago_from_str(item["last_seen_at"])
        item["effective_status"] = effective_license_status(item["status"], item["expires_at"])
        item["client_status"] = online_status_from_last_seen(item["last_seen_at"])
        item["time_left_text"] = format_time_left_human(item["expires_at"])
        item["time_left_seconds"] = time_left_seconds(item["expires_at"])
        item["expires_at_ts"] = dt_str_to_unix(item["expires_at"])
        items.append(item)

    return {"ok": True, "clients": items, "server_now_utc_ts": utc_now_ts()}


@app.get("/admin/logs")
def admin_logs(limit: int = 15, offset: int = 0, x_admin_token: Optional[str] = Header(None)):
    require_admin_token(x_admin_token)

    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) AS cnt FROM license_logs")
    total = int(cur.fetchone()["cnt"])

    cur.execute("""
    SELECT
        id,
        created_at,
        event_type,
        license_key,
        account_login,
        broker_server,
        machine_id,
        status,
        message,
        actor
    FROM license_logs
    ORDER BY id DESC
    LIMIT %s OFFSET %s
    """, (limit, offset))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    return {
        "ok": True,
        "logs": rows,
        "total": total,
        "limit": limit,
        "offset": offset,
        "server_now_utc_ts": utc_now_ts()
    }

@app.get("/admin/errors")
def admin_errors(limit: int = 15, offset: int = 0, x_admin_token: Optional[str] = Header(None)):
    require_admin_token(x_admin_token)

    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) AS cnt FROM slave_errors")
    total = int(cur.fetchone()["cnt"])

    cur.execute("""
    SELECT
        id,
        created_at,
        license_key,
        account_login,
        broker_server,
        machine_id,
        category,
        severity,
        symbol,
        code,
        message,
        details,
        snapshot_id
    FROM slave_errors
    ORDER BY id DESC
    LIMIT %s OFFSET %s
    """, (limit, offset))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    return {
        "ok": True,
        "errors": rows,
        "total": total,
        "limit": limit,
        "offset": offset,
        "server_now_utc_ts": utc_now_ts()
    }


@app.post("/admin/clear-errors")
def admin_clear_errors(x_admin_token: Optional[str] = Header(None)):
    require_admin_token(x_admin_token)

    deleted = clear_all_errors()

    return {
        "ok": True,
        "message": "All errors cleared",
        "deleted": deleted,
        "server_now_utc_ts": utc_now_ts()
    }


@app.post("/admin/clear-logs")
def admin_clear_logs(x_admin_token: Optional[str] = Header(None)):
    require_admin_token(x_admin_token)

    deleted = clear_all_logs()

    return {
        "ok": True,
        "message": "All logs cleared",
        "deleted": deleted,
        "server_now_utc_ts": utc_now_ts()
    }

# =============================
# Admin HTML
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
        button.purple { background:#7c3aed; }
        table { width:100%; border-collapse:collapse; margin-top:10px; }
        th, td { border-bottom:1px solid #ddd; padding:10px; text-align:left; vertical-align:top; font-size:13px; }
        th.sortable { cursor:pointer; user-select:none; white-space:nowrap; }
        th.sortable:hover { background:#f3f4f6; }
        .row { display:grid; grid-template-columns:1fr 1fr 1fr; gap:12px; }
        .small { color:#555; font-size:13px; }
        .hidden { display:none; }
        pre { background:#111827; color:#e5e7eb; padding:12px; border-radius:8px; overflow:auto; }
        .topbar { display:flex; justify-content:space-between; align-items:center; margin-bottom:16px; gap:12px; flex-wrap:wrap; }
        .badge { display:inline-block; padding:4px 8px; border-radius:999px; font-size:12px; color:white; }
        .status-active { background:#16a34a; }
        .status-inactive { background:#dc2626; }
        .status-expired { background:#b91c1c; }
        .client-online { background:#16a34a; }
        .client-stale { background:#f59e0b; }
        .client-offline { background:#6b7280; }
        .toolbar { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
        .actions button { margin-right:6px; margin-bottom:6px; }
        .mono { font-family: Consolas, monospace; }
        .nowrap { white-space:nowrap; }
        #editModalWrap {
            position:fixed; inset:0; background:rgba(0,0,0,0.45);
            display:none; align-items:center; justify-content:center; padding:20px; z-index:9999;
        }
        #editModal {
            width:min(980px, 96vw); max-height:92vh; overflow:auto;
            background:white; border-radius:12px; padding:18px;
            box-shadow:0 10px 40px rgba(0,0,0,0.25);
        }
        .table-wrap { overflow:auto; }
        .tiny { font-size:11px; color:#666; }
        .metric-grid { display:grid; grid-template-columns:repeat(6,1fr); gap:12px; }
        .metric { background:#f9fafb; border:1px solid #e5e7eb; border-radius:10px; padding:12px; }
        .metric .label { font-size:12px; color:#666; }
        .metric .value { font-size:18px; font-weight:bold; margin-top:4px; }

        .login-msg {
            margin-top: 12px;
            padding: 12px;
            border-radius: 8px;
            font-size: 14px;
            display: none;
        }

        .login-msg.success {
            background: #dcfce7;
            color: #166534;
            border: 1px solid #16a34a;
        }

        .login-msg.error {
            background: #fee2e2;
            color: #991b1b;
            border: 1px solid #dc2626;
        }

        @media (max-width: 1100px) {
            .metric-grid { grid-template-columns:repeat(2,1fr); }
            .row { grid-template-columns:1fr; }
        }
    </style>
</head>
<body>
    <div id="loginView" class="card" style="max-width:520px;margin:80px auto;">
        <h2>Admin Login</h2>

        <div id="step1">
            <div>
                <label>Username</label>
                <input id="adminUsername" type="text" placeholder="Username">
            </div>
            <div>
                <label>Password</label>
                <input id="adminPassword" type="password" placeholder="Password">
            </div>
            <button onclick="startLogin()">Send verification code</button>
        </div>

        <div id="step2" class="hidden">
            <div class="small" id="otpInfo"></div>
            <div>
                <label>Verification Code</label>
                <input id="adminOtpCode" type="text" placeholder="6-digit code">
            </div>
            <button onclick="verifyLogin()">Verify and login</button>
            <button class="gray" onclick="backToStep1()">Back</button>
        </div>

        <div id="loginResult" class="login-msg"></div>
    </div>

    <div id="appView" class="hidden">
        <div class="topbar">
            <div>
                <h1>MT5 Copier Admin Panel</h1>
                <div class="small" id="welcomeBar"></div>
                <div class="tiny">Auto refresh every 15 seconds while this page stays open</div>
            </div>
            <div class="toolbar">
                <button class="gray" onclick="loadAll()">Refresh now</button>
                <button class="red" onclick="logoutAdmin()">Logout</button>
            </div>
        </div>

        <div class="card">
            <h3>Dashboard</h3>
            <div id="dashboardStats"></div>
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
                    <input id="createExpiresAt" type="datetime-local">
                    <div class="tiny">Shown and entered in your local timezone</div>
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
                <div>
                    <label>Sort By</label>
                    <select id="sortField" onchange="loadLicenses()">
                        <option value="name">Name</option>
                        <option value="license_key">License Key</option>
                        <option value="effective_status">License Status</option>
                        <option value="client_status">Client Status</option>
                        <option value="time_left_seconds">Time Left</option>
                        <option value="expires_at">Expires At</option>
                        <option value="latest_account_login">Account Login</option>
                        <option value="latest_balance">Balance</option>
                        <option value="latest_equity">Equity</option>
                        <option value="last_seen_at">Last Seen</option>
                    </select>
                </div>
                <div>
                    <label>Sort Direction</label>
                    <select id="sortDir" onchange="loadLicenses()">
                        <option value="asc">Ascending</option>
                        <option value="desc" selected>Descending</option>
                    </select>
                </div>
            </div>
            <div class="toolbar">
                <button onclick="loadLicenses()">Search</button>
                <button class="gray" onclick="clearSearch()">Clear</button>
            </div>
            <div class="table-wrap">
                <div id="licensesTable"></div>
            </div>
        </div>

        <div class="card">
            <h3>Online / Recent Clients</h3>
            <div class="table-wrap">
                <div id="onlineClientsTable"></div>
            </div>
        </div>

        <div class="card">
            <div class="topbar">
                <h3>Slave Errors</h3>
                <div class="toolbar">
                    <button class="red" onclick="clearErrors()">Clear ALL errors</button>
                </div>
            </div>
            <div class="table-wrap">
                <div id="errorsTable"></div>
            </div>
        </div>

        <div class="card">
            <h3>Activations</h3>
            <div class="table-wrap">
                <div id="activationsTable"></div>
            </div>
        </div>

        <div class="card">
            <div class="topbar">
                <h3>Logs</h3>
                <div class="toolbar">
                    <button class="red" onclick="clearLogs()">Clear ALL logs</button>
                </div>
            </div>
            <div class="table-wrap">
                <div id="logsTable"></div>
            </div>
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
                    <input id="editExpiresAt" type="datetime-local">
                    <div class="tiny">Shown and entered in your local timezone</div>
                </div>
                <div>
                    <label>Max Accounts</label>
                    <input id="editMaxAccounts">
                </div>
                <div>
                    <label>Time Left</label>
                    <input id="editTimeLeft" disabled>
                </div>
            </div>

            <div class="row">
                <div>
                    <label>Locked Account</label>
                    <input id="editLockedAccount" disabled>
                </div>
                <div>
                    <label>Locked Broker</label>
                    <input id="editLockedBroker" disabled>
                </div>
                <div>
                    <label>Locked At</label>
                    <input id="editLockedAt" disabled>
                </div>
            </div>

            <div>
                <label>Note</label>
                <textarea id="editNote"></textarea>
            </div>

            <div class="toolbar" style="margin-top:12px;">
                <button class="green" onclick="saveLicenseEdit()">Save Changes</button>
                <button class="purple" onclick="copyCurrentLicense()">Copy License</button>
                <button onclick="extendCurrentLicense(7)">+7 days</button>
                <button onclick="extendCurrentLicense(30)">+30 days</button>
                <button onclick="extendCurrentLicense(90)">+90 days</button>
                <button class="orange" onclick="resetActivationsCurrent()">Reset Activations</button>
                <button class="gray" onclick="resetCurrentLock()">Reset Lock</button>
                <button class="red" onclick="deleteCurrentLicense()">Delete License</button>
            </div>

            <h3 style="margin-top:24px;">License Activations</h3>
            <div class="table-wrap">
                <div id="editActivations"></div>
            </div>

            <pre id="editResult"></pre>
        </div>
    </div>

<script>
let autoRefreshHandle = null;
let adminToken = "";
let loginChallengeId = "";
let logsOffset = 0;
const logsLimit = 15;
let errorsOffset = 0;
const errorsLimit = 15;

function getToken() {
    return adminToken || "";
}

function setToken(token) {
    adminToken = token || "";
}

function clearToken() {
    adminToken = "";
    logsOffset = 0;
    errorsOffset = 0;
}

function showLoginMessage(text, type = "success") {
    const el = document.getElementById("loginResult");
    el.textContent = text || "";
    el.className = "login-msg " + type;
    el.style.display = "block";
}

function pad2(n) {
    return String(n).padStart(2, "0");
}

function utcToLocalInputValue(utcString) {
    if (!utcString) return "";
    const d = new Date(utcString.replace(" ", "T") + "Z");
    if (isNaN(d.getTime())) return "";

    const year = d.getFullYear();
    const month = pad2(d.getMonth() + 1);
    const day = pad2(d.getDate());
    const hours = pad2(d.getHours());
    const mins = pad2(d.getMinutes());

    return `${year}-${month}-${day}T${hours}:${mins}`;
}

function localInputValueToUtcString(localValue) {
    if (!localValue) return "";

    const d = new Date(localValue);
    if (isNaN(d.getTime())) return "";

    const year = d.getUTCFullYear();
    const month = pad2(d.getUTCMonth() + 1);
    const day = pad2(d.getUTCDate());
    const hours = pad2(d.getUTCHours());
    const mins = pad2(d.getUTCMinutes());
    const secs = pad2(d.getUTCSeconds());

    return `${year}-${month}-${day} ${hours}:${mins}:${secs}`;
}

function utcToLocalDisplay(value) {
    if (value === null || value === undefined || value === "") return "";

    let d = null;

    if (typeof value === "number") {
        d = new Date(value * 1000);
    } else if (/^\\d+$/.test(String(value))) {
        d = new Date(Number(value) * 1000);
    } else {
        d = new Date(String(value).replace(" ", "T") + "Z");
    }

    if (isNaN(d.getTime())) return String(value);
    return d.toLocaleString();
}

async function apiGet(url) {
    const token = getToken();
    const res = await fetch(url, {
        headers: token ? { "x-admin-token": token } : {}
    });
    return await res.json();
}

async function apiPost(url, data = {}) {
    const token = getToken();
    const headers = { "Content-Type": "application/json" };
    if (token) headers["x-admin-token"] = token;

    const res = await fetch(url, {
        method: "POST",
        headers,
        body: JSON.stringify(data)
    });

    const json = await res.json();
    console.log("apiPost", url, json);
    return json;
}

async function apiDelete(url) {
    const token = getToken();
    const headers = {};
    if (token) headers["x-admin-token"] = token;

    const res = await fetch(url, {
        method: "DELETE",
        headers
    });
    return await res.json();
}

function showLoginOnly() {
    document.getElementById("loginView").classList.remove("hidden");
    document.getElementById("appView").classList.add("hidden");
    stopAutoRefresh();
    clearToken();
    loginChallengeId = "";
    document.getElementById("step1").classList.remove("hidden");
    document.getElementById("step2").classList.add("hidden");
}

function showAppOnly() {
    document.getElementById("loginView").classList.add("hidden");
    document.getElementById("appView").classList.remove("hidden");
    startAutoRefresh();
}

function startAutoRefresh() {
    stopAutoRefresh();
    autoRefreshHandle = setInterval(() => {
        if (!document.getElementById("appView").classList.contains("hidden")) {
            loadAll();
        }
    }, 15000);
}

function stopAutoRefresh() {
    if (autoRefreshHandle) {
        clearInterval(autoRefreshHandle);
        autoRefreshHandle = null;
    }
}

function backToStep1() {
    loginChallengeId = "";
    document.getElementById("step1").classList.remove("hidden");
    document.getElementById("step2").classList.add("hidden");
    showLoginMessage("", "success");
    document.getElementById("loginResult").style.display = "none";
}

async function startLogin() {
    const username = document.getElementById("adminUsername").value.trim();
    const password = document.getElementById("adminPassword").value.trim();

    const result = await apiPost("/admin/login/start", {
        username: username,
        password: password
    });

    if (result.ok) {
        if (result.token) {
            setToken(result.token);

            const welcomeBar = document.getElementById("welcomeBar");
            if (welcomeBar) {
                welcomeBar.textContent = "Logged in as " + (result.username || "admin");
            }

            showLoginMessage(result.message || "Login successful.", "success");
            showAppOnly();
            await loadAll();
            return;
        }

        loginChallengeId = result.challenge_id;

        document.getElementById("otpInfo").textContent =
            "Verification code sent to: " + (result.email_hint || "your email");

        document.getElementById("step1").classList.add("hidden");
        document.getElementById("step2").classList.remove("hidden");

        showLoginMessage("Verification code sent successfully.", "success");
    } else {
        showLoginMessage(result.message || "Invalid username or password.", "error");
    }
}

async function verifyLogin() {
    const code = document.getElementById("adminOtpCode").value.trim();

    try {
        const result = await apiPost("/admin/login/verify", {
            challenge_id: loginChallengeId,
            code: code
        });

        console.log("verifyLogin result:", result);

        if (result.ok && result.token) {
            setToken(result.token);

            const welcomeBar = document.getElementById("welcomeBar");
            if (welcomeBar) {
                welcomeBar.textContent = "Logged in as " + (result.username || "admin");
            }

            showLoginMessage("Login successful.", "success");
            showAppOnly();

            try {
                await loadAll();
            } catch (e) {
                console.error("loadAll error:", e);
                showLoginMessage("Login succeeded, but dashboard failed to load.", "error");
            }
        } else {
            showLoginMessage(result.message || "Invalid verification code.", "error");
        }
    } catch (e) {
        console.error("verifyLogin error:", e);
        showLoginMessage("Unexpected error during verification.", "error");
    }
}

async function logoutAdmin() {
    await apiPost("/admin/logout", {});
    showLoginOnly();
}

function licenseStatusBadge(status) {
    let cls = "status-inactive";
    if (status === "active") cls = "status-active";
    else if (status === "expired") cls = "status-expired";
    return `<span class="badge ${cls}">${escapeHtml(status)}</span>`;
}

function clientStatusBadge(status) {
    let cls = "client-offline";
    if (status === "online") cls = "client-online";
    else if (status === "stale") cls = "client-stale";
    return `<span class="badge ${cls}">${escapeHtml(status)}</span>`;
}

function safeNum(v, digits=2) {
    if (v === null || v === undefined || v === "") return "";
    const n = Number(v);
    if (Number.isNaN(n)) return "";
    return n.toFixed(digits);
}

function normalizeForSort(v) {
    if (v === null || v === undefined) return null;
    return v;
}

function sortItems(items, field, dir) {
    const mul = dir === "asc" ? 1 : -1;
    return [...items].sort((a, b) => {
        let av = normalizeForSort(a[field]);
        let bv = normalizeForSort(b[field]);

        if (field === "expires_at" || field === "last_seen_at") {
            av = av || "";
            bv = bv || "";
        }

        if (field === "latest_balance" || field === "latest_equity" || field === "time_left_seconds") {
            av = (av === null || av === undefined) ? -999999999 : Number(av);
            bv = (bv === null || bv === undefined) ? -999999999 : Number(bv);
        }

        if (field === "effective_status" || field === "client_status" || field === "name" || field === "license_key" || field === "latest_account_login") {
            av = String(av || "").toLowerCase();
            bv = String(bv || "").toLowerCase();
        }

        if (av < bv) return -1 * mul;
        if (av > bv) return 1 * mul;
        return 0;
    });
}

async function loadDashboard() {
    const result = await apiGet("/admin/dashboard");
    if (!result.ok) {
        if (result.detail) showLoginOnly();
        document.getElementById("dashboardStats").innerHTML = "<pre>" + JSON.stringify(result, null, 2) + "</pre>";
        return;
    }

    document.getElementById("dashboardStats").innerHTML = `
        <div class="metric-grid">
            <div class="metric"><div class="label">Total Licenses</div><div class="value">${result.total_licenses}</div></div>
            <div class="metric"><div class="label">Active Licenses</div><div class="value">${result.active_licenses}</div></div>
            <div class="metric"><div class="label">Total Activations</div><div class="value">${result.total_activations}</div></div>
            <div class="metric"><div class="label">Online Clients</div><div class="value">${result.online_clients}</div></div>
            <div class="metric"><div class="label">Total Balance</div><div class="value">${safeNum(result.total_balance)}</div></div>
            <div class="metric"><div class="label">Total Equity</div><div class="value">${safeNum(result.total_equity)}</div></div>
        </div>
        <div class="small" style="margin-top:10px;">Server time: <b>${utcToLocalDisplay(result.server_now_utc_ts || result.server_time)}</b></div>
    `;
}

async function createLicense() {
    const license_key = document.getElementById("createLicenseKey").value.trim();
    const name = document.getElementById("createName").value.trim();
    const expires_at = localInputValueToUtcString(
        document.getElementById("createExpiresAt").value.trim()
    );
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

async function loadLicenses() {
    const q = document.getElementById("searchInput").value.trim();
    const sortField = document.getElementById("sortField").value;
    const sortDir = document.getElementById("sortDir").value;

    const result = await apiGet("/admin/licenses?q=" + encodeURIComponent(q));

    if (!result.ok) {
        if (result.detail) showLoginOnly();
        document.getElementById("licensesTable").innerHTML = "<pre>" + JSON.stringify(result, null, 2) + "</pre>";
        return;
    }

    const items = sortItems(result.licenses, sortField, sortDir);

    let html = `
    <table>
      <tr>
        <th class="sortable" onclick="setSort('name')">Name</th>
        <th class="sortable" onclick="setSort('license_key')">License Key</th>
        <th class="sortable" onclick="setSort('effective_status')">License Status</th>
        <th class="sortable" onclick="setSort('client_status')">Client Status</th>
        <th class="sortable" onclick="setSort('expires_at')">Expires At</th>
        <th class="sortable" onclick="setSort('time_left_seconds')">Time Left</th>
        <th>Max</th>
        <th class="sortable" onclick="setSort('latest_account_login')">Account Login</th>
        <th>Broker</th>
        <th class="sortable" onclick="setSort('latest_balance')">Balance</th>
        <th class="sortable" onclick="setSort('latest_equity')">Equity</th>
        <th>Open Pos</th>
        <th>Floating P/L</th>
        <th class="sortable" onclick="setSort('last_seen_at')">Last Seen</th>
        <th>Locked To</th>
        <th>Notes</th>
        <th>Actions</th>
      </tr>
    `;

    for (const lic of items) {
        html += `
        <tr>
            <td>${escapeHtml(lic.name || "")}</td>
            <td class="mono">${escapeHtml(lic.license_key)}</td>
            <td>${licenseStatusBadge(lic.effective_status)}</td>
            <td>${clientStatusBadge(lic.client_status)}</td>
            <td class="nowrap">${escapeHtml(utcToLocalDisplay(lic.expires_at_ts || lic.expires_at || ""))}</td>
            <td class="nowrap">${escapeHtml(lic.time_left_text || "-")}</td>
            <td>${lic.max_accounts}</td>
            <td>${escapeHtml(lic.latest_account_login || "")}</td>
            <td>${escapeHtml(lic.latest_broker_server || "")}</td>
            <td>${safeNum(lic.latest_balance)}</td>
            <td>${safeNum(lic.latest_equity)}</td>
            <td>${escapeHtml(String(lic.latest_open_positions_count ?? 0))}</td>
            <td>${safeNum(lic.latest_floating_pnl)}</td>
            <td class="nowrap">${escapeHtml(utcToLocalDisplay(lic.last_seen_at || ""))}</td>
            <td>${escapeHtml((lic.locked_account_login || "") + ((lic.locked_broker_server || "") ? " @ " + lic.locked_broker_server : ""))}</td>
            <td>${escapeHtml(lic.note || "")}</td>
            <td class="actions">
                <button onclick="openEditModal('${jsq(lic.license_key)}')">Edit</button>
                <button class="purple" onclick="copyLicense('${jsq(lic.license_key)}')">Copy</button>
                <button class="green" onclick="quickStatus('${jsq(lic.license_key)}','active')">Activate</button>
                <button class="red" onclick="quickStatus('${jsq(lic.license_key)}','inactive')">Deactivate</button>
                <button onclick="quickExtend('${jsq(lic.license_key)}',7)">+7d</button>
                <button class="orange" onclick="quickExtend('${jsq(lic.license_key)}',30)">+30d</button>
            </td>
        </tr>
        `;
    }

    html += "</table>";
    document.getElementById("licensesTable").innerHTML = html;
}

function setSort(field) {
    const sortField = document.getElementById("sortField");
    const sortDir = document.getElementById("sortDir");

    if (sortField.value === field) {
        sortDir.value = sortDir.value === "asc" ? "desc" : "asc";
    } else {
        sortField.value = field;
        sortDir.value = "desc";
    }
    loadLicenses();
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
        if (result.detail) showLoginOnly();
        document.getElementById("onlineClientsTable").innerHTML = "<pre>" + JSON.stringify(result, null, 2) + "</pre>";
        return;
    }

    let html = `
    <table>
      <tr>
        <th>Name</th>
        <th>License</th>
        <th>License Status</th>
        <th>Client Status</th>
        <th>Account Login</th>
        <th>Broker</th>
        <th>Balance</th>
        <th>Equity</th>
        <th>Open Pos</th>
        <th>Floating P/L</th>
        <th>Expires</th>
        <th>Time Left</th>
        <th>Last Seen</th>
        <th>Locked To</th>
        <th>Notes</th>
      </tr>
    `;
    for (const row of result.clients) {
        html += `
        <tr>
          <td>${escapeHtml(row.name || "")}</td>
          <td class="mono">${escapeHtml(row.license_key)}</td>
          <td>${licenseStatusBadge(row.effective_status)}</td>
          <td>${clientStatusBadge(row.client_status)}</td>
          <td>${escapeHtml(row.account_login || "")}</td>
          <td>${escapeHtml(row.broker_server || "")}</td>
          <td>${safeNum(row.balance)}</td>
          <td>${safeNum(row.equity)}</td>
          <td class="nowrap">${escapeHtml(utcToLocalDisplay(row.expires_at_ts || row.expires_at || ""))}</td>
          <td class="nowrap">${escapeHtml(row.time_left_text || "-")}</td>
          <td class="nowrap">${escapeHtml(utcToLocalDisplay(row.last_seen_at || ""))}</td>
          <td>${escapeHtml((row.locked_account_login || "") + ((row.locked_broker_server || "") ? " @ " + row.locked_broker_server : ""))}</td>
          <td>${escapeHtml(row.note || "")}</td>
        </tr>
        `;
    }
    html += "</table>";
    document.getElementById("onlineClientsTable").innerHTML = html;
}

async function loadActivations() {
    const result = await apiGet("/admin/activations");

    if (!result.ok) {
        if (result.detail) showLoginOnly();
        document.getElementById("activationsTable").innerHTML = "<pre>" + JSON.stringify(result, null, 2) + "</pre>";
        return;
    }

    let html = `
    <table>
      <tr>
        <th>Name</th>
        <th>License</th>
        <th>Status</th>
        <th>Account</th>
        <th>Broker</th>
        <th>Machine</th>
        <th>Balance</th>
        <th>Equity</th>
        <th>Open Pos</th>
        <th>Floating P/L</th>
        <th>Created</th>
        <th>Last Seen</th>
      </tr>
    `;
    for (const row of result.activations) {
        html += `
        <tr>
            <td>${escapeHtml(row.name || "")}</td>
            <td class="mono">${escapeHtml(row.license_key)}</td>
            <td>${clientStatusBadge(row.client_status)}</td>
            <td>${escapeHtml(row.account_login)}</td>
            <td>${escapeHtml(row.broker_server)}</td>
            <td>${escapeHtml(row.machine_id)}</td>
            <td>${safeNum(row.balance)}</td>
            <td>${safeNum(row.equity)}</td>
            <td>${escapeHtml(String(row.open_positions_count ?? 0))}</td>
            <td>${safeNum(row.floating_pnl)}</td>
            <td class="nowrap">${escapeHtml(utcToLocalDisplay(row.created_at))}</td>
            <td class="nowrap">${escapeHtml(utcToLocalDisplay(row.last_seen_at))}</td>
        </tr>
        `;
    }
    html += "</table>";
    document.getElementById("activationsTable").innerHTML = html;
}

async function clearErrors() {
    if (!confirm("Delete ALL slave errors? This cannot be undone.")) return;

    const result = await apiPost("/admin/clear-errors", {});
    if (!result.ok) {
        alert(result.message || "Failed to clear errors");
        return;
    }

    alert((result.message || "All errors cleared") + " Deleted: " + String(result.deleted || 0));
    errorsOffset = 0;
    await loadErrors();
}

async function loadErrors() {
    const result = await apiGet(`/admin/errors?limit=${errorsLimit}&offset=${errorsOffset}`);

    if (!result.ok) {
        if (result.detail) showLoginOnly();
        document.getElementById("errorsTable").innerHTML = "<pre>" + JSON.stringify(result, null, 2) + "</pre>";
        return;
    }

    const total = Number(result.total || 0);
    const offset = Number(result.offset || 0);
    const limit = Number(result.limit || errorsLimit);

    let html = `
    <table>
      <tr>
        <th>Time</th>
        <th>License</th>
        <th>Account</th>
        <th>Broker</th>
        <th>Category</th>
        <th>Severity</th>
        <th>Symbol</th>
        <th>Code</th>
        <th>Message</th>
        <th>Details</th>
        <th>Snapshot</th>
      </tr>
    `;
    for (const row of result.errors) {
        html += `
        <tr>
            <td class="nowrap">${escapeHtml(utcToLocalDisplay(row.created_at || ""))}</td>
            <td class="mono">${escapeHtml(row.license_key || "")}</td>
            <td>${escapeHtml(row.account_login || "")}</td>
            <td>${escapeHtml(row.broker_server || "")}</td>
            <td>${escapeHtml(row.category || "")}</td>
            <td>${escapeHtml(row.severity || "")}</td>
            <td>${escapeHtml(row.symbol || "")}</td>
            <td>${escapeHtml(row.code || "")}</td>
            <td>${escapeHtml(row.message || "")}</td>
            <td>${escapeHtml(row.details || "")}</td>
            <td>${escapeHtml(row.snapshot_id || "")}</td>
        </tr>
        `;
    }
    html += "</table>";

    const currentPage = total === 0 ? 1 : Math.floor(offset / limit) + 1;
    const totalPages = Math.max(1, Math.ceil(total / limit));
    const prevDisabled = offset <= 0 ? "disabled" : "";
    const nextDisabled = offset + limit >= total ? "disabled" : "";

    html += `
    <div class="toolbar" style="margin-top:12px;">
        <button class="gray" onclick="prevErrorsPage()" ${prevDisabled}>Prev</button>
        <button class="gray" onclick="nextErrorsPage()" ${nextDisabled}>Next</button>
        <span class="small">Page ${currentPage} / ${totalPages} | Showing ${result.errors.length} of ${total}</span>
    </div>
    `;

    document.getElementById("errorsTable").innerHTML = html;
}

function prevErrorsPage() {
    errorsOffset = Math.max(0, errorsOffset - errorsLimit);
    loadErrors();
}

function nextErrorsPage() {
    errorsOffset += errorsLimit;
    loadErrors();
}


async function clearLogs() {
    if (!confirm("Delete ALL logs? This cannot be undone.")) return;

    const result = await apiPost("/admin/clear-logs", {});
    if (!result.ok) {
        alert(result.message || "Failed to clear logs");
        return;
    }

    alert((result.message || "All logs cleared") + " Deleted: " + String(result.deleted || 0));
    logsOffset = 0;
    await loadLogs();
}


async function loadLogs() {
    const result = await apiGet(`/admin/logs?limit=${logsLimit}&offset=${logsOffset}`);

    if (!result.ok) {
        if (result.detail) showLoginOnly();
        document.getElementById("logsTable").innerHTML = "<pre>" + JSON.stringify(result, null, 2) + "</pre>";
        return;
    }

    const total = Number(result.total || 0);
    const offset = Number(result.offset || 0);
    const limit = Number(result.limit || logsLimit);

    let html = `
    <table>
      <tr>
        <th>Time</th>
        <th>Event</th>
        <th>License</th>
        <th>Account</th>
        <th>Broker</th>
        <th>Machine</th>
        <th>Status</th>
        <th>Message</th>
        <th>Actor</th>
      </tr>
    `;
    for (const row of result.logs) {
        html += `
        <tr>
            <td class="nowrap">${escapeHtml(utcToLocalDisplay(row.created_at || ""))}</td>
            <td>${escapeHtml(row.event_type || "")}</td>
            <td class="mono">${escapeHtml(row.license_key || "")}</td>
            <td>${escapeHtml(row.account_login || "")}</td>
            <td>${escapeHtml(row.broker_server || "")}</td>
            <td>${escapeHtml(row.machine_id || "")}</td>
            <td>${escapeHtml(row.status || "")}</td>
            <td>${escapeHtml(row.message || "")}</td>
            <td>${escapeHtml(row.actor || "")}</td>
        </tr>
        `;
    }
    html += "</table>";

    const currentPage = total === 0 ? 1 : Math.floor(offset / limit) + 1;
    const totalPages = Math.max(1, Math.ceil(total / limit));
    const prevDisabled = offset <= 0 ? "disabled" : "";
    const nextDisabled = offset + limit >= total ? "disabled" : "";

    html += `
    <div class="toolbar" style="margin-top:12px;">
        <button class="gray" onclick="prevLogsPage()" ${prevDisabled}>Prev</button>
        <button class="gray" onclick="nextLogsPage()" ${nextDisabled}>Next</button>
        <span class="small">Page ${currentPage} / ${totalPages} | Showing ${result.logs.length} of ${total}</span>
    </div>
    `;

    document.getElementById("logsTable").innerHTML = html;
}

function prevLogsPage() {
    logsOffset = Math.max(0, logsOffset - logsLimit);
    loadLogs();
}

function nextLogsPage() {
    logsOffset += logsLimit;
    loadLogs();
}

async function loadAll() {
    await loadDashboard();
    await loadLicenses();
    await loadOnlineClients();
    await loadErrors();
    await loadActivations();
    await loadLogs();
}

async function openEditModal(licenseKey) {
    const result = await apiGet(`/admin/license/${encodeURIComponent(licenseKey)}`);
    if (!result.ok) {
        if (result.detail) showLoginOnly();
        alert(result.message || "Failed to load license");
        return;
    }

    const lic = result.license;
    document.getElementById("editOriginalKey").value = lic.license_key;
    document.getElementById("editLicenseKey").value = lic.license_key || "";
    document.getElementById("editName").value = lic.name || "";
    document.getElementById("editStatus").value = lic.status || "active";
    document.getElementById("editExpiresAt").value = utcToLocalInputValue(lic.expires_at || "");
    document.getElementById("editMaxAccounts").value = lic.max_accounts || 1;
    document.getElementById("editNote").value = lic.note || "";
    document.getElementById("editTimeLeft").value = lic.time_left_text || "-";
    document.getElementById("editLockedAccount").value = lic.locked_account_login || "";
    document.getElementById("editLockedBroker").value = lic.locked_broker_server || "";
    document.getElementById("editLockedAt").value = utcToLocalDisplay(lic.locked_at || "");
    document.getElementById("editResult").textContent = "";

    let html = "<table><tr><th>Account</th><th>Broker</th><th>Machine</th><th>Balance</th><th>Equity</th><th>Open Pos</th><th>Floating P/L</th><th>Created</th><th>Last Seen</th></tr>";
    for (const a of result.activations) {
        html += `
        <tr>
          <td>${escapeHtml(a.account_login || "")}</td>
          <td>${escapeHtml(a.broker_server || "")}</td>
          <td>${escapeHtml(a.machine_id || "")}</td>
          <td>${safeNum(a.balance)}</td>
          <td>${safeNum(a.equity)}</td>
          <td>${escapeHtml(String(a.open_positions_count ?? 0))}</td>
          <td>${safeNum(a.floating_pnl)}</td>
          <td>${escapeHtml(utcToLocalDisplay(a.created_at || ""))}</td>
          <td>${escapeHtml(utcToLocalDisplay(a.last_seen_at || ""))}</td>
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
        expires_at: localInputValueToUtcString(document.getElementById("editExpiresAt").value.trim()),
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
        await loadAll();
        await openEditModal(key);
    }
}

async function resetCurrentLock() {
    const key = currentEditKey();
    if (!confirm("Reset lock for this license?")) return;

    const result = await apiPost(`/admin/license/${encodeURIComponent(key)}/reset-lock`, {});
    document.getElementById("editResult").textContent = JSON.stringify(result, null, 2);
    if (result.ok) {
        await loadAll();
        await openEditModal(key);
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

async function copyLicense(licenseKey) {
    try {
        await navigator.clipboard.writeText(licenseKey);
        alert("License copied: " + licenseKey);
    } catch (e) {
        alert("Could not copy license.");
    }
}

async function copyCurrentLicense() {
    const key = document.getElementById("editLicenseKey").value.trim();
    if (key) await copyLicense(key);
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

showLoginOnly();
</script>
</body>
</html>
    """
