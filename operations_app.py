"""PostgreSQL master data, MQTT telemetry ingestion, and ClickHouse dashboard API."""

from datetime import datetime, timezone, timedelta
from io import BytesIO
import json
import math
import os
from pathlib import Path
import random
import secrets
import threading
import time
import uuid

import clickhouse_connect
from flask import Flask, g, jsonify, request, send_file, send_from_directory, session
import paho.mqtt.client as mqtt
import psycopg
from psycopg.errors import ForeignKeyViolation, UniqueViolation
from psycopg.rows import dict_row

from report_pdf import make_report_pdf
from reporting import REPORT_TIMEZONE, build_report, period_bounds
from ai_chat import ChatError, answer_chat, call_model, parse_action, private_settings, save_settings, settings_public
from chat_markdown import render_ai_markdown
from work_plan import list_plans, read_plan, save_plan
from auth import (ROLES, clear_login_failures, login_limited, normalize_username,
                  password_hash, password_matches, record_login_failure,
                  secret_file, service_token_matches)
from historical_baseline import FACTORY_TIMEZONE, HISTORY_TOPIC, expected_series, load_profiles, reading_profile, recent_minutes
from chart_ranges import chart_window, historical_bucket_series


app = Flask(__name__, static_folder=None)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = Path(os.getenv("DASHBOARD_DIR", PROJECT_ROOT / "app" / "dist"))
LEGACY_STYLES_DIR = PROJECT_ROOT / "app" / "src" / "styles"
SESSION_SECRET_FILE = os.getenv("SESSION_SECRET_FILE", "data/session_secret.key")
SIMULATOR_SERVICE_KEY_FILE = os.getenv("SIMULATOR_SERVICE_KEY_FILE", "data/simulator_service.key")
app.secret_key = secret_file(SESSION_SECRET_FILE)
secret_file(SIMULATOR_SERVICE_KEY_FILE)
app.config.update(SESSION_COOKIE_NAME=os.getenv("SESSION_COOKIE_NAME", "session"),
                  SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE") == "1",
                  PERMANENT_SESSION_LIFETIME=timedelta(hours=12))
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://wheelworks:wheelworks_local@localhost:5433/wheelworks")
CH_HOST = os.getenv("CLICKHOUSE_HOST", "localhost")
CH_PORT = int(os.getenv("CLICKHOUSE_PORT", "8123" if CH_HOST != "localhost" else "8124"))
MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1884"))
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "factory/wheel-rim/#")
SIM_INTERVAL = max(2, int(os.getenv("SIM_INTERVAL", "5")))
CH_LOCK = threading.Lock()
CH_CLIENT = None
INGEST_STATUS = {"connected": False, "last_message": None, "error": None}


def as_utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def pg():
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def current_user():
    identity = session.get("user_id")
    if not identity:
        return None
    with pg() as conn:
        user = conn.execute("""SELECT id,username,role,enabled,session_version
            FROM app_users WHERE id=%s""", (identity,)).fetchone()
    if not user or not user["enabled"] or user["session_version"] != session.get("session_version"):
        session.clear()
        return None
    return user


def public_user(user):
    return {key: user[key] for key in ("id", "username", "role")}


@app.before_request
def auth_guard():
    path = request.path
    if (path == "/" or path.startswith("/dashboard/") or
            path in ("/operations.css", "/console-theme.css", "/operations.js", "/api/health", "/api/auth/login", "/api/auth/me")):
        return None
    supplied = request.headers.get("Authorization", "")
    token = supplied[7:] if supplied.startswith("Bearer ") else ""
    if token and path == "/api/simulator-catalog" and request.method == "GET" and service_token_matches(SIMULATOR_SERVICE_KEY_FILE, token):
        g.simulator_service = True
        return None
    if token and path == "/api/master/sensors" and request.method == "PUT" and service_token_matches(SIMULATOR_SERVICE_KEY_FILE, token):
        payload = request.get_json(silent=True)
        if isinstance(payload, dict) and set(payload) == {"id", "enabled"} and isinstance(payload["id"], str) and isinstance(payload["enabled"], bool):
            g.simulator_service = True
            return None
        return jsonify({"error": "Simulator credential can update only metering enabled state"}), 403
    user = current_user()
    if not user:
        return jsonify({"error": "Authentication required"}), 401
    g.user = user
    if path.startswith("/api/users") or path.startswith("/api/ai/settings"):
        if user["role"] != "super_admin":
            return jsonify({"error": "Super admin access required"}), 403
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if not secrets.compare_digest(str(session.get("csrf_token", "")), request.headers.get("X-CSRF-Token", "")) or not session.get("csrf_token"):
            return jsonify({"error": "Invalid session token"}), 403
        if (user["role"] == "viewer" and path not in ("/api/auth/logout", "/api/auth/change-password", "/api/ai/chat") and
                not path.startswith("/api/ai/conversations")):
            return jsonify({"error": "Read-only role"}), 403
    return None


@app.after_request
def no_store_api(response):
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/auth/me")
def auth_me():
    user = current_user()
    return jsonify({"user": public_user(user) if user else None,
                    "csrf_token": session.get("csrf_token") if user else None})


@app.post("/api/auth/login")
def auth_login():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "Send a JSON object"}), 400
    username = str(body.get("username", "")).strip().lower()
    password = body.get("password", "")
    key = (request.remote_addr or "local", username)
    if login_limited(key):
        return jsonify({"error": "Too many attempts. Try again in 15 minutes."}), 429
    with pg() as conn:
        user = conn.execute("""SELECT id,username,role,enabled,session_version,password_hash
            FROM app_users WHERE username=%s""", (username,)).fetchone()
        matches = password_matches(user["password_hash"] if user else None, password)
        if not user or not user["enabled"] or not matches:
            record_login_failure(key)
            return jsonify({"error": "Invalid username or password"}), 401
        conn.execute("UPDATE app_users SET last_login_at=now() WHERE id=%s", (user["id"],))
    clear_login_failures(key)
    session.clear()
    session.permanent = True
    session["user_id"] = user["id"]
    session["session_version"] = user["session_version"]
    session["csrf_token"] = secrets.token_urlsafe(32)
    return jsonify({"user": public_user(user), "csrf_token": session["csrf_token"]})


@app.post("/api/auth/logout")
def auth_logout():
    session.clear()
    return jsonify({"ok": True})


@app.post("/api/auth/change-password")
def auth_change_password():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "Send a JSON object"}), 400
    with pg() as conn:
        row = conn.execute("SELECT password_hash FROM app_users WHERE id=%s FOR UPDATE", (g.user["id"],)).fetchone()
        if not row or not password_matches(row["password_hash"], body.get("current_password")):
            return jsonify({"error": "Current password is incorrect"}), 400
        try:
            hashed = password_hash(body.get("new_password"))
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
        updated = conn.execute("""UPDATE app_users SET password_hash=%s,session_version=session_version+1,
            updated_at=now() WHERE id=%s RETURNING session_version""", (hashed, g.user["id"])).fetchone()
    session["session_version"] = updated["session_version"]
    return jsonify({"ok": True})


@app.get("/api/users")
def users_get():
    with pg() as conn:
        rows = conn.execute("""SELECT id,username,role,enabled,created_at,last_login_at
            FROM app_users ORDER BY id""").fetchall()
    return jsonify({"users": rows, "roles": ROLES})


@app.post("/api/users")
def users_create():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "Send a JSON object"}), 400
    try:
        username = normalize_username(body.get("username"))
        role = body.get("role", "viewer")
        if role not in ROLES:
            raise ValueError("Unknown role")
        hashed = password_hash(body.get("password"))
        with pg() as conn:
            row = conn.execute("""INSERT INTO app_users (username,password_hash,role)
                VALUES (%s,%s,%s) RETURNING id""", (username, hashed, role)).fetchone()
    except UniqueViolation:
        return jsonify({"error": "Username already exists"}), 409
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    return jsonify({"id": row["id"], "username": username, "role": role}), 201


@app.patch("/api/users/<int:user_id>")
def users_update(user_id):
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict) or not body or set(body) - {"role", "enabled", "password"}:
        return jsonify({"error": "Send role, enabled, or password"}), 400
    try:
        if "role" in body and body["role"] not in ROLES:
            raise ValueError("Unknown role")
        if "enabled" in body and not isinstance(body["enabled"], bool):
            raise ValueError("enabled must be true or false")
        hashed = password_hash(body["password"]) if "password" in body else None
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    with pg() as conn:
        conn.execute("LOCK TABLE app_users IN SHARE ROW EXCLUSIVE MODE")
        user = conn.execute("SELECT id,role,enabled FROM app_users WHERE id=%s FOR UPDATE", (user_id,)).fetchone()
        if not user:
            return jsonify({"error": "User not found"}), 404
        next_role, next_enabled = body.get("role", user["role"]), body.get("enabled", user["enabled"])
        if user_id == g.user["id"] and (next_role != user["role"] or not next_enabled):
            return jsonify({"error": "You cannot demote or disable your own account"}), 400
        if user["role"] == "super_admin" and user["enabled"] and (next_role != "super_admin" or not next_enabled):
            count = conn.execute("SELECT count(*) AS n FROM app_users WHERE role='super_admin' AND enabled").fetchone()["n"]
            if count <= 1:
                return jsonify({"error": "At least one enabled super admin is required"}), 400
        result = conn.execute("""UPDATE app_users SET role=%s,enabled=%s,
            password_hash=COALESCE(%s,password_hash),session_version=session_version+1,
            updated_at=now() WHERE id=%s RETURNING session_version""",
            (next_role, next_enabled, hashed, user_id)).fetchone()
    if user_id == g.user["id"]:
        session["session_version"] = result["session_version"]
    return jsonify({"ok": True})


@app.delete("/api/users/<int:user_id>")
def users_delete(user_id):
    if user_id == g.user["id"]:
        return jsonify({"error": "You cannot delete your own account"}), 400
    with pg() as conn:
        conn.execute("LOCK TABLE app_users IN SHARE ROW EXCLUSIVE MODE")
        user = conn.execute("SELECT role,enabled FROM app_users WHERE id=%s FOR UPDATE", (user_id,)).fetchone()
        if not user:
            return jsonify({"error": "User not found"}), 404
        if user["role"] == "super_admin" and user["enabled"]:
            count = conn.execute("SELECT count(*) AS n FROM app_users WHERE role='super_admin' AND enabled").fetchone()["n"]
            if count <= 1:
                return jsonify({"error": "At least one enabled super admin is required"}), 400
        conn.execute("DELETE FROM app_users WHERE id=%s", (user_id,))
    return jsonify({"ok": True})


def ch():
    global CH_CLIENT
    if CH_CLIENT is None:
        CH_CLIENT = clickhouse_connect.get_client(
            host=CH_HOST, port=CH_PORT, username=os.getenv("CLICKHOUSE_USER", "wheelworks"),
            password=os.getenv("CLICKHOUSE_PASSWORD", "wheelworks_local"), database="wheelworks",
        )
    return CH_CLIENT


SCHEMA = """
CREATE TABLE IF NOT EXISTS factories (
 id SERIAL PRIMARY KEY, name TEXT NOT NULL UNIQUE, code TEXT NOT NULL UNIQUE,
 description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS locations (
 id SERIAL PRIMARY KEY, factory_id INTEGER NOT NULL REFERENCES factories(id),
 name TEXT NOT NULL, code TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
 UNIQUE(factory_id, code)
);
CREATE TABLE IF NOT EXISTS production_lines (
 id SERIAL PRIMARY KEY, location_id INTEGER NOT NULL REFERENCES locations(id),
 name TEXT NOT NULL, code TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
 UNIQUE(location_id, code)
);
CREATE TABLE IF NOT EXISTS machines (
 id SERIAL PRIMARY KEY, production_line_id INTEGER NOT NULL REFERENCES production_lines(id),
 name TEXT NOT NULL, code TEXT NOT NULL, category TEXT NOT NULL,
 stage INTEGER NOT NULL DEFAULT 1, lane INTEGER NOT NULL DEFAULT 1,
 enabled BOOLEAN NOT NULL DEFAULT true,
 description TEXT NOT NULL DEFAULT '', UNIQUE(production_line_id, code)
);
CREATE TABLE IF NOT EXISTS machine_flow_links (
 left_machine_id INTEGER NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
 right_machine_id INTEGER NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
 PRIMARY KEY (left_machine_id, right_machine_id),
 CHECK (left_machine_id <> right_machine_id)
);
CREATE TABLE IF NOT EXISTS work_plans (
 id SERIAL PRIMARY KEY,
 start_at TIMESTAMPTZ NOT NULL,
 duration_minutes INTEGER NOT NULL CHECK (duration_minutes BETWEEN 15 AND 1440),
 pic TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS work_plan_machines (
 plan_id INTEGER NOT NULL REFERENCES work_plans(id) ON DELETE CASCADE,
 machine_id INTEGER NOT NULL REFERENCES machines(id) ON DELETE CASCADE,
 planned_on BOOLEAN NOT NULL,
 PRIMARY KEY(plan_id,machine_id)
);
CREATE TABLE IF NOT EXISTS sensors (
 id TEXT PRIMARY KEY, machine_id INTEGER NOT NULL REFERENCES machines(id),
 name TEXT NOT NULL, type TEXT NOT NULL, unit TEXT NOT NULL,
 baseline DOUBLE PRECISION NOT NULL CHECK (baseline >= 0),
 meter_role TEXT NOT NULL DEFAULT 'consumption' CHECK (meter_role IN ('consumption','supply')),
 enabled BOOLEAN NOT NULL DEFAULT false,
 min_value DOUBLE PRECISION NOT NULL DEFAULT 0,
 max_value DOUBLE PRECISION NOT NULL DEFAULT 500,
 CHECK (max_value > min_value)
);
CREATE TABLE IF NOT EXISTS metering_alerts (
 id BIGSERIAL PRIMARY KEY,
 sensor_id TEXT NOT NULL,
 sensor_name TEXT NOT NULL,
 machine_name TEXT NOT NULL,
 location_name TEXT NOT NULL,
 utility_type TEXT NOT NULL,
 unit TEXT NOT NULL,
 baseline DOUBLE PRECISION NOT NULL,
 alert_type TEXT NOT NULL DEFAULT 'baseline',
 threshold_value DOUBLE PRECISION NOT NULL,
 first_value DOUBLE PRECISION NOT NULL,
 latest_value DOUBLE PRECISION NOT NULL,
 peak_value DOUBLE PRECISION NOT NULL,
 exceedance_count INTEGER NOT NULL DEFAULT 1,
 started_at TIMESTAMPTZ NOT NULL,
 last_seen_at TIMESTAMPTZ NOT NULL,
 last_observed_at TIMESTAMPTZ NOT NULL,
 resolved_at TIMESTAMPTZ,
 machine_id INTEGER,
 review_status TEXT,
 review_note TEXT,
 reviewed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS metering_alerts_recent
 ON metering_alerts(started_at DESC);
CREATE TABLE IF NOT EXISTS metering_condition_state (
 sensor_id TEXT PRIMARY KEY,
 machine_id INTEGER NOT NULL,
 last_value DOUBLE PRECISION NOT NULL,
 last_seen_at TIMESTAMPTZ NOT NULL,
 zero_since TIMESTAMPTZ,
 maximum_since TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS metering_baseline_profiles (
 sensor_id TEXT NOT NULL REFERENCES sensors(id) ON DELETE CASCADE,
 day_of_week SMALLINT NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
 slot SMALLINT NOT NULL CHECK (slot BETWEEN 0 AND 95),
 expected DOUBLE PRECISION NOT NULL,
 lower_bound DOUBLE PRECISION NOT NULL,
 upper_bound DOUBLE PRECISION NOT NULL,
 sample_count INTEGER NOT NULL,
 day_count INTEGER NOT NULL,
 source TEXT NOT NULL,
 generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(sensor_id,day_of_week,slot)
);
CREATE TABLE IF NOT EXISTS historical_seed_runs (
 id TEXT PRIMARY KEY,
 started_at TIMESTAMPTZ NOT NULL,
 ended_at TIMESTAMPTZ NOT NULL,
 row_count BIGINT NOT NULL,
 meter_count INTEGER NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS app_users (
 id BIGSERIAL PRIMARY KEY,
 username TEXT NOT NULL UNIQUE,
 password_hash TEXT NOT NULL,
 role TEXT NOT NULL CHECK (role IN ('super_admin','admin','viewer')),
 enabled BOOLEAN NOT NULL DEFAULT true,
 session_version INTEGER NOT NULL DEFAULT 1,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 last_login_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS ai_conversations (
 id UUID PRIMARY KEY,
 user_id BIGINT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
 title TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ai_conversations_by_user
 ON ai_conversations(user_id, updated_at DESC);
CREATE TABLE IF NOT EXISTS ai_chat_messages (
 id BIGSERIAL PRIMARY KEY,
 conversation_id UUID NOT NULL REFERENCES ai_conversations(id) ON DELETE CASCADE,
 role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
 content TEXT NOT NULL,
 payload JSONB NOT NULL DEFAULT '{}'::jsonb,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ai_chat_messages_by_conversation
 ON ai_chat_messages(conversation_id, id);
"""


FLOW = [
    ("Raw Material Warehouse", "RAW", "Warehouse", 1, 2),
    ("Charging Area", "CHG", "Charging", 2, 2),
    ("Melting Furnace 01", "MELT-01", "Melting", 3, 1),
    ("Melting Furnace 02", "MELT-02", "Melting", 3, 2),
    ("Melting Furnace 03", "MELT-03", "Melting", 3, 3),
    ("Melting Furnace 04", "MELT-04", "Melting", 3, 4),
    ("Rotary Degassing 01", "RD-01", "Degassing", 4, 1),
    ("Rotary Degassing 02", "RD-02", "Degassing", 4, 2),
    ("Rotary Degassing 03", "RD-03", "Degassing", 4, 3),
    ("Rotary Degassing 04", "RD-04", "Degassing", 4, 4),
    ("Casting 01", "CAST-01", "Casting", 5, 1),
    ("Casting 02", "CAST-02", "Casting", 5, 2),
    ("Casting 03", "CAST-03", "Casting", 5, 3),
    ("Casting 04", "CAST-04", "Casting", 5, 4),
    ("Casting 05", "CAST-05", "Casting", 5, 5),
    ("T6 Heat Treatment 01", "T6-01", "Heat treatment", 6, 2),
    ("T6 Heat Treatment 02", "T6-02", "Heat treatment", 6, 4),
    ("Machining 01", "MACH-01", "Machining", 7, 1),
    ("Machining 02", "MACH-02", "Machining", 7, 2),
    ("Machining 03", "MACH-03", "Machining", 7, 3),
    ("Machining 04", "MACH-04", "Machining", 7, 4),
    ("Painting 01", "PAINT-01", "Painting", 8, 2),
    ("Painting 02", "PAINT-02", "Painting", 8, 4),
]

SEED_FLOW_EDGES = [
    ("RAW", "CHG"),
    *(("CHG", f"MELT-0{n}") for n in range(1, 5)),
    *((f"MELT-0{n}", f"RD-0{n}") for n in range(1, 5)),
    ("RD-01", "CAST-01"), ("RD-01", "CAST-02"),
    ("RD-02", "CAST-03"), ("RD-03", "CAST-04"), ("RD-04", "CAST-05"),
    ("CAST-01", "T6-01"), ("CAST-02", "T6-01"),
    ("CAST-03", "T6-02"), ("CAST-04", "T6-02"), ("CAST-05", "T6-02"),
    ("T6-01", "MACH-01"), ("T6-01", "MACH-02"),
    ("T6-02", "MACH-03"), ("T6-02", "MACH-04"),
    ("MACH-01", "PAINT-01"), ("MACH-02", "PAINT-01"),
    ("MACH-03", "PAINT-02"), ("MACH-04", "PAINT-02"),
]

LEGACY_SENSORS = [
    # Sensor Studio ID, previous dashboard ID, map machine, type, unit, baseline, maximum.
    ("raw-material-warehouse--clean-water", "cast-01--clean-water", "RAW", "Clean Water", "m³/h", 22, 100),
    ("charging-area--clean-water", "cast-03--clean-water", "CHG", "Clean Water", "m³/h", 22, 100),
    ("raw-material-warehouse--compressed-air", "rd-01--compressed-air", "RAW", "Compressed Air", "m³/h", 38, 150),
    ("charging-area--compressed-air", "mach-02--compressed-air", "CHG", "Compressed Air", "m³/h", 38, 150),
    ("charging-area--diesel", "chg--diesel", "CHG", "Diesel", "L/h", 24, 100),
    ("raw-material-warehouse--diesel", "t6-02--diesel", "RAW", "Diesel", "L/h", 24, 100),
    ("raw-material-warehouse--electricity", "melt-01--electricity", "RAW", "Electricity", "kW", 0, 500),
    ("charging-area--electricity", "mach-01--electricity", "CHG", "Electricity", "kW", 0, 500),
    ("charging-area--natural-gas", "melt-02--natural-gas", "CHG", "Natural Gas", "m³/h", 30, 120),
    ("melting-furnace--natural-gas", "t6-01--natural-gas", "MELT-01", "Natural Gas", "m³/h", 30, 120),
]


def init_storage():
    with pg() as conn:
        conn.execute(SCHEMA)
        conn.execute("ALTER TABLE sensors ADD COLUMN IF NOT EXISTS meter_role TEXT NOT NULL DEFAULT 'consumption'")
        conn.execute("ALTER TABLE machines ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT true")
        conn.execute("ALTER TABLE metering_alerts ADD COLUMN IF NOT EXISTS alert_type TEXT NOT NULL DEFAULT 'baseline'")
        conn.execute("ALTER TABLE metering_alerts ADD COLUMN IF NOT EXISTS threshold_value DOUBLE PRECISION")
        conn.execute("ALTER TABLE metering_alerts ADD COLUMN IF NOT EXISTS machine_id INTEGER")
        conn.execute("ALTER TABLE metering_alerts ADD COLUMN IF NOT EXISTS review_status TEXT")
        conn.execute("ALTER TABLE metering_alerts ADD COLUMN IF NOT EXISTS review_note TEXT")
        conn.execute("ALTER TABLE metering_alerts ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ")
        conn.execute("UPDATE metering_alerts SET threshold_value=baseline WHERE threshold_value IS NULL")
        conn.execute("ALTER TABLE metering_alerts ALTER COLUMN threshold_value SET NOT NULL")
        conn.execute("DROP INDEX IF EXISTS metering_alerts_one_open_per_meter")
        conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS metering_alerts_one_open_per_type
            ON metering_alerts(sensor_id,alert_type) WHERE resolved_at IS NULL""")
        conn.execute("""DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='utilitymind_ai_reader') THEN
                CREATE ROLE utilitymind_ai_reader NOLOGIN;
            END IF;
        END $$""")
        conn.execute("GRANT USAGE ON SCHEMA public TO utilitymind_ai_reader")
        conn.execute("GRANT SELECT ON factories,locations,production_lines,machines,sensors,work_plans,work_plan_machines TO utilitymind_ai_reader")
        conn.execute("GRANT SELECT ON metering_baseline_profiles,historical_seed_runs TO utilitymind_ai_reader")
        conn.execute("INSERT INTO app_settings VALUES ('simulator_running', 'true') ON CONFLICT DO NOTHING")
        if not conn.execute("SELECT count(*) AS n FROM factories").fetchone()["n"]:
            seed_master_data(conn)
        if not conn.execute("SELECT 1 FROM app_settings WHERE key='machine_flow_seeded'").fetchone():
            seed_machine_flow(conn)
            conn.execute("INSERT INTO app_settings VALUES ('machine_flow_seeded', 'true')")
        conn.execute("ALTER TABLE sensors DROP CONSTRAINT IF EXISTS sensors_baseline_check")
        conn.execute("ALTER TABLE sensors ADD CONSTRAINT sensors_baseline_check CHECK (baseline >= 0)")
        if not conn.execute("SELECT 1 FROM app_settings WHERE key='legacy_bridge_migrated'").fetchone():
            ensure_seed_sensors(conn)
            conn.execute("INSERT INTO app_settings VALUES ('legacy_bridge_migrated', 'true')")
        conn.execute("DELETE FROM sensors WHERE lower(type) IN ('waste', 'capacity / occupancy')")
        if os.getenv("SIMULATOR_MODE") == "external":
            conn.execute("UPDATE app_settings SET value='false' WHERE key='simulator_running'")
    with CH_LOCK:
        ch().command("""CREATE TABLE IF NOT EXISTS telemetry (
            event_id UUID, sensor_id String, value Float64, unit LowCardinality(String),
            observed_at DateTime64(3, 'UTC'), received_at DateTime64(3, 'UTC'),
            topic String, simulated Bool
        ) ENGINE = MergeTree ORDER BY (sensor_id, observed_at, event_id)""")


def seed_master_data(conn):
        factory = conn.execute("INSERT INTO factories (name,code,description) VALUES (%s,%s,%s) RETURNING id",
                               ("Wheelworks Plant", "WW-01", "Wheel rim manufacturing pilot")).fetchone()["id"]
        location = conn.execute("INSERT INTO locations (factory_id,name,code,description) VALUES (%s,%s,%s,%s) RETURNING id",
                                (factory, "Main Production Hall", "HALL-A", "Foundry through painting")).fetchone()["id"]
        line = conn.execute("INSERT INTO production_lines (location_id,name,code,description) VALUES (%s,%s,%s,%s) RETURNING id",
                            (location, "Wheel Rim Line A", "LINE-A", "Process flow from supplied reference map")).fetchone()["id"]
        for name, code, category, stage, lane in FLOW:
            conn.execute(
                "INSERT INTO machines (production_line_id,name,code,category,stage,lane) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                (line, name, code, category, stage, lane),
            )


def seed_machine_flow(conn):
    rows = conn.execute("""SELECT m.id,m.code,p.id AS line_id FROM machines m
        JOIN production_lines p ON p.id=m.production_line_id
        JOIN factories f ON f.id=(SELECT l.factory_id FROM locations l WHERE l.id=p.location_id)
        WHERE f.code='WW-01' AND p.code='LINE-A'""").fetchall()
    machine_ids = {row["code"]: row["id"] for row in rows}
    for left_code, right_code in SEED_FLOW_EDGES:
        if left_code in machine_ids and right_code in machine_ids:
            conn.execute("""INSERT INTO machine_flow_links (left_machine_id,right_machine_id)
                VALUES (%s,%s) ON CONFLICT DO NOTHING""", (machine_ids[left_code], machine_ids[right_code]))


def ensure_seed_sensors(conn):
    machine_ids = {row["code"]: row["id"] for row in conn.execute("""SELECT m.code,m.id FROM machines m
        JOIN production_lines p ON p.id=m.production_line_id JOIN locations l ON l.id=p.location_id
        JOIN factories f ON f.id=l.factory_id WHERE f.code='WW-01' AND p.code='LINE-A'""")}
    counts = {}
    for sensor_id, previous_id, code, category, unit, baseline, maximum in LEGACY_SENSORS:
        if code not in machine_ids:
            continue
        counts[category] = counts.get(category, 0) + 1
        name = f"{category} {counts[category]:02d}"
        if conn.execute("SELECT 1 FROM sensors WHERE id=%s", (sensor_id,)).fetchone():
            conn.execute("DELETE FROM sensors WHERE id=%s", (previous_id,))
        elif conn.execute("SELECT 1 FROM sensors WHERE id=%s", (previous_id,)).fetchone():
            conn.execute("""UPDATE sensors SET id=%s,machine_id=%s,name=%s,type=%s,unit=%s,
                baseline=%s,enabled=true,min_value=0,max_value=%s WHERE id=%s""",
                (sensor_id, machine_ids[code], name, category, unit, baseline, maximum, previous_id))
        else:
            conn.execute("""INSERT INTO sensors
                (id,machine_id,name,type,unit,baseline,enabled,min_value,max_value)
                VALUES (%s,%s,%s,%s,%s,%s,true,0,%s)""",
                (sensor_id, machine_ids[code], name, category, unit, baseline, maximum))


def master_data():
    with pg() as conn:
        return {
            "factories": conn.execute("SELECT * FROM factories ORDER BY id").fetchall(),
            "locations": conn.execute("""SELECT l.*, f.name AS factory_name FROM locations l
                JOIN factories f ON f.id=l.factory_id ORDER BY l.id""").fetchall(),
            "production_lines": conn.execute("""SELECT p.*, l.name AS location_name, f.name AS factory_name FROM production_lines p
                JOIN locations l ON l.id=p.location_id JOIN factories f ON f.id=l.factory_id ORDER BY p.id""").fetchall(),
            "machines": conn.execute("""SELECT m.*, p.location_id, l.factory_id, p.name AS production_line_name, l.name AS location_name,
                f.name AS factory_name,
                COALESCE((SELECT array_agg(fl.left_machine_id ORDER BY fl.left_machine_id)
                    FROM machine_flow_links fl WHERE fl.right_machine_id=m.id), ARRAY[]::INTEGER[]) AS left_machine_ids,
                COALESCE((SELECT array_agg(fl.right_machine_id ORDER BY fl.right_machine_id)
                    FROM machine_flow_links fl WHERE fl.left_machine_id=m.id), ARRAY[]::INTEGER[]) AS right_machine_ids
                FROM machines m JOIN production_lines p ON p.id=m.production_line_id
                JOIN locations l ON l.id=p.location_id JOIN factories f ON f.id=l.factory_id ORDER BY m.stage,m.lane,m.id""").fetchall(),
            "sensors": conn.execute("""SELECT s.*, m.name AS machine_name, m.code AS machine_code,
                p.name AS production_line_name, l.name AS location_name, f.name AS factory_name
                FROM sensors s JOIN machines m ON m.id=s.machine_id
                JOIN production_lines p ON p.id=m.production_line_id
                JOIN locations l ON l.id=p.location_id JOIN factories f ON f.id=l.factory_id
                ORDER BY s.type,s.id""").fetchall(),
            "simulator_running": conn.execute("SELECT value FROM app_settings WHERE key='simulator_running'").fetchone()["value"] == "true",
        }


def latest_readings():
    with CH_LOCK:
        rows = ch().query("""SELECT sensor_id, argMax(value, observed_at) AS latest_value,
            max(observed_at) AS latest_at FROM telemetry GROUP BY sensor_id""").result_rows
        count = ch().query("SELECT count() FROM telemetry").first_row[0]
    return {sensor_id: {"value": value, "observed_at": observed_at.isoformat() + "Z" if observed_at.tzinfo is None else observed_at.isoformat()}
            for sensor_id, value, observed_at in rows}, count


def overview_data():
    data = master_data()
    latest, count = latest_readings()
    now = datetime.now(timezone.utc)
    with pg() as conn:
        profiles = load_profiles(conn, [sensor["id"] for sensor in data["sensors"]], [now])
    for sensor in data["sensors"]:
        reading = latest.get(sensor["id"])
        age = (now - datetime.fromisoformat(reading["observed_at"].replace("Z", "+00:00"))).total_seconds() if reading else None
        fresh = bool(age is not None and 0 <= age <= SIM_INTERVAL * 3)
        sensor["reading"] = reading if fresh and sensor["enabled"] else None
        sensor["status"] = "live" if fresh and sensor["enabled"] else "off" if not sensor["enabled"] else "stale"
        profile = reading_profile(profiles, sensor["id"], now)
        sensor["historical_baseline"] = ({"expected": profile["expected"],
            "lower": profile["lower_bound"], "upper": profile["upper_bound"],
            "sample_count": profile["sample_count"], "source": profile["source"]}
            if profile else None)
    for machine in data["machines"]:
        linked = [s for s in data["sensors"] if s["machine_id"] == machine["id"]]
        machine["sensors"] = linked
        machine["status"] = "live" if machine["enabled"] and any(s["status"] == "live" for s in linked) else "off"
    with pg() as conn:
        data["metering_alert_count"] = conn.execute(
            "SELECT count(*) AS n FROM metering_alerts WHERE resolved_at IS NULL AND review_status IS NULL"
        ).fetchone()["n"]
    data["telemetry_count"] = count
    data["ingest"] = dict(INGEST_STATUS)
    data["interval"] = SIM_INTERVAL
    return data


TABLES = {
    "factories": ("name", "code", "description"),
    "locations": ("factory_id", "name", "code", "description"),
    "production_lines": ("location_id", "name", "code", "description"),
    "machines": ("production_line_id", "name", "code", "category", "stage", "lane", "enabled", "description"),
    "sensors": ("id", "machine_id", "name", "type", "unit", "baseline", "meter_role", "enabled", "min_value", "max_value"),
}


def validated(table, values, creating):
    fields = TABLES[table]
    output = {}
    for field in fields:
        if field not in values:
            if creating and field not in ("description", "enabled", "stage", "lane", "min_value", "max_value", "meter_role"):
                raise ValueError(f"{field} is required")
            continue
        value = values[field]
        if field in ("factory_id", "location_id", "production_line_id", "machine_id", "stage", "lane"):
            value = int(value)
            if value < 1:
                raise ValueError(f"{field} must be positive")
            if field == "stage" and value > 8 or field == "lane" and value > 5:
                raise ValueError(f"{field} is outside the map grid")
        elif field in ("baseline", "min_value", "max_value"):
            value = float(value)
            if not -1_000_000 < value < 1_000_000:
                raise ValueError(f"{field} is out of range")
        elif field == "enabled":
            if not isinstance(value, bool):
                raise ValueError("enabled must be true or false")
        else:
            value = str(value).strip()
            if len(value) > 160 or (not value and field != "description"):
                raise ValueError(f"Invalid {field}")
        output[field] = value
    if not creating:
        output.pop("id", None)
    if table == "sensors" and output.get("type", "").casefold() in {"waste", "capacity / occupancy"}:
        raise ValueError("This metering category is no longer measured")
    if table == "sensors" and output.get("meter_role", "consumption") not in {"consumption", "supply"}:
        raise ValueError("Meter role must be consumption or supply")
    if not output:
        raise ValueError("No fields to update")
    return output


def flow_has_cycle(edges):
    followers = {}
    for left_id, right_id in edges:
        followers.setdefault(left_id, set()).add(right_id)
    visiting, visited = set(), set()

    def visit(machine_id):
        if machine_id in visiting:
            return True
        if machine_id in visited:
            return False
        visiting.add(machine_id)
        for next_id in followers.get(machine_id, ()):
            if visit(next_id):
                return True
        visiting.remove(machine_id)
        visited.add(machine_id)
        return False

    return any(visit(machine_id) for machine_id in followers)


def set_machine_flow(conn, machine_id, left_ids, right_ids):
    machine = conn.execute("SELECT production_line_id FROM machines WHERE id=%s", (machine_id,)).fetchone()
    if not machine:
        raise ValueError("Machine not found")

    def parse_ids(values, side):
        if not isinstance(values, list):
            raise ValueError(f"{side} must be a list of machines")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in values):
            raise ValueError(f"{side} contains an invalid machine")
        if len(values) != len(set(values)) or machine_id in values:
            raise ValueError(f"{side} contains a duplicate or the machine itself")
        return set(values)

    left, right = parse_ids(left_ids, "Left"), parse_ids(right_ids, "Right")
    neighbors = left | right
    if neighbors:
        rows = conn.execute("SELECT id,production_line_id FROM machines WHERE id=ANY(%s)",
                            (list(neighbors),)).fetchall()
        if len(rows) != len(neighbors) or any(row["production_line_id"] != machine["production_line_id"] for row in rows):
            raise ValueError("Left and right machines must belong to the same production line")

    existing = conn.execute("SELECT left_machine_id,right_machine_id FROM machine_flow_links").fetchall()
    edges = {(row["left_machine_id"], row["right_machine_id"]) for row in existing
             if machine_id not in (row["left_machine_id"], row["right_machine_id"])}
    edges.update((other_id, machine_id) for other_id in left)
    edges.update((machine_id, other_id) for other_id in right)
    if flow_has_cycle(edges):
        raise ValueError("These left and right selections would create a circular flow")

    conn.execute("DELETE FROM machine_flow_links WHERE left_machine_id=%s OR right_machine_id=%s",
                 (machine_id, machine_id))
    for other_id in sorted(left):
        conn.execute("INSERT INTO machine_flow_links (left_machine_id,right_machine_id) VALUES (%s,%s)",
                     (other_id, machine_id))
    for other_id in sorted(right):
        conn.execute("INSERT INTO machine_flow_links (left_machine_id,right_machine_id) VALUES (%s,%s)",
                     (machine_id, other_id))


@app.get("/")
def index():
    return send_from_directory(DASHBOARD_DIR, "index.html")


@app.get("/dashboard/<path:name>")
def dashboard_asset(name):
    return send_from_directory(DASHBOARD_DIR, name)


@app.get("/<path:name>")
def static_file(name):
    if name not in ("operations.css", "console-theme.css", "operations.js"):
        return "Not found", 404
    if name in ("operations.css", "console-theme.css"):
        return send_from_directory(LEGACY_STYLES_DIR, name)
    return send_from_directory(PROJECT_ROOT / "app" / "legacy", name)


@app.get("/api/dashboard")
def dashboard():
    return jsonify(overview_data())


@app.get("/api/alerts")
@app.get("/api/notifications")
def alerts_get():
    with pg() as conn:
        rows = conn.execute("""SELECT * FROM metering_alerts
            ORDER BY (resolved_at IS NULL AND review_status IS NULL) DESC,
            CASE WHEN alert_type='prolonged_maximum' THEN 0
                 WHEN alert_type='maximum' THEN 1
                 WHEN alert_type IN ('historical_high','historical_low') THEN 2 ELSE 3 END,
            last_seen_at DESC LIMIT 200""").fetchall()
        open_count = conn.execute("""SELECT count(*) AS n FROM metering_alerts
            WHERE resolved_at IS NULL AND review_status IS NULL""").fetchone()["n"]
    response = jsonify({"alerts": rows, "open_count": open_count})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/notifications/<int:alert_id>/action")
def alert_action(alert_id):
    body = request.get_json(silent=True) or {}
    action = body.get("action")
    note = str(body.get("note") or "").strip()
    if action not in {"ignore", "resolve", "confirm_off", "confirm_on"}:
        return jsonify({"error": "Unknown notification action"}), 400
    if action == "resolve" and not note:
        return jsonify({"error": "A resolution note is required"}), 400
    if len(note) > 1000:
        return jsonify({"error": "Resolution note is too long"}), 400
    with pg() as conn:
        identity = conn.execute("SELECT machine_id FROM metering_alerts WHERE id=%s", (alert_id,)).fetchone()
        if not identity:
            return jsonify({"error": "Notification not found"}), 404
        machine = None
        if identity["machine_id"]:
            machine = conn.execute("SELECT enabled FROM machines WHERE id=%s FOR UPDATE",
                                   (identity["machine_id"],)).fetchone()
        alert = conn.execute("SELECT * FROM metering_alerts WHERE id=%s FOR UPDATE", (alert_id,)).fetchone()
        if not alert:
            return jsonify({"error": "Notification not found"}), 404
        if alert["resolved_at"] or alert["review_status"]:
            return jsonify({"error": "Notification has already been handled"}), 409
        if action in {"confirm_off", "confirm_on"}:
            expected_type = "machine_off_question" if action == "confirm_off" else "machine_on_question"
            if alert["alert_type"] != expected_type or not alert["machine_id"]:
                return jsonify({"error": "This action does not apply to the notification"}), 400
            if not machine:
                return jsonify({"error": "Machine not found"}), 404
            target_enabled = action == "confirm_on"
            if machine["enabled"] == target_enabled:
                return jsonify({"error": "Machine status has already changed"}), 409
            conn.execute("UPDATE machines SET enabled=%s WHERE id=%s", (target_enabled, alert["machine_id"]))
        elif action == "resolve" and alert["alert_type"] in {"machine_off_question", "machine_on_question"}:
            return jsonify({"error": "Confirm the machine status or ignore this question"}), 400
        now = datetime.now(timezone.utc)
        status = {"confirm_off": "confirmed_off", "confirm_on": "confirmed_on"}.get(action, action)
        conn.execute("""UPDATE metering_alerts SET review_status=%s,review_note=%s,reviewed_at=%s,
            resolved_at=CASE WHEN %s IN ('confirmed_off','confirmed_on') THEN %s ELSE resolved_at END
            WHERE id=%s""", (status, note or None, now, status, now, alert_id))
    return jsonify({"ok": True, "review_status": status})


@app.get("/api/work-plans")
def work_plans_get():
    with pg() as conn:
        plans = list_plans(conn)
    response = jsonify({"plans": plans})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/work-plans/<int:plan_id>")
def work_plan_get(plan_id):
    with pg() as conn:
        plan = read_plan(conn, plan_id)
    if plan is None:
        return jsonify({"error": "Work plan not found."}), 404
    observed_end = min(datetime.fromisoformat(plan["end_at"]), datetime.now(timezone.utc))
    if observed_end > datetime.fromisoformat(plan["start_at"]):
        meter_by_id = {meter["id"]: meter for meter in master_data()["sensors"]
                       if meter["machine_id"] in {machine["machine_id"] for machine in plan["machines"]}}
        if meter_by_id:
            with CH_LOCK:
                rows = ch().query("""SELECT sensor_id,count() AS samples,countIf(value>0) AS positive_samples,
                    max(value) AS peak_rate,argMax(value,observed_at) AS latest_rate,
                    max(observed_at) AS latest_at FROM telemetry
                    WHERE sensor_id IN {ids:Array(String)}
                    AND observed_at >= toDateTime64({start:String},3,'UTC')
                    AND observed_at < toDateTime64({end:String},3,'UTC')
                    GROUP BY sensor_id""", parameters={"ids": list(meter_by_id),
                    "start": datetime.fromisoformat(plan["start_at"]).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f"),
                    "end": observed_end.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")}).result_rows
            observed = {row[0]: row for row in rows}
            for machine in plan["machines"]:
                machine["metering"] = []
                for meter_id, meter in meter_by_id.items():
                    if meter["machine_id"] != machine["machine_id"]:
                        continue
                    row = observed.get(meter_id)
                    if row:
                        machine["metering"].append({"name": meter["name"], "type": meter["type"],
                            "unit": meter["unit"], "samples": row[1], "positive_samples": row[2],
                            "peak_rate": row[3], "latest_rate": row[4],
                            "latest_at": as_utc(row[5]).isoformat(),
                            "unexpected_use": not machine["planned_on"] and row[2] > 0})
                    else:
                        machine["metering"].append({"name": meter["name"], "type": meter["type"],
                            "unit": meter["unit"], "samples": 0, "positive_samples": 0,
                            "unexpected_use": False})
    for machine in plan["machines"]:
        machine.setdefault("metering", [])
    plan["unexpected_machine_count"] = sum(any(m["unexpected_use"] for m in machine["metering"])
                                           for machine in plan["machines"])
    response = jsonify(plan)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/work-plans")
def work_plan_create():
    try:
        with pg() as conn:
            plan = save_plan(conn, request.get_json(silent=True))
        return jsonify(plan), 201
    except ValueError as error:
        return jsonify({"error": str(error)}), 400


@app.put("/api/work-plans/<int:plan_id>")
def work_plan_put(plan_id):
    try:
        with pg() as conn:
            plan = save_plan(conn, request.get_json(silent=True), plan_id)
        return jsonify(plan) if plan else (jsonify({"error": "Work plan not found."}), 404)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400


@app.delete("/api/work-plans/<int:plan_id>")
def work_plan_delete(plan_id):
    with pg() as conn:
        deleted = conn.execute("DELETE FROM work_plans WHERE id=%s RETURNING id", (plan_id,)).fetchone()
    return ("", 204) if deleted else (jsonify({"error": "Work plan not found."}), 404)


@app.get("/api/ai/settings")
def ai_settings_get():
    with pg() as conn:
        response = jsonify(settings_public(conn))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/api/ai/settings")
def ai_settings_save():
    if request.content_type != "application/json":
        return jsonify({"error": "Send JSON settings."}), 415
    values = request.get_json(silent=True) or {}
    try:
        with pg() as conn:
            result = save_settings(conn, values)
        response = jsonify(result)
        response.headers["Cache-Control"] = "no-store"
        return response
    except ChatError as error:
        return jsonify({"error": str(error)}), 400


@app.get("/api/ai/conversations")
def ai_conversations_list():
    with pg() as conn:
        rows = conn.execute("""SELECT id,title,updated_at FROM ai_conversations
            WHERE user_id=%s ORDER BY updated_at DESC,id DESC""",
            (g.user["id"],)).fetchall()
    return jsonify({"conversations": [
        {"id": str(row["id"]), "title": row["title"], "updated_at": row["updated_at"].isoformat()}
        for row in rows]})


@app.get("/api/ai/conversations/<uuid:conversation_id>")
def ai_conversation_get(conversation_id):
    with pg() as conn:
        conversation = conn.execute("""SELECT id,title,updated_at FROM ai_conversations
            WHERE id=%s AND user_id=%s""", (conversation_id, g.user["id"])).fetchone()
        if not conversation:
            return jsonify({"error": "Chat not found."}), 404
        rows = conn.execute("""SELECT role,content,payload FROM ai_chat_messages
            WHERE conversation_id=%s ORDER BY id""", (conversation_id,)).fetchall()
    messages = []
    for row in rows:
        payload = row["payload"] or {}
        message = {"role": row["role"], "text": row["content"]}
        if row["role"] == "assistant":
            message.update({"failed": bool(payload.get("failed")),
                            "html": "" if payload.get("failed") else render_ai_markdown(row["content"]),
                            "evidence": payload.get("evidence", []),
                            "visuals": payload.get("visuals", [])})
        messages.append(message)
    return jsonify({"conversation": {"id": str(conversation["id"]), "title": conversation["title"]},
                    "messages": messages})


@app.delete("/api/ai/conversations/<uuid:conversation_id>")
def ai_conversation_delete(conversation_id):
    with pg() as conn:
        deleted = conn.execute("DELETE FROM ai_conversations WHERE id=%s AND user_id=%s RETURNING id",
                               (conversation_id, g.user["id"])).fetchone()
    return jsonify({"ok": True}) if deleted else (jsonify({"error": "Chat not found."}), 404)


@app.post("/api/ai/chat")
def ai_chat_route():
    if request.content_type != "application/json":
        return jsonify({"error": "Send a JSON chat request."}), 415
    values = request.get_json(silent=True) or {}
    if not isinstance(values, dict):
        return jsonify({"error": "Send a JSON chat request."}), 400
    question = values.get("question")
    if not isinstance(question, str) or not 1 <= len(question.strip()) <= 2000:
        return jsonify({"error": "Enter a question under 2,000 characters."}), 400
    try:
        conversation_id = uuid.UUID(str(values["conversation_id"])) if values.get("conversation_id") else uuid.uuid4()
    except (ValueError, TypeError, AttributeError):
        return jsonify({"error": "Invalid chat ID."}), 400
    message_saved = False
    try:
        with pg() as conn:
            settings = private_settings(conn)
            owner = conn.execute("SELECT user_id FROM ai_conversations WHERE id=%s FOR UPDATE",
                                 (conversation_id,)).fetchone()
            if owner and owner["user_id"] != g.user["id"]:
                return jsonify({"error": "Chat not found."}), 404
            if not owner:
                title = " ".join(question.split())[:80]
                conn.execute("INSERT INTO ai_conversations(id,user_id,title) VALUES (%s,%s,%s)",
                             (conversation_id, g.user["id"], title))
            prior = conn.execute("""SELECT role,content,payload FROM ai_chat_messages
                WHERE conversation_id=%s ORDER BY id DESC LIMIT 12""", (conversation_id,)).fetchall()
            history = []
            for item in reversed(prior):
                if item["role"] == "assistant" and (item["payload"] or {}).get("failed"):
                    if history and history[-1]["role"] == "user":
                        history.pop()
                else:
                    history.append({"role": item["role"], "content": item["content"]})
            conn.execute("INSERT INTO ai_chat_messages(conversation_id,role,content) VALUES (%s,'user',%s)",
                         (conversation_id, question.strip()))
            conn.execute("UPDATE ai_conversations SET updated_at=now() WHERE id=%s", (conversation_id,))
        message_saved = True
        result = answer_chat(question.strip(), history, settings, pg, ch, CH_LOCK)
        result["answer_html"] = render_ai_markdown(result["answer"])
        with pg() as conn:
            conn.execute("""INSERT INTO ai_chat_messages(conversation_id,role,content,payload)
                VALUES (%s,'assistant',%s,%s::jsonb)""",
                (conversation_id, result["answer"],
                 json.dumps({"evidence": result["evidence"], "visuals": result["visuals"]}, default=str)))
            conn.execute("UPDATE ai_conversations SET updated_at=now() WHERE id=%s", (conversation_id,))
        response = jsonify({**result, "conversation_id": str(conversation_id)})
        response.headers["Cache-Control"] = "no-store"
        return response
    except ChatError as error:
        if message_saved:
            with pg() as conn:
                conn.execute("""INSERT INTO ai_chat_messages(conversation_id,role,content,payload)
                    VALUES (%s,'assistant',%s,'{"failed":true}'::jsonb)""",
                    (conversation_id, f"I could not answer this question: {error}"))
        return jsonify({"error": str(error)}), 400
    except Exception:
        app.logger.exception("AI chat failed")
        if message_saved:
            with pg() as conn:
                conn.execute("""INSERT INTO ai_chat_messages(conversation_id,role,content,payload)
                    VALUES (%s,'assistant',%s,'{"failed":true}'::jsonb)""",
                    (conversation_id, "I could not answer this question. Please try again."))
        return jsonify({"error": "Chat could not complete. Try a narrower question."}), 500


@app.post("/api/ai/settings/test")
def ai_settings_test():
    try:
        with pg() as conn:
            url, model, key = private_settings(conn)
        action = parse_action(call_model(url, model, key, [
            {"role": "system", "content": "Return only a JSON object in the utility_chat_action format. This is a connection test with synthetic data; do not request a database query."},
            {"role": "user", "content": "Return {\"action\":\"final\",\"answer\":\"ready\",\"visuals\":[]}."},
        ], structured=True))
        if action["action"] != "final":
            raise ChatError("AI service responded, but did not return a final JSON answer.")
        return jsonify({"connected": True})
    except ChatError as error:
        return jsonify({"error": str(error)}), 400


def report_data(kind):
    current = datetime.now(REPORT_TIMEZONE)
    default = current.strftime("%Y-%m-%d" if kind == "daily" else "%Y-%m")
    period = period_bounds(kind, request.args.get("period", default))
    factory_param = request.args.get("factory_id", "").strip()
    if factory_param and (not factory_param.isdigit() or int(factory_param) < 1):
        raise ValueError("Factory must be a positive ID")
    factory_id = int(factory_param) if factory_param else None
    with pg() as conn:
        factory = conn.execute("SELECT id,name,code FROM factories WHERE id=%s", (factory_id,)).fetchone() if factory_id else None
        if factory_id and not factory:
            raise ValueError("Factory not found")
        meters = conn.execute("""SELECT s.id,s.name,s.type,s.unit,s.baseline,
            m.id AS machine_id,m.name AS machine_name,m.code AS machine_code,
            l.name AS location_name,f.name AS factory_name
            FROM sensors s JOIN machines m ON m.id=s.machine_id
            JOIN production_lines p ON p.id=m.production_line_id
            JOIN locations l ON l.id=p.location_id
            JOIN factories f ON f.id=l.factory_id
            WHERE s.meter_role='consumption' AND (%s::integer IS NULL OR f.id=%s)
            ORDER BY s.type,m.name,s.name""", (factory_id, factory_id)).fetchall()
    rows = []
    if meters:
        parameters = {
            "ids": [meter["id"] for meter in meters],
            "start": period["start_utc"].strftime("%Y-%m-%d %H:%M:%S.%f"),
            "end": period["end_utc"].strftime("%Y-%m-%d %H:%M:%S.%f"),
        }
        with CH_LOCK:
            rows = ch().query("""SELECT sensor_id,toStartOfHour(observed_at) AS hour,
                avg(value) AS mean_rate,count() AS samples,countIf(simulated) AS simulated_samples,
                min(observed_at) AS first_sample,max(observed_at) AS last_sample,
                countIf(topic={history_topic:String}) AS historical_samples
                FROM telemetry WHERE sensor_id IN {ids:Array(String)}
                AND observed_at >= toDateTime64({start:String},3,'UTC')
                AND observed_at < toDateTime64({end:String},3,'UTC')
                GROUP BY sensor_id,hour ORDER BY sensor_id,hour""",
                parameters={**parameters, "history_topic": HISTORY_TOPIC}).result_rows
    return build_report(period, meters, rows, SIM_INTERVAL, factory)


@app.get("/api/reports/<kind>")
def report_json(kind):
    try:
        return jsonify(report_data(kind))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400


@app.get("/api/reports/<kind>/pdf")
def report_pdf(kind):
    try:
        report = report_data(kind)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    code = report["factory"]["code"].lower().replace(" ", "-")
    filename = f"utility-{kind}-report-{code}-{report['period']}.pdf"
    response = send_file(BytesIO(make_report_pdf(report)), mimetype="application/pdf",
                         as_attachment=True, download_name=filename)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/category/<path:category>")
def category_detail(category):
    with pg() as conn:
        meters = conn.execute("""SELECT s.*, m.name AS machine_name, m.code AS machine_code,
            l.name AS location_name FROM sensors s JOIN machines m ON m.id=s.machine_id
            JOIN production_lines p ON p.id=m.production_line_id
            JOIN locations l ON l.id=p.location_id
            WHERE s.type=%s AND s.meter_role='consumption' ORDER BY m.name,s.name""",
            (category,)).fetchall()
        machine_rows = conn.execute("""SELECT m.id,m.name,m.code,m.enabled,l.name AS location_name
            FROM machines m JOIN production_lines p ON p.id=m.production_line_id
            JOIN locations l ON l.id=p.location_id
            WHERE EXISTS (SELECT 1 FROM sensors s WHERE s.machine_id=m.id
                AND s.type=%s AND s.meter_role='consumption')
            ORDER BY m.stage,m.lane,m.id""", (category,)).fetchall()
    if not meters:
        return jsonify({"error": "Metering category not found"}), 404
    ids = [meter["id"] for meter in meters]
    parameters = {"ids": ids}
    with CH_LOCK:
        latest_rows = ch().query("""SELECT sensor_id, argMax(value, observed_at), max(observed_at)
            FROM telemetry WHERE sensor_id IN {ids:Array(String)} GROUP BY sensor_id""",
            parameters=parameters).result_rows
        minute_rows = ch().query("""SELECT toStartOfMinute(observed_at) AS minute, sensor_id, avg(value)
            FROM telemetry WHERE sensor_id IN {ids:Array(String)}
            AND observed_at >= now() - INTERVAL 60 MINUTE
            GROUP BY minute, sensor_id ORDER BY minute""", parameters=parameters).result_rows
        event_rows = ch().query("""SELECT event_id, sensor_id, value, observed_at
            FROM telemetry WHERE sensor_id IN {ids:Array(String)}
            AND observed_at >= now() - INTERVAL 60 MINUTE
            ORDER BY observed_at DESC LIMIT 60""", parameters=parameters).result_rows
    latest = {sensor_id: (value, observed_at) for sensor_id, value, observed_at in latest_rows}
    now = datetime.now(timezone.utc)
    baseline_ids = [meter["id"] for meter in meters if meter["enabled"]]
    with pg() as conn:
        profiles = load_profiles(conn, baseline_ids, recent_minutes(now))
    machines = {machine["id"]: {"id": machine["id"], "name": machine["name"], "enabled": machine["enabled"],
                "code": machine["code"], "location_name": machine["location_name"], "meters": []}
                for machine in machine_rows}
    for meter in meters:
        reading = latest.get(meter["id"])
        age = (now - as_utc(reading[1])).total_seconds() if reading else None
        fresh = age is not None and 0 <= age <= SIM_INTERVAL * 3
        meter["reading"] = {"value": reading[0], "observed_at": as_utc(reading[1]).isoformat()} if fresh and meter["enabled"] else None
        meter["status"] = "live" if fresh and meter["enabled"] else "off" if not meter["enabled"] else "stale"
        profile = reading_profile(profiles, meter["id"], now)
        meter["historical_baseline"] = ({"expected": profile["expected"], "lower": profile["lower_bound"],
            "upper": profile["upper_bound"], "source": profile["source"]} if profile else None)
        machines[meter["machine_id"]]["meters"].append(meter)
    for machine in machines.values():
        machine["status"] = "live" if machine["enabled"] and any(meter["status"] == "live" for meter in machine["meters"]) else "off"
    minute_totals = {}
    for minute, sensor_id, average in minute_rows:
        key = as_utc(minute).isoformat()
        minute_totals[key] = minute_totals.get(key, 0) + average
    names = {meter["id"]: meter["machine_name"] for meter in meters}
    return jsonify({
        "category": category, "unit": meters[0]["unit"], "machines": list(machines.values()),
        "series": [{"minute": minute, "value": round(value, 2)} for minute, value in sorted(minute_totals.items())],
        "historical_baseline_series": expected_series(profiles, baseline_ids, now),
        "events": [{"id": str(event_id), "machine_name": names[sensor_id], "value": value,
                    "observed_at": as_utc(observed_at).isoformat()} for event_id, sensor_id, value, observed_at in event_rows],
        "updated_at": now.isoformat(),
    })


@app.get("/api/overview/trends")
def overview_trends():
    with pg() as conn:
        meters = conn.execute("SELECT id,type,enabled FROM sensors WHERE meter_role='consumption'").fetchall()
        now = datetime.now(timezone.utc)
        profiles = load_profiles(conn, [meter["id"] for meter in meters if meter["enabled"]], recent_minutes(now))
    types = {meter["id"]: meter["type"] for meter in meters}
    series = {category: {} for category in sorted(set(types.values()))}
    if types:
        with CH_LOCK:
            rows = ch().query("""SELECT toStartOfMinute(observed_at) AS minute, sensor_id, avg(value)
                FROM telemetry WHERE sensor_id IN {ids:Array(String)}
                AND observed_at >= now() - INTERVAL 60 MINUTE
                GROUP BY minute,sensor_id ORDER BY minute""",
                parameters={"ids": list(types)}).result_rows
        for minute, sensor_id, average in rows:
            category = types.get(sensor_id)
            if category is None:
                continue
            bucket = as_utc(minute).isoformat()
            series[category][bucket] = series[category].get(bucket, 0) + average
    return jsonify({
        "updated_at": now.isoformat(),
        "series": {category: [{"minute": minute, "value": round(value, 2)}
                             for minute, value in sorted(points.items())]
                   for category, points in series.items()},
        "historical_baseline_series": {category: expected_series(profiles,
            [meter["id"] for meter in meters if meter["type"] == category and meter["enabled"]], now)
            for category in series},
    })


@app.get("/api/chart-trends")
def chart_trends():
    """Bucketed observed rates and time-matched historical rates for longer views."""
    try:
        window = chart_window(request.args)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    category_filter = request.args.get("category", "").strip()
    with pg() as conn:
        meters = conn.execute("""SELECT id,type FROM sensors
            WHERE enabled AND meter_role='consumption'
              AND (%s='' OR type=%s) ORDER BY type,id""",
            (category_filter, category_filter)).fetchall()
        local_start = window["start"].astimezone(FACTORY_TIMEZONE)
        monday = local_start.date() - timedelta(days=local_start.weekday())
        profile_start = datetime.combine(monday, datetime.min.time(), FACTORY_TIMEZONE)
        profile_instants = [profile_start + timedelta(minutes=15 * index) for index in range(7 * 96)]
        profiles = load_profiles(conn, [meter["id"] for meter in meters], profile_instants)

    types = {meter["id"]: meter["type"] for meter in meters}
    categories = sorted(set(types.values()))
    observed = {name: {} for name in categories}
    if types:
        parameters = {"ids": list(types),
                      "start": window["start"].strftime("%Y-%m-%d %H:%M:%S.%f"),
                      "end": window["end"].strftime("%Y-%m-%d %H:%M:%S.%f"),
                      "start_epoch": int(window["start"].timestamp()),
                      "bucket_seconds": window["bucket_seconds"]}
        with CH_LOCK:
            rows = ch().query("""SELECT
                intDiv(toUnixTimestamp(observed_at)-{start_epoch:Int64},
                       {bucket_seconds:Int32}) AS bucket,
                sensor_id,avg(value) AS rate
                FROM telemetry WHERE sensor_id IN {ids:Array(String)}
                AND observed_at >= toDateTime64({start:String},3,'UTC')
                AND observed_at < toDateTime64({end:String},3,'UTC')
                GROUP BY bucket,sensor_id ORDER BY bucket""",
                parameters=parameters).result_rows
        for bucket, sensor_id, rate in rows:
            name = types[sensor_id]
            observed[name][bucket] = observed[name].get(bucket, 0) + rate

    return jsonify({
        "range": window["range"],
        "window_start": window["start"].isoformat(),
        "window_end": window["end"].isoformat(),
        "interval_seconds": window["bucket_seconds"],
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "series": {name: [{"minute": (window["start"] + timedelta(seconds=bucket * window["bucket_seconds"])).isoformat(),
                            "value": round(rate, 2)} for bucket, rate in sorted(observed[name].items())]
                   for name in categories},
        "historical_baseline_series": {name: historical_bucket_series(profiles,
            [meter["id"] for meter in meters if meter["type"] == name], window)
            for name in categories},
    })


@app.get("/api/machine/<int:machine_id>/trend")
def machine_trend(machine_id):
    category = request.args.get("category", "").strip()
    if not category:
        return jsonify({"error": "Metering category is required"}), 400
    with pg() as conn:
        machine = conn.execute("SELECT id FROM machines WHERE id=%s", (machine_id,)).fetchone()
        if not machine:
            return jsonify({"error": "Machine not found"}), 404
        meters = conn.execute("SELECT id,enabled FROM sensors WHERE machine_id=%s AND type=%s",
                              (machine_id, category)).fetchall()
        now = datetime.now(timezone.utc)
        baseline_ids = [meter["id"] for meter in meters if meter["enabled"]]
        profiles = load_profiles(conn, baseline_ids, recent_minutes(now))
    minute_totals = {}
    if meters:
        with CH_LOCK:
            rows = ch().query("""SELECT toStartOfMinute(observed_at) AS minute, sensor_id, avg(value)
                FROM telemetry WHERE sensor_id IN {ids:Array(String)}
                AND observed_at >= now() - INTERVAL 60 MINUTE
                GROUP BY minute, sensor_id ORDER BY minute""",
                parameters={"ids": [meter["id"] for meter in meters]}).result_rows
        for minute, _sensor_id, average in rows:
            key = as_utc(minute).isoformat()
            minute_totals[key] = minute_totals.get(key, 0) + average
    return jsonify({"machine_id": machine_id, "category": category,
                    "series": [{"minute": minute, "value": round(value, 2)}
                               for minute, value in sorted(minute_totals.items())],
                    "historical_baseline_series": expected_series(profiles, baseline_ids, now),
                    "updated_at": now.isoformat()})


@app.get("/api/machine/<int:machine_id>/metering-trends")
def machine_metering_trends(machine_id):
    with pg() as conn:
        machine = conn.execute("""SELECT m.id,m.name,m.code,m.enabled,l.name AS location_name
            FROM machines m JOIN production_lines p ON p.id=m.production_line_id
            JOIN locations l ON l.id=p.location_id WHERE m.id=%s""", (machine_id,)).fetchone()
        if not machine:
            return jsonify({"error": "Machine not found"}), 404
        meters = conn.execute("""SELECT id,name,type,unit,baseline,max_value,enabled
            FROM sensors WHERE machine_id=%s ORDER BY type,name,id""", (machine_id,)).fetchall()
    latest = {}
    history = {meter["id"]: [] for meter in meters}
    if meters:
        ids = [meter["id"] for meter in meters]
        with CH_LOCK:
            latest_rows = ch().query("""SELECT sensor_id,argMax(value,observed_at),max(observed_at)
                FROM telemetry WHERE sensor_id IN {ids:Array(String)} GROUP BY sensor_id""",
                parameters={"ids": ids}).result_rows
            minute_rows = ch().query("""SELECT toStartOfMinute(observed_at) AS minute,sensor_id,avg(value)
                FROM telemetry WHERE sensor_id IN {ids:Array(String)}
                AND observed_at >= now() - INTERVAL 60 MINUTE
                GROUP BY minute,sensor_id ORDER BY minute""",
                parameters={"ids": ids}).result_rows
        latest = {sensor_id: (value, as_utc(observed_at)) for sensor_id, value, observed_at in latest_rows}
        for minute, sensor_id, average in minute_rows:
            if sensor_id in history:
                history[sensor_id].append({"minute": as_utc(minute).isoformat(), "value": round(average, 2)})
    now = datetime.now(timezone.utc)
    with pg() as conn:
        profiles = load_profiles(conn, [meter["id"] for meter in meters], recent_minutes(now))
    for meter in meters:
        last = latest.get(meter["id"])
        age = (now - last[1]).total_seconds() if last else None
        fresh = age is not None and 0 <= age <= SIM_INTERVAL * 3
        meter["reading"] = {"value": last[0], "observed_at": last[1].isoformat()} if fresh and meter["enabled"] else None
        meter["last_seen_at"] = last[1].isoformat() if last else None
        meter["status"] = "live" if fresh and meter["enabled"] else "off" if not meter["enabled"] else "stale"
        meter["series"] = history[meter["id"]]
        meter["historical_baseline_series"] = expected_series(profiles, [meter["id"]], now)
        profile = reading_profile(profiles, meter["id"], now)
        meter["historical_baseline"] = ({"expected": profile["expected"], "lower": profile["lower_bound"],
            "upper": profile["upper_bound"], "source": profile["source"]} if profile else None)
    machine["status"] = "live" if machine["enabled"] and any(meter["status"] == "live" for meter in meters) else "off"
    return jsonify({"machine": machine, "meters": meters, "updated_at": now.isoformat()})


@app.get("/api/simulator-catalog")
def simulator_catalog():
    with pg() as conn:
        rows = conn.execute("""SELECT s.id, s.name, s.type, s.unit, s.baseline, s.enabled,
            s.min_value, s.max_value, m.name AS machine_name
            FROM sensors s JOIN machines m ON m.id=s.machine_id ORDER BY s.id""").fetchall()
    return jsonify({"sensors": rows})


@app.get("/api/health")
def health():
    with pg() as conn:
        conn.execute("SELECT 1")
    with CH_LOCK:
        ch().query("SELECT 1")
    return jsonify({"postgres": "ok", "clickhouse": "ok", "mqtt": INGEST_STATUS})


@app.route("/api/master/<table>", methods=["POST", "PUT", "DELETE"])
def mutate(table):
    if table not in TABLES:
        return jsonify({"error": "Unknown master-data table"}), 404
    body = request.get_json(silent=True) or {}
    try:
        with pg() as conn:
            if request.method == "DELETE":
                identity = str(body.get("id", ""))
                if not identity:
                    raise ValueError("id is required")
                result = conn.execute(f"DELETE FROM {table} WHERE id=%s", (identity,))
                if not result.rowcount:
                    return jsonify({"error": "Record not found"}), 404
                return jsonify({"ok": True})
            creating = request.method == "POST"
            values = validated(table, body, creating)
            if creating:
                names = list(values)
                row = conn.execute(f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join(['%s'] * len(names))}) RETURNING id",
                                   [values[name] for name in names]).fetchone()
            else:
                identity = str(body.get("id", ""))
                if not identity:
                    raise ValueError("id is required")
                row = conn.execute(f"UPDATE {table} SET {','.join(name + '=%s' for name in values)} WHERE id=%s RETURNING id",
                                   [*values.values(), identity]).fetchone()
                if not row:
                    return jsonify({"error": "Record not found"}), 404
            if table == "machines":
                links = conn.execute("""SELECT left_machine_id,right_machine_id FROM machine_flow_links
                    WHERE left_machine_id=%s OR right_machine_id=%s""", (row["id"], row["id"])).fetchall()
                current_left = [link["left_machine_id"] for link in links if link["right_machine_id"] == row["id"]]
                current_right = [link["right_machine_id"] for link in links if link["left_machine_id"] == row["id"]]
                set_machine_flow(conn, row["id"], body.get("left_machine_ids", current_left),
                                 body.get("right_machine_ids", current_right))
        return jsonify({"ok": True, "id": row["id"]}), 201 if creating else 200
    except ForeignKeyViolation:
        return jsonify({"error": "Record is referenced by another item or parent does not exist"}), 409
    except UniqueViolation:
        return jsonify({"error": "Code or ID already exists"}), 409
    except (ValueError, TypeError, psycopg.errors.CheckViolation) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/simulator")
def simulator():
    if os.getenv("SIMULATOR_MODE") == "external":
        return jsonify({"error": "Use the Metering Simulator at http://127.0.0.1:8766/ to control publishing"}), 409
    running = (request.get_json(silent=True) or {}).get("running")
    if not isinstance(running, bool):
        return jsonify({"error": "running must be true or false"}), 400
    with pg() as conn:
        conn.execute("UPDATE app_settings SET value=%s WHERE key='simulator_running'", ("true" if running else "false",))
    return jsonify({"ok": True, "running": running})


def on_connect(client, userdata, flags, reason_code, properties):
    INGEST_STATUS["connected"] = reason_code == 0
    INGEST_STATUS["error"] = None if reason_code == 0 else f"MQTT connection code {reason_code}"
    if reason_code == 0:
        client.subscribe(MQTT_TOPIC, qos=1)


def on_disconnect(client, userdata, flags, reason_code, properties):
    INGEST_STATUS["connected"] = False


def record_metering_alerts(conn, sensor, value, observed, received):
    """Keep independent baseline and maximum episodes per enabled meter."""
    if not sensor["enabled"]:
        return
    for alert_type, threshold in (("baseline", sensor["baseline"]), ("maximum", sensor["max_value"])):
        if value > threshold:
            conn.execute("""INSERT INTO metering_alerts
                (sensor_id,sensor_name,machine_name,location_name,utility_type,unit,
                 baseline,alert_type,threshold_value,first_value,latest_value,peak_value,
                 started_at,last_seen_at,last_observed_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (sensor_id,alert_type) WHERE resolved_at IS NULL DO UPDATE SET
                 latest_value=EXCLUDED.latest_value,
                 peak_value=GREATEST(metering_alerts.peak_value,EXCLUDED.latest_value),
                 baseline=EXCLUDED.baseline,
                 threshold_value=EXCLUDED.threshold_value,
                 exceedance_count=metering_alerts.exceedance_count+1,
                 last_seen_at=EXCLUDED.last_seen_at,
                 last_observed_at=EXCLUDED.last_observed_at""",
                (sensor["id"], sensor["name"], sensor["machine_name"], sensor["location_name"],
                 sensor["type"], sensor["unit"], sensor["baseline"], alert_type, threshold,
                 value, value, value, received, received, observed))
        else:
            conn.execute("""UPDATE metering_alerts SET resolved_at=%s,last_seen_at=%s,
                last_observed_at=%s,latest_value=%s,baseline=%s,threshold_value=%s
                WHERE sensor_id=%s AND alert_type=%s AND resolved_at IS NULL""",
                (received, received, observed, value, sensor["baseline"], threshold,
                 sensor["id"], alert_type))


def record_historical_deviation(conn, sensor, value, observed, received, profile):
    """Compare a fresh reading to the empirical band for its local weekly slot."""
    if not sensor["enabled"]:
        return
    for alert_type, threshold, deviates in (
        ("historical_high", profile["upper_bound"] if profile else 0,
         bool(profile and value > profile["upper_bound"])),
        ("historical_low", profile["lower_bound"] if profile else 0,
         bool(profile and profile["lower_bound"] > 0 and value < profile["lower_bound"])),
    ):
        if deviates:
            extreme = "LEAST" if alert_type == "historical_low" else "GREATEST"
            conn.execute(f"""INSERT INTO metering_alerts
                (sensor_id,sensor_name,machine_name,location_name,utility_type,unit,
                 baseline,alert_type,threshold_value,first_value,latest_value,peak_value,
                 started_at,last_seen_at,last_observed_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (sensor_id,alert_type) WHERE resolved_at IS NULL DO UPDATE SET
                 latest_value=EXCLUDED.latest_value,
                 peak_value={extreme}(metering_alerts.peak_value,EXCLUDED.latest_value),
                 baseline=EXCLUDED.baseline,
                 threshold_value=EXCLUDED.threshold_value,
                 exceedance_count=metering_alerts.exceedance_count+1,
                 last_seen_at=EXCLUDED.last_seen_at,
                 last_observed_at=EXCLUDED.last_observed_at""",
                (sensor["id"], sensor["name"], sensor["machine_name"], sensor["location_name"],
                 sensor["type"], sensor["unit"], profile["expected"], alert_type, threshold,
                 value, value, value, received, received, observed))
        else:
            conn.execute("""UPDATE metering_alerts SET resolved_at=%s,last_seen_at=%s,
                last_observed_at=%s,latest_value=%s WHERE sensor_id=%s
                AND alert_type=%s AND resolved_at IS NULL""",
                (received, received, observed, value, sensor["id"], alert_type))


def update_condition_state(conn, sensor, value, received):
    """Track uninterrupted local receipt time; a missing stream cannot prove a 1-minute condition."""
    previous = conn.execute("SELECT * FROM metering_condition_state WHERE sensor_id=%s FOR UPDATE",
                            (sensor["id"],)).fetchone()
    continuous = bool(previous and previous["machine_id"] == sensor["machine_id"]
                      and timedelta(0) <= received - previous["last_seen_at"]
                      <= timedelta(seconds=max(15, SIM_INTERVAL * 3)))
    zero_since = ((previous["zero_since"] or received) if continuous and previous["last_value"] == 0
                  else received) if value == 0 else None
    maximum_since = ((previous["maximum_since"] or received) if continuous and previous["last_value"] > sensor["max_value"]
                     else received) if value > sensor["max_value"] else None
    conn.execute("""INSERT INTO metering_condition_state
        (sensor_id,machine_id,last_value,last_seen_at,zero_since,maximum_since)
        VALUES (%s,%s,%s,%s,%s,%s)
        ON CONFLICT (sensor_id) DO UPDATE SET machine_id=EXCLUDED.machine_id,
          last_value=EXCLUDED.last_value,last_seen_at=EXCLUDED.last_seen_at,
          zero_since=EXCLUDED.zero_since,maximum_since=EXCLUDED.maximum_since""",
        (sensor["id"], sensor["machine_id"], value, received, zero_since, maximum_since))
    return {"zero_since": zero_since, "maximum_since": maximum_since}


def record_prolonged_maximum(conn, sensor, value, observed, received, maximum_since):
    threshold = sensor["max_value"]
    prolonged = bool(maximum_since and received - maximum_since >= timedelta(minutes=1))
    if prolonged:
        conn.execute("""INSERT INTO metering_alerts
            (sensor_id,sensor_name,machine_name,location_name,utility_type,unit,baseline,
             alert_type,threshold_value,first_value,latest_value,peak_value,
             started_at,last_seen_at,last_observed_at,machine_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,'prolonged_maximum',%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (sensor_id,alert_type) WHERE resolved_at IS NULL DO UPDATE SET
              latest_value=EXCLUDED.latest_value,
              peak_value=GREATEST(metering_alerts.peak_value,EXCLUDED.latest_value),
              exceedance_count=metering_alerts.exceedance_count+1,
              last_seen_at=EXCLUDED.last_seen_at,last_observed_at=EXCLUDED.last_observed_at""",
            (sensor["id"], sensor["name"], sensor["machine_name"], sensor["location_name"],
             sensor["type"], sensor["unit"], sensor["baseline"], threshold, value, value, value,
             maximum_since, received, observed, sensor["machine_id"]))
    else:
        conn.execute("""UPDATE metering_alerts SET resolved_at=%s,last_seen_at=%s,
            last_observed_at=%s,latest_value=%s WHERE sensor_id=%s
            AND alert_type='prolonged_maximum' AND resolved_at IS NULL""",
            (received, received, observed, value, sensor["id"]))


def update_machine_condition_alert(conn, machine, alert_type, active, received, meter_names=""):
    key = f"machine:{machine['id']}"
    if active:
        conn.execute("""INSERT INTO metering_alerts
            (sensor_id,sensor_name,machine_name,location_name,utility_type,unit,baseline,
             alert_type,threshold_value,first_value,latest_value,peak_value,
             started_at,last_seen_at,last_observed_at,machine_id)
            VALUES (%s,%s,%s,%s,'Machine','',0,%s,0,0,0,0,%s,%s,%s,%s)
            ON CONFLICT (sensor_id,alert_type) WHERE resolved_at IS NULL DO UPDATE SET
              last_seen_at=EXCLUDED.last_seen_at,last_observed_at=EXCLUDED.last_observed_at,
              sensor_name=EXCLUDED.sensor_name,machine_name=EXCLUDED.machine_name""",
            (key, meter_names, machine["name"], machine["location_name"], alert_type,
             received, received, received, machine["id"]))
    else:
        conn.execute("""UPDATE metering_alerts SET resolved_at=%s,last_seen_at=%s
            WHERE sensor_id=%s AND alert_type=%s AND resolved_at IS NULL""",
            (received, received, key, alert_type))


def machine_condition_flags(rows, received):
    fresh = [row for row in rows if row["last_seen_at"] is not None
             and timedelta(0) <= received - row["last_seen_at"]
             <= timedelta(seconds=max(15, SIM_INTERVAL * 3))]
    all_fresh = bool(rows) and len(fresh) == len(rows)
    long_zero = [row for row in fresh if row["last_value"] == 0 and row["zero_since"]
                 and received - row["zero_since"] >= timedelta(minutes=1)]
    all_zero = all_fresh and len(long_zero) == len(rows)
    partial_zero = (all_fresh and len(rows) > 1 and bool(long_zero)
                    and any(row["last_value"] > 0 for row in fresh))
    positive = any(row["last_value"] > 0 for row in fresh)
    return all_zero, partial_zero, positive, fresh, long_zero


def evaluate_machine_conditions(conn, machine, received):
    rows = conn.execute("""SELECT s.id,s.name,c.last_value,c.last_seen_at,c.zero_since
        FROM sensors s LEFT JOIN metering_condition_state c ON c.sensor_id=s.id
        WHERE s.machine_id=%s AND s.enabled ORDER BY s.id""", (machine["id"],)).fetchall()
    all_zero, partial_zero, positive, fresh, long_zero = machine_condition_flags(rows, received)
    update_machine_condition_alert(conn, machine, "machine_off_question",
                                   machine["enabled"] and all_zero, received,
                                   ", ".join(row["name"] for row in rows))
    update_machine_condition_alert(conn, machine, "metering_check", partial_zero, received,
                                   ", ".join(row["name"] for row in long_zero))
    update_machine_condition_alert(conn, machine, "machine_on_question",
                                   not machine["enabled"] and positive, received,
                                   ", ".join(row["name"] for row in fresh if row["last_value"] > 0))


def on_message(client, userdata, message):
    try:
        payload = json.loads(message.payload)
        readings = payload.get("readings") if isinstance(payload, dict) else None
        readings = readings if isinstance(readings, list) else [payload]
        if not 1 <= len(readings) <= 500:
            raise ValueError("Invalid metering batch size")
        sensor_ids = [str(item["sensor_id"]) for item in readings]
        with pg() as conn:
            sensors = conn.execute("""SELECT s.id,s.machine_id,s.name,s.type,s.unit,s.baseline,s.max_value,s.enabled,
                m.name AS machine_name,l.name AS location_name
                FROM sensors s JOIN machines m ON m.id=s.machine_id
                JOIN production_lines p ON p.id=m.production_line_id
                JOIN locations l ON l.id=p.location_id
                WHERE s.id = ANY(%s)""", (sensor_ids,)).fetchall()
        catalog = {sensor["id"]: sensor for sensor in sensors}
        received = datetime.now(timezone.utc)
        valid = []
        for item in readings:
            sensor_id = str(item["sensor_id"])
            sensor = catalog.get(sensor_id)
            if not sensor:
                continue
            value = float(item["value"])
            if not math.isfinite(value):
                raise ValueError("Metering reading must be finite")
            observed = datetime.fromisoformat(str(item["timestamp"]).replace("Z", "+00:00"))
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=timezone.utc)
            valid.append((sensor, value, observed, bool(item.get("simulated", False))))
        if not valid:
            return
        with CH_LOCK:
            ch().insert("telemetry", [[str(uuid.uuid4()), sensor["id"], value, sensor["unit"], observed,
                                       received, message.topic, simulated]
                                      for sensor, value, observed, simulated in valid],
                        column_names=["event_id", "sensor_id", "value", "unit", "observed_at", "received_at", "topic", "simulated"])
        with pg() as conn:
            for sensor, value, observed, _ in valid:
                machine = conn.execute("""SELECT m.id,m.name,m.enabled,l.name AS location_name
                    FROM machines m JOIN production_lines p ON p.id=m.production_line_id
                    JOIN locations l ON l.id=p.location_id WHERE m.id=%s FOR UPDATE OF m""",
                    (sensor["machine_id"],)).fetchone()
                if sensor["enabled"] and machine:
                    record_metering_alerts(conn, sensor, value, observed, received)
                    if timedelta(0) <= received - observed <= timedelta(minutes=2):
                        profiles = load_profiles(conn, [sensor["id"]], [observed])
                        record_historical_deviation(conn, sensor, value, observed, received,
                                                    reading_profile(profiles, sensor["id"], observed))
                    state = update_condition_state(conn, sensor, value, received)
                    record_prolonged_maximum(conn, sensor, value, observed, received, state["maximum_since"])
                    evaluate_machine_conditions(conn, machine, received)
        INGEST_STATUS["last_message"] = received.isoformat()
        INGEST_STATUS["error"] = None
    except Exception as exc:
        INGEST_STATUS["error"] = str(exc)


def start_mqtt():
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"dashboard-ingest-{uuid.uuid4().hex[:8]}")
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    if os.getenv("MQTT_USERNAME"):
        client.username_pw_set(os.getenv("MQTT_USERNAME"), os.getenv("MQTT_PASSWORD"))
    if os.getenv("MQTT_TLS", "false").lower() == "true":
        client.tls_set(ca_certs=os.getenv("MQTT_CA_CERT") or None)
    client.connect_async(MQTT_HOST, MQTT_PORT, 30)
    client.loop_start()
    return client


def simulator_loop(client):
    while True:
        try:
            with pg() as conn:
                running = conn.execute("SELECT value FROM app_settings WHERE key='simulator_running'").fetchone()["value"] == "true"
                sensors = conn.execute("SELECT id,type,unit,baseline,min_value,max_value FROM sensors WHERE enabled ORDER BY id").fetchall()
            if running and client.is_connected():
                stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
                for sensor in sensors:
                    value = round(max(sensor["min_value"], min(sensor["max_value"],
                                      random.gauss(sensor["baseline"] * .7, sensor["baseline"] * .06))), 2)
                    topic = f"factory/wheel-rim/{sensor['type'].lower().replace(' ', '-')}/{sensor['id']}"
                    client.publish(topic, json.dumps({"sensor_id": sensor["id"], "type": sensor["type"],
                                                     "value": value, "unit": sensor["unit"], "timestamp": stamp,
                                                     "simulated": True}), qos=1)
        except Exception as exc:
            INGEST_STATUS["error"] = f"Simulator: {exc}"
        time.sleep(SIM_INTERVAL)


if __name__ == "__main__":
    for attempt in range(30):
        try:
            init_storage()
            break
        except Exception as exc:
            if attempt == 29:
                raise
            print(f"Waiting for databases: {exc}", flush=True)
            time.sleep(2)
    mqtt_client = start_mqtt()
    if os.getenv("SIMULATOR_MODE") != "external":
        threading.Thread(target=simulator_loop, args=(mqtt_client,), daemon=True).start()
    app.run(host="0.0.0.0", port=8765, threaded=True)
