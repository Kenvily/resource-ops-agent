"""Calendar-month query boundary in the platform's Asia/Shanghai timezone."""

from calendar import monthrange
from datetime import datetime
from zoneinfo import ZoneInfo


def months_ago(timestamp: int, months: int) -> int:
    end = datetime.fromtimestamp(timestamp, ZoneInfo("Asia/Shanghai"))
    month_index = end.year * 12 + end.month - 1 - months
    year, month = divmod(month_index, 12)
    month += 1
    start = end.replace(year=year, month=month, day=min(end.day, monthrange(year, month)[1]))
    return int(start.timestamp())


def four_months_ago(timestamp: int) -> int:
    return months_ago(timestamp, 4)


def range_seconds(window, end):
    fixed = {"1h": 3600, "1d": 86400, "24h": 86400, "1w": 604800, "7d": 604800}
    if window in fixed:
        return fixed[window]
    return end - months_ago(end, {"1mo": 1, "3mo": 3, "4mo": 4}.get(window, 4))
