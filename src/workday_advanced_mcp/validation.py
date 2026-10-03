"""Small, deterministic checks shared by previews and submissions."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any


def iso_date(value: str, name: str = "effective_date") -> str:
    try:
        parsed = date.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{name} must be a valid date in YYYY-MM-DD format.") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{name} must use YYYY-MM-DD format.")
    return value


def iso_moment(value: str, name: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone.")
    return value


def validate_dates(data: Any) -> None:
    if isinstance(data, dict):
        for key, value in data.items():
            if key.endswith("_Date") and isinstance(value, str):
                iso_date(value, key)
            validate_dates(value)
    elif isinstance(data, list):
        for item in data:
            validate_dates(item)


def required(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required.")
    return value.strip()
