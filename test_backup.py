"""Tests for backup.py's UTC timestamp generation (INF-17).

Run: pytest test_backup.py
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import backup  # noqa: E402


def test_utc_timestamp_str_converts_non_utc_local_time():
    # Wall-clock 05:18:37 on a host whose local zone is +02:00 is 03:18:37 UTC.
    # Against the pre-fix code (naive `datetime.now()`, no conversion) this
    # would have produced "051837" instead of "031837".
    local_plus2 = datetime(2026, 8, 17, 5, 18, 37, tzinfo=timezone(timedelta(hours=2)))
    assert backup.utc_timestamp_str(local_plus2) == "2026-08-17T031837Z"


def test_utc_timestamp_str_defaults_to_real_utc_now():
    result = backup.utc_timestamp_str()
    parsed = datetime.strptime(result, "%Y-%m-%dT%H%M%SZ")
    now_utc_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs((now_utc_naive - parsed).total_seconds()) < 5
