"""Keep every page of a scan at the same Workday entry moment."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .validation import iso_moment


def entry_moment(value: str | None = None) -> str:
    return (
        iso_moment(value, "as_of_entry_datetime")
        if value
        else datetime.now(UTC).isoformat()
    )


def at_entry_moment(request: dict[str, Any], moment: str) -> dict[str, Any]:
    filters = dict(request.get("Response_Filter", {}))
    effective = filters.pop("As_Of_Effective_Date", None)
    filters.pop("As_Of_Entry_DateTime", None)
    return {
        **request,
        "Response_Filter": {
            **({"As_Of_Effective_Date": effective} if effective else {}),
            "As_Of_Entry_DateTime": moment,
            **filters,
        },
    }
