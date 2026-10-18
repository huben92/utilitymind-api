"""Time-matched, simulated-history metering baselines.

Profiles are quarter-hour slots in the factory's local week. They are kept
separate from the operator's configured baseline and operational maximum.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


FACTORY_TIMEZONE = ZoneInfo("Asia/Jakarta")
HISTORY_TOPIC = "synthetic-history/wheelworks-v1"
MIN_PROFILE_DAYS = 8


def slot_key(instant):
    local = instant.astimezone(FACTORY_TIMEZONE)
    return local.weekday(), local.hour * 4 + local.minute // 15


def recent_minutes(end, count=60):
    end = end.astimezone(timezone.utc).replace(second=0, microsecond=0)
    return [end - timedelta(minutes=count - 1 - index) for index in range(count)]


def load_profiles(conn, meter_ids, instants):
    if not meter_ids or not instants:
        return {}
    slots = {slot_key(instant) for instant in instants}
    rows = conn.execute("""SELECT sensor_id,day_of_week,slot,expected,lower_bound,upper_bound,
            sample_count,day_count,source
        FROM metering_baseline_profiles WHERE sensor_id=ANY(%s)
          AND day_of_week=ANY(%s) AND slot=ANY(%s)""",
        (list(meter_ids), list({day for day, _ in slots}), list({slot for _, slot in slots}))).fetchall()
    return {(row["sensor_id"], row["day_of_week"], row["slot"]): row
            for row in rows if row["day_count"] >= MIN_PROFILE_DAYS}


def reading_profile(profiles, sensor_id, instant):
    return profiles.get((sensor_id, *slot_key(instant)))


def expected_series(profiles, sensor_ids, end, count=60):
    """Sum only fully covered slots, so missing meters cannot understate demand."""
    if not sensor_ids:
        return []
    result = []
    for minute in recent_minutes(end, count):
        rows = [reading_profile(profiles, sensor_id, minute) for sensor_id in sensor_ids]
        if any(row is None for row in rows):
            continue
        result.append({"minute": minute.isoformat(),
                       "value": round(sum(row["expected"] for row in rows), 2),
                       "lower": round(sum(row["lower_bound"] for row in rows), 2),
                       "upper": round(sum(row["upper_bound"] for row in rows), 2)})
    return result


def profile_bounds(median, p10, p90, unit):
    """A conservative empirical band, with a small absolute floor at zero."""
    absolute = {"kW": 1.0, "m³/h": 0.5, "L/h": 0.2, "kg/h": 1.0}.get(unit, 0.5)
    lower = max(0.0, min(p10 * 0.9, median * 0.85))
    upper = max(p90 * 1.1, median * 1.2, median + absolute)
    return round(median, 3), round(lower, 3), round(upper, 3)
