"""Period reporting from metering metadata and hourly ClickHouse samples."""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
import re


REPORT_TIMEZONE = ZoneInfo("Asia/Jakarta")
UTILITY_ORDER = (
    "Electricity", "Clean Water", "Compressed Air", "Natural Gas", "Diesel",
    "Chilled Water", "Steam", "Hot Water", "Steam/Hot Water",
)
# Integrated quantity = average rate * covered hours * factor.
RATE_UNITS = {
    "kW": ("kWh", 1),
    "m³/h": ("m³", 1),
    "m3/h": ("m³", 1),
    "L/h": ("L", 1),
    "kg/h": ("kg", 1),
    "t/h": ("t", 1),
    "Nm³/min": ("Nm³", 60),
    "Nm3/min": ("Nm³", 60),
    "m³/min": ("m³", 60),
    "m3/min": ("m³", 60),
}


def period_bounds(kind, value, now=None):
    """Return a local calendar period with a UTC, exclusive observation end."""
    now = (now or datetime.now(timezone.utc)).astimezone(REPORT_TIMEZONE)
    if kind == "daily":
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value or ""):
            raise ValueError("Date must use YYYY-MM-DD")
        selected = date.fromisoformat(value)
        start = datetime.combine(selected, time.min, REPORT_TIMEZONE)
        calendar_end = start + timedelta(days=1)
        label = selected.strftime("%d %B %Y")
    elif kind == "monthly":
        if not re.fullmatch(r"\d{4}-\d{2}", value or ""):
            raise ValueError("Month must use YYYY-MM")
        selected = date.fromisoformat(value + "-01")
        start = datetime.combine(selected, time.min, REPORT_TIMEZONE)
        calendar_end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        label = selected.strftime("%B %Y")
    else:
        raise ValueError("Report type must be daily or monthly")
    if start >= now:
        raise ValueError("Select a current or past reporting period")
    end = min(calendar_end, now)
    return {
        "kind": kind, "key": value, "label": label,
        "start": start, "end": end, "calendar_end": calendar_end,
        "start_utc": start.astimezone(timezone.utc),
        "end_utc": end.astimezone(timezone.utc),
        "elapsed_seconds": (end - start).total_seconds(),
        "partial": end < calendar_end,
        "timezone": str(REPORT_TIMEZONE),
    }


def _rounded(value, digits=2):
    return round(value, digits) if value is not None else None


def _percent(part, whole):
    return _rounded(100 * part / whole, 1) if whole else None


def build_report(period, meters, hourly_rows, sample_interval=5, factory=None):
    """Calculate covered-time totals without filling telemetry gaps."""
    elapsed = period["elapsed_seconds"]
    by_id = {}
    for source in meters:
        meter = dict(source)
        meter.update(samples=0, simulated_samples=0, covered_seconds=0,
                     observed_total=None, average_rate=None, coverage_pct=0,
                     baseline_total=None, first_sample=None, last_sample=None)
        output = RATE_UNITS.get(meter["unit"])
        meter["quantity_unit"] = output[0] if output else None
        meter["rate_factor"] = output[1] if output else None
        meter["_total"] = 0.0
        meter["_baseline_total"] = 0.0
        by_id[meter["id"]] = meter

    bucket_totals = {}
    for row in hourly_rows:
        if len(row) == 8:
            sensor_id, bucket, mean_rate, samples, simulated, first_sample, last_sample, historical_samples = row
        else:
            sensor_id, bucket, mean_rate, samples, simulated, first_sample, last_sample = row
            historical_samples = 0
        meter = by_id.get(sensor_id)
        if not meter:
            continue
        bucket = bucket.replace(tzinfo=timezone.utc) if bucket.tzinfo is None else bucket.astimezone(timezone.utc)
        bucket_end = bucket + timedelta(hours=1)
        possible = max(0, (min(bucket_end, period["end_utc"]) - max(bucket, period["start_utc"])).total_seconds())
        covered = min(possible, (int(samples) - int(historical_samples)) * sample_interval
                      + int(historical_samples) * 300)
        if covered <= 0:
            continue
        meter["samples"] += int(samples)
        meter["simulated_samples"] += int(simulated)
        meter["covered_seconds"] += covered
        first_sample = first_sample.replace(tzinfo=timezone.utc) if first_sample.tzinfo is None else first_sample.astimezone(timezone.utc)
        last_sample = last_sample.replace(tzinfo=timezone.utc) if last_sample.tzinfo is None else last_sample.astimezone(timezone.utc)
        if meter["first_sample"] is None or first_sample < meter["first_sample"]:
            meter["first_sample"] = first_sample
        if meter["last_sample"] is None or last_sample > meter["last_sample"]:
            meter["last_sample"] = last_sample
        if meter["rate_factor"] is not None:
            quantity = float(mean_rate) * covered / 3600 * meter["rate_factor"]
            meter["_total"] += quantity
            meter["_baseline_total"] += float(meter["baseline"]) * covered / 3600 * meter["rate_factor"]
            local_bucket = bucket.astimezone(REPORT_TIMEZONE)
            key = local_bucket.strftime("%H:00") if period["kind"] == "daily" else local_bucket.strftime("%Y-%m-%d")
            bucket_totals[(meter["type"], meter["unit"], key)] = bucket_totals.get((meter["type"], meter["unit"], key), 0) + quantity

    for meter in by_id.values():
        meter["covered_seconds"] = min(elapsed, meter["covered_seconds"])
        meter["coverage_pct"] = _percent(meter["covered_seconds"], elapsed) or 0
        if meter["samples"] and meter["rate_factor"] is not None:
            meter["observed_total"] = _rounded(meter["_total"])
            meter["baseline_total"] = _rounded(meter["_baseline_total"])
            meter["average_rate"] = _rounded(meter["_total"] / (meter["covered_seconds"] / 3600 * meter["rate_factor"]))
        meter["first_sample"] = meter["first_sample"].isoformat() if meter["first_sample"] else None
        meter["last_sample"] = meter["last_sample"].isoformat() if meter["last_sample"] else None
        del meter["_total"], meter["_baseline_total"]

    axis = []
    cursor = period["start"]
    step = timedelta(hours=1) if period["kind"] == "daily" else timedelta(days=1)
    while cursor < period["end"]:
        axis.append(cursor.strftime("%H:00") if period["kind"] == "daily" else cursor.strftime("%Y-%m-%d"))
        cursor += step

    grouped = {}
    for meter in by_id.values():
        key = (meter["type"], meter["unit"])
        group = grouped.setdefault(key, {"category": meter["type"], "rate_unit": meter["unit"],
            "quantity_unit": meter["quantity_unit"], "meters": [], "machines": {}})
        group["meters"].append(meter)
        machine = group["machines"].setdefault(meter["machine_id"], {
            "name": meter["machine_name"], "code": meter["machine_code"],
            "location": meter["location_name"], "total": 0.0, "samples": 0})
        machine["total"] += meter["observed_total"] or 0
        machine["samples"] += meter["samples"]

    utilities = []
    for key, group in grouped.items():
        members = group["meters"]
        count = sum(m["samples"] for m in members)
        covered = sum(m["covered_seconds"] for m in members)
        simulated = sum(m["simulated_samples"] for m in members)
        observed = sum(m["observed_total"] or 0 for m in members) if count and group["quantity_unit"] else None
        baseline_total = sum(m["baseline_total"] or 0 for m in members)
        utilization = _percent(observed, baseline_total) if observed is not None and baseline_total > 0 else None
        machines = sorted((m for m in group["machines"].values() if m["samples"]), key=lambda item: -item["total"])
        group.update(
            meter_count=len(members), reporting_meters=sum(bool(m["samples"]) for m in members),
            samples=count, simulated_samples=simulated,
            coverage_pct=_percent(covered, len(members) * elapsed) or 0,
            observed_total=_rounded(observed), baseline_total=_rounded(baseline_total) if count and group["quantity_unit"] else None,
            utilization_pct=utilization,
            machines=[{**machine, "total": _rounded(machine["total"])} for machine in machines],
            trend=[{"bucket": bucket, "value": _rounded(bucket_totals[(key[0], key[1], bucket)])
                    if (key[0], key[1], bucket) in bucket_totals else None} for bucket in axis],
        )
        del group["meters"]
        utilities.append(group)
    utilities.sort(key=lambda group: (UTILITY_ORDER.index(group["category"]) if group["category"] in UTILITY_ORDER else len(UTILITY_ORDER), group["category"], group["rate_unit"]))

    total_samples = sum(m["samples"] for m in by_id.values())
    total_simulated = sum(m["simulated_samples"] for m in by_id.values())
    coverage = _percent(sum(m["covered_seconds"] for m in by_id.values()), len(by_id) * elapsed) or 0
    if not total_samples:
        origin = "No readings"
    elif total_simulated == total_samples:
        origin = "Simulated"
    elif total_simulated:
        origin = "Mixed simulated and physical"
    else:
        origin = "Physical"
    review = []
    for group in utilities:
        if not group["samples"]:
            review.append(f"{group['category']}: no telemetry in the selected period. Check meter availability before using totals.")
        elif group["coverage_pct"] < 90:
            review.append(f"{group['category']}: {group['coverage_pct']:.1f}% catalog coverage. Observed totals omit unmeasured time; inspect gaps and meter status.")
        if group["utilization_pct"] is not None and group["utilization_pct"] > 100:
            review.append(f"{group['category']}: observed rate is {group['utilization_pct']:.1f}% of the current configured baseline over covered time. Review the baseline and production context.")
    if not review:
        review.append("No coverage or baseline screening items from the available metering data. Production alignment still requires production records.")

    return {
        "kind": period["kind"], "period": period["key"], "label": period["label"],
        "timezone": period["timezone"], "period_start": period["start"].isoformat(),
        "period_end": period["end"].isoformat(), "partial": period["partial"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "factory": factory or {"name": "All factories", "code": "ALL"},
        "data_origin": origin, "simulated_samples": total_simulated, "samples": total_samples,
        "meter_count": len(by_id), "reporting_meters": sum(bool(m["samples"]) for m in by_id.values()),
        "coverage_pct": coverage, "utilities": utilities, "meter_quality": list(by_id.values()),
        "review_items": review,
        "unavailable": [
            "Production output and targets are not connected; usage per product and production alignment cannot be calculated.",
            "Actual shift schedules and attendance are not connected; per-shift usage is unavailable.",
            "No verified anomaly workflow or operating-limit history is connected; anomaly counts and supply compliance are unavailable.",
            "Tariffs are not configured; cost and savings are unavailable.",
        ],
        "method": (
            f"Hourly average rate multiplied by observed sample coverage, using {sample_interval}-second live intervals "
            "and five-minute synthetic-history intervals "
            "and a one-hour cap per meter. Gaps are not filled. Coverage is measured against the current metering catalog "
            "for the elapsed period. Current configured baselines are reference rates, not historical or production-adjusted targets."
        ),
    }
