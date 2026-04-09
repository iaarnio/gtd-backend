from datetime import UTC, datetime


def utcnow_naive() -> datetime:
    """
    Return a naive UTC datetime without using deprecated utcnow().

    The codebase currently stores and compares naive UTC datetimes, so this
    preserves existing behavior while removing Python 3.14 deprecation warnings.
    """
    return datetime.now(UTC).replace(tzinfo=None)


def utcnow_iso_z() -> str:
    return f"{utcnow_naive().isoformat()}Z"
