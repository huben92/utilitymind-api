"""Seed a reproducible, clearly marked 3-month wheel-factory demonstration history.

Run once inside the dashboard container: python api/seed_historical_data.py
Only machines from the documented Wheelworks pilot and the utility systems
created here are modeled. User-added machines are deliberately left alone.
"""

from calendar import monthrange
from datetime import datetime, timedelta, timezone
import math
import sys
import uuid
import zlib

from historical_baseline import HISTORY_TOPIC, profile_bounds
from operations_app import CH_LOCK, FLOW, ch, init_storage, pg


RUN_ID = "wheelworks-synthetic-history-v1"
CADENCE_MINUTES = 5
TIMEZONE_NAME = "Asia/Jakarta"

# Typical process *relationships*, not calibrated values from the actual plant.
# Rates are illustrative, in the category's displayed unit per hour.
PROCESS_SPECS = {
    "Warehouse": {"Electricity": ("kW", 20), "Clean Water": ("m³/h", 1.2),
                  "Compressed Air": ("m³/h", 4), "Diesel": ("L/h", 3)},
    "Charging": {"Electricity": ("kW", 35), "Clean Water": ("m³/h", 0.8),
                 "Compressed Air": ("m³/h", 7), "Diesel": ("L/h", 5),
                 "Natural Gas": ("m³/h", 8)},
    "Melting": {"Natural Gas": ("m³/h", 80), "Electricity": ("kW", 55)},
    "Degassing": {"Electricity": ("kW", 18), "Compressed Air": ("m³/h", 4)},
    "Casting": {"Electricity": ("kW", 45), "Compressed Air": ("m³/h", 12),
                "Chilled Water": ("m³/h", 9)},
    "Heat treatment": {"Natural Gas": ("m³/h", 65), "Electricity": ("kW", 70),
                       "Clean Water": ("m³/h", 8)},
    "Machining": {"Electricity": ("kW", 40), "Compressed Air": ("m³/h", 10)},
    "Painting": {"Electricity": ("kW", 65), "Natural Gas": ("m³/h", 50),
                 "Clean Water": ("m³/h", 7), "Compressed Air": ("m³/h", 15),
                 "Steam": ("kg/h", 100)},
}

# The role prevents double-counting output from a utility system and its users.
UTILITY_SYSTEMS = {
    "AIR-COMP-01": ("Air Compressor 01", "Compressed air system", [
        ("Electricity", "kW", 110, "consumption"),
        ("Compressed Air", "m³/h", 200, "supply")]),
    "CHILLER-01": ("Chiller 01", "Chilled water system", [
        ("Electricity", "kW", 110, "consumption"),
        ("Chilled Water", "m³/h", 55, "supply")]),
    "BOILER-01": ("Boiler 01", "Steam system", [
        ("Natural Gas", "m³/h", 90, "consumption"),
        ("Steam", "kg/h", 240, "supply")]),
    "WATER-PUMP-01": ("Process Water Pump 01", "Water system", [
        ("Electricity", "kW", 25, "consumption"),
        ("Clean Water", "m³/h", 35, "supply")]),
    "GEN-01": ("Backup Generator 01", "Emergency power", [
        ("Diesel", "L/h", 30, "consumption"),
        ("Electricity", "kW", 130, "supply")]),
}


def previous_three_months(instant):
    total = instant.year * 12 + instant.month - 1 - 3
    year, zero_month = divmod(total, 12)
    month = zero_month + 1
    return instant.replace(year=year, month=month,
                           day=min(instant.day, monthrange(year, month)[1]))


def stable_noise(key, spread=0.06):
    fraction = zlib.crc32(key.encode()) / 0xffffffff
    return 1 + (fraction * 2 - 1) * spread


def work_fraction(local, stage):
    hour = local.hour + local.minute / 60
    if stage == "Emergency power":
        return 1.0 if local.weekday() == 0 and 8 <= hour < 8.5 else 0.0
    if stage == "Painting":
        start, end = 7, 20
    else:
        start, end = 6, 22
    if local.weekday() == 5:
        end = min(end, 14)
    if local.weekday() == 6 or hour < start or hour >= end:
        return 0.0
    ramp = 0.65 if hour < start + 1 or hour >= end - 1 else 1.0
    lunch = 0.87 if 12 <= hour < 13 else 1.0
    saturday = 0.75 if local.weekday() == 5 else 1.0
    return ramp * lunch * saturday


def normal_rate(meter, instant):
    from historical_baseline import FACTORY_TIMEZONE

    local = instant.astimezone(FACTORY_TIMEZONE)
    stage, category = meter["stage"], meter["type"]
    work = work_fraction(local, stage)
    idle = {"Melting": 0.38, "Heat treatment": 0.30,
            "Compressed air system": 0.18, "Chilled water system": 0.23,
            "Steam system": 0.20, "Water system": 0.15}.get(stage, 0.04)
    if category in ("Clean Water", "Diesel", "Compressed Air", "Steam") and stage not in (
            "Compressed air system", "Water system", "Steam system"):
        idle = 0.0
    if stage == "Emergency power":
        idle = 0.0
    daily = stable_noise(f"{meter['machine_code']}:{local.date()}", 0.11)
    cycle = 1 + 0.07 * math.sin((local.hour * 60 + local.minute) / 19 + meter["machine_id"])
    jitter = stable_noise(f"{meter['id']}:{instant.isoformat()}", 0.055)
    value = meter["nominal"] * (idle + (1 - idle) * work) * daily * cycle * jitter
    return round(min(max(0, value), meter["max_value"] * 0.94), 3)


def ensure_catalog(conn):
    factory = conn.execute("SELECT id FROM factories WHERE code='WW-01'").fetchone()
    if not factory:
        raise RuntimeError("Wheelworks pilot factory was not found")
    process_codes = {code for _, code, _, _, _ in FLOW}
    process = conn.execute("""SELECT m.id,m.code,m.name,m.category FROM machines m
        JOIN production_lines p ON p.id=m.production_line_id
        JOIN locations l ON l.id=p.location_id
        WHERE l.factory_id=%s AND p.code='LINE-A'""", (factory["id"],)).fetchall()
    if not process_codes.issubset({machine["code"] for machine in process}):
        raise RuntimeError("The seeded wheel production flow is incomplete")

    conn.execute("""INSERT INTO locations(factory_id,name,code,description)
        VALUES (%s,'Utility Services','UTIL','Pilot support systems; not a production step')
        ON CONFLICT(factory_id,code) DO NOTHING""", (factory["id"],))
    location_id = conn.execute("SELECT id FROM locations WHERE factory_id=%s AND code='UTIL'",
                               (factory["id"],)).fetchone()["id"]
    conn.execute("""INSERT INTO production_lines(location_id,name,code,description)
        VALUES (%s,'Utility Systems','UTIL-SYS','Generation and distribution assets')
        ON CONFLICT(location_id,code) DO NOTHING""", (location_id,))
    line_id = conn.execute("SELECT id FROM production_lines WHERE location_id=%s AND code='UTIL-SYS'",
                           (location_id,)).fetchone()["id"]
    for code, (name, category, _) in UTILITY_SYSTEMS.items():
        conn.execute("""INSERT INTO machines(production_line_id,name,code,category,stage,lane,description)
            VALUES (%s,%s,%s,%s,1,1,'Synthetic pilot support system')
            ON CONFLICT(production_line_id,code) DO NOTHING""", (line_id, name, code, category))
    utilities = conn.execute("SELECT id,code,name,category FROM machines WHERE production_line_id=%s",
                             (line_id,)).fetchall()

    modeled = [machine for machine in process if machine["code"] in process_codes] + [
        machine for machine in utilities if machine["code"] in UTILITY_SYSTEMS]
    for machine in modeled:
        if machine["code"] in UTILITY_SYSTEMS:
            specs = UTILITY_SYSTEMS[machine["code"]][2]
        else:
            specs = [(category, unit, nominal, "consumption") for category, (unit, nominal)
                     in PROCESS_SPECS[machine["category"]].items()]
        for category, unit, nominal, role in specs:
            existing = conn.execute("SELECT id FROM sensors WHERE machine_id=%s AND type=%s AND meter_role=%s",
                                    (machine["id"], category, role)).fetchone()
            if existing:
                continue
            code = category.lower().replace(" ", "-")
            sensor_id = f"demo-{machine['code'].lower()}-{code}-{role}"
            conn.execute("""INSERT INTO sensors
                (id,machine_id,name,type,unit,baseline,meter_role,enabled,min_value,max_value)
                VALUES (%s,%s,%s,%s,%s,%s,%s,true,0,%s) ON CONFLICT(id) DO NOTHING""",
                (sensor_id, machine["id"], f"{category} · {machine['name']}", category, unit,
                 nominal, role, round(nominal * 1.8, 2)))

    rows = conn.execute("""SELECT s.id,s.machine_id,s.name,s.type,s.unit,s.baseline,s.max_value,s.meter_role,
            m.code AS machine_code,m.category AS stage
        FROM sensors s JOIN machines m ON m.id=s.machine_id
        WHERE m.id=ANY(%s) ORDER BY s.id""", ([machine["id"] for machine in modeled],)).fetchall()
    for meter in rows:
        if meter["machine_code"] in UTILITY_SYSTEMS:
            specification = next((nominal for category, _, nominal, role in
                UTILITY_SYSTEMS[meter["machine_code"]][2]
                if category == meter["type"] and role == meter["meter_role"]), None)
        else:
            specification = PROCESS_SPECS.get(meter["stage"], {}).get(meter["type"], (None, None))[1]
        meter["nominal"] = specification or max(1.0, meter["baseline"], meter["max_value"] * 0.35)
    return rows


def write_history(meters, start, end):
    from historical_baseline import FACTORY_TIMEZONE

    consumer = [meter for meter in meters if meter["meter_role"] == "consumption"]
    supply = [meter for meter in meters if meter["meter_role"] == "supply"]
    cursor = start
    batch = []
    total = 0
    columns = ["event_id", "sensor_id", "value", "unit", "observed_at", "received_at", "topic", "simulated"]
    while cursor < end:
        totals = {}
        values = {}
        for meter in consumer:
            value = normal_rate(meter, cursor)
            values[meter["id"]] = value
            totals[meter["type"]] = totals.get(meter["type"], 0) + value
        for meter in supply:
            if meter["machine_code"] == "GEN-01":
                value = normal_rate(meter, cursor)
            else:
                floor = meter["nominal"] * (0.07 if meter["type"] == "Steam" else 0.04)
                value = min(meter["max_value"] * 0.94, max(floor, totals.get(meter["type"], 0) * 1.04))
            values[meter["id"]] = round(value, 3)
        for meter in meters:
            batch.append([str(uuid.uuid5(uuid.NAMESPACE_URL, f"{HISTORY_TOPIC}/{meter['id']}/{cursor.isoformat()}")),
                          meter["id"], values[meter["id"]], meter["unit"], cursor, cursor,
                          HISTORY_TOPIC, True])
        if len(batch) >= 20000:
            with CH_LOCK:
                ch().insert("telemetry", batch, column_names=columns)
            total += len(batch)
            batch = []
            if total % 200000 < 20000:
                print(f"Inserted {total:,} synthetic readings", flush=True)
        cursor += timedelta(minutes=CADENCE_MINUTES)
    if batch:
        with CH_LOCK:
            ch().insert("telemetry", batch, column_names=columns)
        total += len(batch)
    return total


def build_profiles(conn, units):
    with CH_LOCK:
        rows = ch().query("""WITH toTimeZone(observed_at,'Asia/Jakarta') AS local_time
            SELECT sensor_id,toDayOfWeek(local_time,1) AS day_of_week,
                toHour(local_time)*4+intDiv(toMinute(local_time),15) AS slot,
                quantileExact(0.5)(value) AS median,
                quantileExact(0.1)(value) AS p10,
                quantileExact(0.9)(value) AS p90,
                count() AS samples,uniqExact(toDate(local_time)) AS days
            FROM telemetry WHERE topic={topic:String}
            GROUP BY sensor_id,day_of_week,slot ORDER BY sensor_id,day_of_week,slot""",
            parameters={"topic": HISTORY_TOPIC}).result_rows
    data = []
    for sensor_id, day, slot, median, p10, p90, samples, days in rows:
        expected, lower, upper = profile_bounds(median, p10, p90, units[sensor_id])
        data.append((sensor_id, day, slot, expected, lower, upper, samples, days, RUN_ID))
    with conn.cursor() as cursor:
        cursor.executemany("""INSERT INTO metering_baseline_profiles
            (sensor_id,day_of_week,slot,expected,lower_bound,upper_bound,sample_count,day_count,source)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(sensor_id,day_of_week,slot) DO UPDATE SET
              expected=EXCLUDED.expected,lower_bound=EXCLUDED.lower_bound,
              upper_bound=EXCLUDED.upper_bound,sample_count=EXCLUDED.sample_count,
              day_count=EXCLUDED.day_count,source=EXCLUDED.source,generated_at=now()""", data)
    return len(data)


def main():
    init_storage()
    with pg() as conn:
        if conn.execute("SELECT 1 FROM historical_seed_runs WHERE id=%s", (RUN_ID,)).fetchone():
            print("The three-month synthetic history is already installed; no rows were added.")
            return
    with pg() as conn:
        meters = ensure_catalog(conn)
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    start = previous_three_months(end)
    with CH_LOCK:
        existing, earliest, latest, meter_count = ch().query("""SELECT count(),min(observed_at),
            max(observed_at),uniqExact(sensor_id) FROM telemetry WHERE topic={topic:String}""",
            parameters={"topic": HISTORY_TOPIC}).first_row
    if existing:
        start = earliest.replace(tzinfo=timezone.utc)
        end = latest.replace(tzinfo=timezone.utc) + timedelta(minutes=CADENCE_MINUTES)
        expected = int((end - start).total_seconds() // (CADENCE_MINUTES * 60)) * len(meters)
        if (existing != expected or meter_count != len(meters) or
                start != previous_three_months(end) or end > datetime.now(timezone.utc) - timedelta(hours=1)):
            raise RuntimeError(f"Found {existing:,} partial synthetic rows without a completed run; inspect before retrying")
        total = existing
        print(f"Resuming baseline calculation from {existing:,} complete synthetic readings", flush=True)
    else:
        print(f"Generating {len(meters)} meter histories from {start.isoformat()} to {end.isoformat()} "
              f"at {CADENCE_MINUTES}-minute cadence", flush=True)
        total = write_history(meters, start, end)
    with pg() as conn:
        profiles = build_profiles(conn, {meter["id"]: meter["unit"] for meter in meters})
        conn.execute("""INSERT INTO historical_seed_runs(id,started_at,ended_at,row_count,meter_count)
            VALUES (%s,%s,%s,%s,%s)""", (RUN_ID, start, end, total, len(meters)))
    print(f"Completed: {total:,} readings; {profiles:,} quarter-hour baseline slots", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Historical seed failed: {error}", file=sys.stderr)
        raise
