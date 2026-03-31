from fastapi import FastAPI
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime


app = FastAPI(title="MT5 Copier Test API")


# -----------------------------
# In-memory storage for test
# -----------------------------
LATEST_SNAPSHOT = {
    "snapshot_id": "",
    "timestamp": "",
    "positions": [],
    "pending_orders": []
}

VALID_MASTER_TOKEN = "MASTER123"
VALID_LICENSE_KEYS = {"TEST-001"}


# -----------------------------
# Models
# -----------------------------
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


# -----------------------------
# Routes
# -----------------------------
@app.get("/")
def root():
    return {
        "ok": True,
        "service": "mt5 copier test api",
        "time": datetime.utcnow().isoformat()
    }


@app.post("/slave/activate")
def slave_activate(payload: SlaveActivateRequest):
    if payload.license_key not in VALID_LICENSE_KEYS:
        return {
            "ok": False,
            "message": "Invalid license key"
        }

    return {
        "ok": True,
        "message": "License is valid",
        "mode": "test",
        "poll_seconds": 1
    }


@app.post("/master/publish")
def master_publish(payload: MasterPublishRequest):
    if payload.master_token != VALID_MASTER_TOKEN:
        return {
            "ok": False,
            "message": "Invalid master token"
        }

    LATEST_SNAPSHOT["snapshot_id"] = payload.snapshot_id
    LATEST_SNAPSHOT["timestamp"] = datetime.utcnow().isoformat()
    LATEST_SNAPSHOT["positions"] = [p.model_dump() for p in payload.positions]
    LATEST_SNAPSHOT["pending_orders"] = [o.model_dump() for o in payload.pending_orders]

    return {
        "ok": True,
        "message": "Snapshot saved",
        "snapshot_id": LATEST_SNAPSHOT["snapshot_id"]
    }


@app.post("/slave/pull")
def slave_pull(payload: SlavePullRequest):
    if payload.license_key not in VALID_LICENSE_KEYS:
        return {
            "ok": False,
            "message": "Invalid license key"
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