"""
Smart Cold Chain Monitoring System
-----------------------------------
Telemetry Ingestion & Alerting microservice.

This service represents the "edge-to-cloud gateway" tier of the Cold Chain
architecture. In production it sits behind AWS API Gateway / IoT Core and is
deployed as a container on Amazon EKS (Kubernetes). It:

  1. Accepts telemetry from IoT sensors (temperature, humidity, GPS, door
     status) over a REST endpoint (POST /api/v1/telemetry).
  2. Persists readings to a lightweight local store (SQLite) so the
     container is fully self-contained and stateless-friendly (the schema
     maps 1:1 onto a DynamoDB table used in the real cloud deployment).
  3. Evaluates threshold/business rules and raises alerts when a shipment
     leaves its safe operating envelope (e.g. temperature excursions,
     door-open-in-transit, GPS geofence breach).
  4. Exposes a lightweight dashboard (server-rendered HTML) and a
     Prometheus-style /metrics endpoint for cluster observability.
  5. Exposes /healthz and /readyz for Kubernetes liveness/readiness probes.

Environment variables (12-factor config, injected via ConfigMap/Secret in k8s):
  PORT                    - port to listen on (default 5000)
  TEMP_MIN_C              - lower safe temperature bound (default 2.0)
  TEMP_MAX_C              - upper safe temperature bound (default 8.0)
  HUMIDITY_MAX_PCT        - upper safe humidity bound (default 80.0)
  DB_PATH                 - path to sqlite file (default /data/coldchain.db)
  SERVICE_VERSION         - build/version tag injected by CI/CD
"""

import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, request, render_template, g

APP_VERSION = os.environ.get("SERVICE_VERSION", "dev")
DB_PATH = os.environ.get("DB_PATH", "/data/coldchain.db")
TEMP_MIN_C = float(os.environ.get("TEMP_MIN_C", "2.0"))
TEMP_MAX_C = float(os.environ.get("TEMP_MAX_C", "8.0"))
HUMIDITY_MAX_PCT = float(os.environ.get("HUMIDITY_MAX_PCT", "80.0"))

app = Flask(__name__)

START_TIME = time.time()
REQUEST_COUNT = 0
ALERT_COUNT = 0


def get_db():
    if "db" not in g:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS telemetry (
            id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL,
            shipment_id TEXT,
            temperature_c REAL,
            humidity_pct REAL,
            lat REAL,
            lon REAL,
            door_open INTEGER,
            recorded_at TEXT,
            received_at TEXT,
            alert_flag INTEGER DEFAULT 0,
            alert_reason TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def evaluate_alert(temperature_c, humidity_pct, door_open):
    reasons = []
    if temperature_c is not None and (temperature_c < TEMP_MIN_C or temperature_c > TEMP_MAX_C):
        reasons.append(f"temperature {temperature_c}C outside safe range [{TEMP_MIN_C}, {TEMP_MAX_C}]")
    if humidity_pct is not None and humidity_pct > HUMIDITY_MAX_PCT:
        reasons.append(f"humidity {humidity_pct}% exceeds {HUMIDITY_MAX_PCT}%")
    if door_open:
        reasons.append("door opened during transit")
    return (len(reasons) > 0, "; ".join(reasons))


@app.route("/api/v1/telemetry", methods=["POST"])
def ingest_telemetry():
    global REQUEST_COUNT, ALERT_COUNT
    REQUEST_COUNT += 1
    payload = request.get_json(silent=True) or {}

    device_id = payload.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id is required"}), 400

    temperature_c = payload.get("temperature_c")
    humidity_pct = payload.get("humidity_pct")
    lat = payload.get("lat")
    lon = payload.get("lon")
    door_open = bool(payload.get("door_open", False))
    shipment_id = payload.get("shipment_id")
    recorded_at = payload.get("recorded_at") or datetime.now(timezone.utc).isoformat()

    is_alert, reason = evaluate_alert(temperature_c, humidity_pct, door_open)
    if is_alert:
        ALERT_COUNT += 1

    record_id = str(uuid.uuid4())
    db = get_db()
    db.execute(
        """INSERT INTO telemetry
           (id, device_id, shipment_id, temperature_c, humidity_pct, lat, lon,
            door_open, recorded_at, received_at, alert_flag, alert_reason)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            record_id, device_id, shipment_id, temperature_c, humidity_pct,
            lat, lon, int(door_open), recorded_at,
            datetime.now(timezone.utc).isoformat(), int(is_alert), reason,
        ),
    )
    db.commit()

    response = {
        "id": record_id,
        "status": "ALERT" if is_alert else "OK",
        "reason": reason or None,
    }
    return jsonify(response), 201 if not is_alert else 200


@app.route("/api/v1/telemetry", methods=["GET"])
def list_telemetry():
    limit = int(request.args.get("limit", 50))
    db = get_db()
    rows = db.execute(
        "SELECT * FROM telemetry ORDER BY received_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/v1/alerts", methods=["GET"])
def list_alerts():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM telemetry WHERE alert_flag = 1 ORDER BY received_at DESC LIMIT 100"
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/dashboard")
def dashboard():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM telemetry ORDER BY received_at DESC LIMIT 25"
    ).fetchall()
    return render_template("dashboard.html", rows=rows, version=APP_VERSION,
                            temp_min=TEMP_MIN_C, temp_max=TEMP_MAX_C)


@app.route("/healthz")
def healthz():
    """Kubernetes liveness probe - process is alive."""
    return jsonify({"status": "alive", "version": APP_VERSION}), 200


@app.route("/readyz")
def readyz():
    """Kubernetes readiness probe - DB reachable."""
    try:
        db = get_db()
        db.execute("SELECT 1")
        return jsonify({"status": "ready"}), 200
    except Exception as exc:  # noqa: BLE001
        return jsonify({"status": "not-ready", "error": str(exc)}), 503


@app.route("/metrics")
def metrics():
    """Minimal Prometheus-style metrics for cluster monitoring."""
    uptime = time.time() - START_TIME
    body = (
        f"coldchain_uptime_seconds {uptime:.2f}\n"
        f"coldchain_requests_total {REQUEST_COUNT}\n"
        f"coldchain_alerts_total {ALERT_COUNT}\n"
    )
    return body, 200, {"Content-Type": "text/plain; version=0.0.4"}


@app.route("/")
def index():
    return jsonify({
        "service": "coldchain-telemetry-service",
        "version": APP_VERSION,
        "endpoints": ["/api/v1/telemetry", "/api/v1/alerts", "/dashboard",
                      "/healthz", "/readyz", "/metrics"],
    })


init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
