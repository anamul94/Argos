"""JSON serialization helpers shared across tools and nodes."""

import datetime as dt
from decimal import Decimal


def make_json_safe(obj: object) -> object:
    """Recursively convert non-JSON-serialisable types to safe equivalents.

    Returns a new object with datetime → ISO string, Decimal → string,
    bytes → UTF-8 string, sets/tuples → list.
    """
    if isinstance(obj, (dt.datetime, dt.date, dt.time)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    if isinstance(obj, dict):
        return {k: make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [make_json_safe(v) for v in obj]
    return obj
