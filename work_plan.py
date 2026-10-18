"""Dated machine work plans and advisory metering checks."""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo
import re

LOCAL = ZoneInfo("Asia/Jakarta")


def normalize_plan(payload):
    if not isinstance(payload, dict):
        raise ValueError("Send a work plan.")
    try:
        day = date.fromisoformat(str(payload.get("date", "")))
        start = time.fromisoformat(str(payload.get("start_time", "")))
    except ValueError:
        raise ValueError("Enter a valid date and start time.") from None
    if not re.fullmatch(r"\d{2}:\d{2}", str(payload.get("start_time", ""))):
        raise ValueError("Start time must be HH:MM.")
    try:
        hours = Decimal(str(payload.get("duration_hours", 8)))
    except (InvalidOperation, ValueError):
        raise ValueError("Enter a valid duration.") from None
    if not hours.is_finite() or not Decimal("0.25") <= hours <= Decimal("24") or hours * 4 != (hours * 4).to_integral_value():
        raise ValueError("Duration must be 0.25–24 hours in 15-minute steps.")
    pic = str(payload.get("pic") or "").strip()
    if not 1 <= len(pic) <= 120:
        raise ValueError("Enter the responsible PIC (up to 120 characters).")
    machines = payload.get("machines")
    if not isinstance(machines, list) or not machines:
        raise ValueError("Select at least one machine.")
    selected = []
    seen = set()
    for item in machines:
        if not isinstance(item, dict) or type(item.get("machine_id")) is not int or item["machine_id"] < 1 or type(item.get("planned_on")) is not bool:
            raise ValueError("Each selected machine needs a valid planned ON/OFF state.")
        if item["machine_id"] in seen:
            raise ValueError("A machine may be selected only once.")
        seen.add(item["machine_id"])
        selected.append({"machine_id": item["machine_id"], "planned_on": item["planned_on"]})
    start_at = datetime.combine(day, start, LOCAL).astimezone(timezone.utc)
    return {"start_at": start_at, "end_at": start_at + timedelta(hours=float(hours)),
            "duration_minutes": int(hours * 60), "pic": pic, "machines": selected}


def _serialize(row, machines=None):
    local = row["start_at"].astimezone(LOCAL)
    end = row["start_at"] + timedelta(minutes=row["duration_minutes"])
    now = datetime.now(timezone.utc)
    result = {"id": row["id"], "date": local.date().isoformat(), "start_time": local.strftime("%H:%M"),
              "duration_hours": row["duration_minutes"] / 60, "pic": row["pic"],
              "start_at": row["start_at"].isoformat(), "end_at": end.isoformat(),
              "status": "upcoming" if now < row["start_at"] else "active" if now < end else "completed"}
    if machines is not None:
        result["machines"] = machines
        result["machine_count"] = len(machines)
        result["planned_on_count"] = sum(m["planned_on"] for m in machines)
        result["planned_off_count"] = result["machine_count"] - result["planned_on_count"]
    return result


def list_plans(conn):
    rows = conn.execute("SELECT id,start_at,duration_minutes,pic FROM work_plans ORDER BY start_at DESC,id DESC LIMIT 200").fetchall()
    result = []
    for row in rows:
        machines = conn.execute("""SELECT machine_id,planned_on FROM work_plan_machines WHERE plan_id=%s""", (row["id"],)).fetchall()
        result.append(_serialize(row, machines))
    return result


def read_plan(conn, plan_id):
    row = conn.execute("SELECT id,start_at,duration_minutes,pic FROM work_plans WHERE id=%s", (plan_id,)).fetchone()
    if not row:
        return None
    machines = conn.execute("""SELECT w.machine_id,w.planned_on,m.name AS machine_name,m.code AS machine_code,
        l.name AS location_name FROM work_plan_machines w JOIN machines m ON m.id=w.machine_id
        JOIN production_lines p ON p.id=m.production_line_id JOIN locations l ON l.id=p.location_id
        WHERE w.plan_id=%s ORDER BY l.name,m.name""", (plan_id,)).fetchall()
    return _serialize(row, [dict(item) for item in machines])


def save_plan(conn, payload, plan_id=None):
    plan = normalize_plan(payload)
    ids = [m["machine_id"] for m in plan["machines"]]
    rows = conn.execute("SELECT id FROM machines WHERE id=ANY(%s) FOR SHARE", (ids,)).fetchall()
    if {r["id"] for r in rows} != set(ids):
        raise ValueError("One or more selected machines no longer exist.")
    # Serialize plan writes so concurrent requests cannot create overlapping reservations.
    conn.execute("SELECT pg_advisory_xact_lock(728451)")
    overlap = conn.execute("""SELECT p.id,m.name FROM work_plans p JOIN work_plan_machines w ON w.plan_id=p.id
        JOIN machines m ON m.id=w.machine_id WHERE w.machine_id=ANY(%s) AND p.id<>%s
        AND p.start_at<%s AND p.start_at + p.duration_minutes * interval '1 minute'>%s LIMIT 1""",
        (ids, plan_id or 0, plan["end_at"], plan["start_at"])).fetchone()
    if overlap:
        raise ValueError(f"{overlap['name']} already belongs to an overlapping work plan #{overlap['id']}.")
    if plan_id is None:
        plan_id = conn.execute("""INSERT INTO work_plans(start_at,duration_minutes,pic)
            VALUES(%s,%s,%s) RETURNING id""", (plan["start_at"], plan["duration_minutes"], plan["pic"])).fetchone()["id"]
    else:
        updated = conn.execute("""UPDATE work_plans SET start_at=%s,duration_minutes=%s,pic=%s,updated_at=now()
            WHERE id=%s RETURNING id""", (plan["start_at"], plan["duration_minutes"], plan["pic"], plan_id)).fetchone()
        if not updated:
            return None
        conn.execute("DELETE FROM work_plan_machines WHERE plan_id=%s", (plan_id,))
    for machine in plan["machines"]:
        conn.execute("INSERT INTO work_plan_machines(plan_id,machine_id,planned_on) VALUES(%s,%s,%s)",
                     (plan_id, machine["machine_id"], machine["planned_on"]))
    return read_plan(conn, plan_id)


def active_off_machines(conn):
    return conn.execute("""SELECT p.id AS plan_id,p.pic,p.start_at,p.duration_minutes,w.machine_id,m.name AS machine_name
        FROM work_plans p JOIN work_plan_machines w ON w.plan_id=p.id
        JOIN machines m ON m.id=w.machine_id WHERE w.planned_on=false
        AND p.start_at<=now() AND p.start_at + p.duration_minutes * interval '1 minute'>now()""").fetchall()
