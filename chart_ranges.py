"""Calendar windows and chart resolution for utility rate trends."""

from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone

from historical_baseline import FACTORY_TIMEZONE, slot_key


RANGES = {"60m", "today", "week", "month", "last_month", "3m", "custom"}


def months_ago(day, count):
    month_index = day.year * 12 + day.month - 1 - count
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    return day.replace(year=year, month=month,
                       day=min(day.day, monthrange(year, month)[1]))


def chart_window(args, now=None):
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    local_now = now.astimezone(FACTORY_TIMEZONE)
    today = local_now.date()
    period = args.get("range", "60m")
    if period not in RANGES:
        raise ValueError("Unknown chart range")

    midnight = lambda day: datetime.combine(day, time.min, FACTORY_TIMEZONE)
    if period == "60m":
        end = now
        start = end - timedelta(minutes=60)
    elif period == "today":
        start, end = midnight(today), now
    elif period == "week":
        start, end = midnight(today - timedelta(days=today.weekday())), now
    elif period == "month":
        start, end = midnight(today.replace(day=1)), now
    elif period == "last_month":
        end = midnight(today.replace(day=1))
        start = midnight(months_ago(today.replace(day=1), 1))
    elif period == "3m":
        earlier = months_ago(today, 3)
        start = local_now.replace(year=earlier.year, month=earlier.month, day=earlier.day)
        end = now
    else:
        try:
            start_day = date.fromisoformat(args.get("start", ""))
            end_day = date.fromisoformat(args.get("end", ""))
        except ValueError as exc:
            raise ValueError("Choose valid start and end dates") from exc
        if end_day < start_day:
            raise ValueError("End date must be on or after start date")
        start = midnight(start_day)
        end = min(midnight(end_day + timedelta(days=1)), now)

    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    seconds = (end - start).total_seconds()
    if seconds <= 0:
        raise ValueError("Chart range must include past time")
    if seconds > 366 * 86400:
        raise ValueError("Choose a range of 366 days or less")
    if seconds <= 2 * 3600:
        bucket_seconds = 60
    elif seconds <= 36 * 3600:
        bucket_seconds = 300
    elif seconds <= 8 * 86400:
        bucket_seconds = 1800
    elif seconds <= 35 * 86400:
        bucket_seconds = 10800
    elif seconds <= 100 * 86400:
        bucket_seconds = 21600
    else:
        bucket_seconds = 86400
    return {"range": period, "start": start, "end": end,
            "bucket_seconds": bucket_seconds}


def historical_bucket_series(profiles, meter_ids, window):
    """Average the expected rate over each chart bucket, with complete meter coverage."""
    if not meter_ids:
        return []
    slot_totals = {}
    for weekday in range(7):
        for slot in range(96):
            values = [profiles.get((meter_id, weekday, slot)) for meter_id in meter_ids]
            if all(row is not None for row in values):
                slot_totals[(weekday, slot)] = sum(float(row["expected"]) for row in values)
    start, end, width = window["start"], window["end"], window["bucket_seconds"]
    result = []
    bucket = start
    while bucket < end:
        stop = min(bucket + timedelta(seconds=width), end)
        sample = bucket
        values = []
        cadence = timedelta(seconds=min(width, 900))
        while sample < stop:
            value = slot_totals.get(slot_key(sample))
            if value is None:
                values = []
                break
            values.append(value)
            sample += cadence
        if values:
            result.append({"minute": bucket.isoformat(),
                           "value": round(sum(values) / len(values), 2)})
        bucket = stop
    return result
