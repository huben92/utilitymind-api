"""Create a synthetic steel-plate pilot and three months of metering history.

Run in the dashboard container after the API schema migration. This data is
illustrative and must never be presented as measured plant performance.
"""

from datetime import datetime, timedelta, timezone
import math
import sys
import uuid

from historical_baseline import FACTORY_TIMEZONE, HISTORY_TOPIC, profile_bounds
from operations_app import CH_LOCK, ch, init_storage, pg
from seed_historical_data import previous_three_months, stable_noise


RUN_ID = "steel-plate-synthetic-history-v1"
CADENCE_MINUTES = 10

# A compact hot-rolling and finishing flow. Nominal values are synthetic
# rates, chosen to represent relative process loads rather than design data.
LOCATIONS = [
    ("HOT", "Hot Rolling Hall", "Slab reheating and hot rolling"),
    ("FIN", "Plate Finishing Hall", "Cooling, sizing, inspection and dispatch"),
]
LINES = [
    ("HOT", "HR-01", "Hot Rolling Line", "Reheat, descale and reduce slab thickness"),
    ("FIN", "PF-01", "Plate Finishing Line", "Cool, level, cut and inspect plate"),
]
MACHINES = [
    ("HR-01", "SLAB-01", "Slab Charging Conveyor", "Material handling", 1,
     [("Electricity", "kW", 22), ("Diesel", "L/h", 4)]),
    ("HR-01", "FURN-01", "Slab Reheating Furnace", "Reheating", 2,
     [("Natural Gas", "m³/h", 180), ("Electricity", "kW", 42)]),
    ("HR-01", "DESC-01", "High-Pressure Descaler", "Descaling", 3,
     [("Clean Water", "m³/h", 30), ("Electricity", "kW", 55)]),
    ("HR-01", "MILL-01", "Roughing Mill", "Rolling", 4,
     [("Electricity", "kW", 235), ("Compressed Air", "m³/h", 12)]),
    ("HR-01", "MILL-02", "Finishing Mill", "Rolling", 5,
     [("Electricity", "kW", 270), ("Compressed Air", "m³/h", 15)]),
    ("PF-01", "COOL-01", "Plate Cooling Bed", "Cooling", 1,
     [("Clean Water", "m³/h", 22), ("Electricity", "kW", 32)]),
    ("PF-01", "LEVEL-01", "Plate Leveler", "Finishing", 2,
     [("Electricity", "kW", 82), ("Compressed Air", "m³/h", 8)]),
    ("PF-01", "SHEAR-01", "Cut-to-Length Shear", "Cutting", 3,
     [("Electricity", "kW", 68), ("Compressed Air", "m³/h", 10)]),
    ("PF-01", "INSP-01", "Inspection and Packing", "Inspection", 4,
     [("Electricity", "kW", 24)]),
]


def ensure_catalog(conn):
    conn.execute("""INSERT INTO factories(name,code,description)
        VALUES ('Steel Plate Manufacturing','SP-01',
                'Synthetic pilot for hot-rolled steel plate utilities')
        ON CONFLICT(code) DO NOTHING""")
    factory_id = conn.execute("SELECT id FROM factories WHERE code='SP-01'").fetchone()["id"]
    location_ids = {}
    for code, name, description in LOCATIONS:
        conn.execute("""INSERT INTO locations(factory_id,name,code,description)
            VALUES (%s,%s,%s,%s) ON CONFLICT(factory_id,code) DO NOTHING""",
                     (factory_id, name, code, description))
        location_ids[code] = conn.execute("SELECT id FROM locations WHERE factory_id=%s AND code=%s",
                                          (factory_id, code)).fetchone()["id"]
    line_ids = {}
    for location, code, name, description in LINES:
        conn.execute("""INSERT INTO production_lines(location_id,name,code,description)
            VALUES (%s,%s,%s,%s) ON CONFLICT(location_id,code) DO NOTHING""",
                     (location_ids[location], name, code, description))
        line_ids[code] = conn.execute("SELECT id FROM production_lines WHERE location_id=%s AND code=%s",
                                      (location_ids[location], code)).fetchone()["id"]
    machine_ids = {}
    for line_code, code, name, category, stage, meters in MACHINES:
        conn.execute("""INSERT INTO machines
            (production_line_id,name,code,category,stage,lane,description)
            VALUES (%s,%s,%s,%s,%s,1,'Synthetic steel plate production flow')
            ON CONFLICT(production_line_id,code) DO NOTHING""",
                     (line_ids[line_code], name, code, category, stage))
        machine_id = conn.execute("SELECT id FROM machines WHERE production_line_id=%s AND code=%s",
                                  (line_ids[line_code], code)).fetchone()["id"]
        machine_ids[code] = machine_id
        for utility, unit, nominal in meters:
            sensor_id = "sp-" + code.lower() + "-" + utility.lower().replace(" ", "-")
            conn.execute("""INSERT INTO sensors
                (id,machine_id,name,type,unit,baseline,meter_role,enabled,min_value,max_value)
                VALUES (%s,%s,%s,%s,%s,%s,'consumption',true,0,%s)
                ON CONFLICT(id) DO NOTHING""",
                (sensor_id, machine_id, f"{utility} · {name}", utility, unit,
                 nominal, round(nominal * 1.65, 2)))
    for line_code in line_ids:
        chain = [code for line, code, *_ in MACHINES if line == line_code]
        for left, right in zip(chain, chain[1:]):
            conn.execute("""INSERT INTO machine_flow_links(left_machine_id,right_machine_id)
                VALUES (%s,%s) ON CONFLICT DO NOTHING""",
                (machine_ids[left], machine_ids[right]))
    rows = conn.execute("""SELECT s.id,s.unit,s.type,s.baseline,s.max_value,m.code AS machine_code,
            m.category AS stage FROM sensors s JOIN machines m ON m.id=s.machine_id
            JOIN production_lines p ON p.id=m.production_line_id
            JOIN locations l ON l.id=p.location_id
            WHERE l.factory_id=%s AND s.id LIKE 'sp-%%' ORDER BY s.id""", (factory_id,)).fetchall()
    if len(rows) != sum(len(machine[5]) for machine in MACHINES):
        raise RuntimeError("Steel plate catalog does not contain the expected meters")
    return factory_id, rows


def normal_rate(meter, instant):
    local = instant.astimezone(FACTORY_TIMEZONE)
    hour = local.hour + local.minute / 60
    work = 0.0 if local.weekday() == 6 or hour < 6 or hour >= (14 if local.weekday() == 5 else 22) else 1.0
    if work and (hour < 7 or hour >= 21):
        work *= 0.72
    if work and 12 <= hour < 13:
        work *= 0.88
    if local.weekday() == 5:
        work *= 0.75
    idle = 0.30 if meter["stage"] == "Reheating" and meter["type"] == "Natural Gas" else (
        0.035 if meter["type"] == "Electricity" else 0.0)
    daily = stable_noise(f"steel:{meter['machine_code']}:{local.date()}", 0.11)
    cycle = 1 + 0.06 * math.sin((local.hour * 60 + local.minute) / 23 + len(meter["id"]))
    jitter = stable_noise(f"steel:{meter['id']}:{instant.isoformat()}", 0.055)
    return round(min(meter["max_value"] * 0.93,
                     meter["baseline"] * (idle + (1 - idle) * work) * daily * cycle * jitter), 3)


def write_history(meters, start, end):
    cursor, batch, total = start, [], 0
    columns = ["event_id", "sensor_id", "value", "unit", "observed_at", "received_at", "topic", "simulated"]
    while cursor < end:
        for meter in meters:
            batch.append([str(uuid.uuid5(uuid.NAMESPACE_URL, f"{RUN_ID}/{meter['id']}/{cursor.isoformat()}")),
                          meter["id"], normal_rate(meter, cursor), meter["unit"], cursor, cursor,
                          HISTORY_TOPIC, True])
        if len(batch) >= 20000:
            with CH_LOCK:
                ch().insert("telemetry", batch, column_names=columns)
            total += len(batch)
            batch = []
            print(f"Inserted {total:,} steel plate readings", flush=True)
        cursor += timedelta(minutes=CADENCE_MINUTES)
    if batch:
        with CH_LOCK:
            ch().insert("telemetry", batch, column_names=columns)
        total += len(batch)
    return total


def build_profiles(conn, meters):
    ids = [meter["id"] for meter in meters]
    units = {meter["id"]: meter["unit"] for meter in meters}
    with CH_LOCK:
        rows = ch().query("""WITH toTimeZone(observed_at,'Asia/Jakarta') AS local_time
            SELECT sensor_id,toDayOfWeek(local_time,1) AS day_of_week,
                toHour(local_time)*4+intDiv(toMinute(local_time),15) AS slot,
                quantileExact(0.5)(value) AS median,
                quantileExact(0.1)(value) AS p10,
                quantileExact(0.9)(value) AS p90,
                count() AS samples,uniqExact(toDate(local_time)) AS days
            FROM telemetry WHERE topic={topic:String} AND sensor_id IN {ids:Array(String)}
            GROUP BY sensor_id,day_of_week,slot ORDER BY sensor_id,day_of_week,slot""",
            parameters={"topic": HISTORY_TOPIC, "ids": ids}).result_rows
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
            print("Steel plate history already installed; no rows added.")
            return
        factory_id, meters = ensure_catalog(conn)
    end = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    start = previous_three_months(end)
    ids = [meter["id"] for meter in meters]
    with CH_LOCK:
        existing, earliest, latest, meter_count = ch().query("""SELECT count(),min(observed_at),
            max(observed_at),uniqExact(sensor_id) FROM telemetry
            WHERE topic={topic:String} AND sensor_id IN {ids:Array(String)}""",
            parameters={"topic": HISTORY_TOPIC, "ids": ids}).first_row
    if existing:
        start = earliest.replace(tzinfo=timezone.utc)
        end = latest.replace(tzinfo=timezone.utc) + timedelta(minutes=CADENCE_MINUTES)
        expected = int((end - start).total_seconds() // (CADENCE_MINUTES * 60)) * len(meters)
        if existing != expected or meter_count != len(meters) or start != previous_three_months(end):
            raise RuntimeError("Partial steel plate history detected; inspect before rerunning")
        total = existing
    else:
        print(f"Generating {len(meters)} steel plate meters from {start} to {end}", flush=True)
        total = write_history(meters, start, end)
    with pg() as conn:
        profiles = build_profiles(conn, meters)
        conn.execute("""INSERT INTO historical_seed_runs(id,started_at,ended_at,row_count,meter_count)
            VALUES (%s,%s,%s,%s,%s)""", (RUN_ID, start, end, total, len(meters)))
    print(f"Factory {factory_id}: {total:,} synthetic readings, {profiles:,} baseline slots", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Steel plate seed failed: {error}", file=sys.stderr)
        raise
