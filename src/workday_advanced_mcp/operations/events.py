"""Get_Event_Details: what happened to a business process after it was submitted.

This is the recovery path. A write that times out, or one submitted without
Auto_Complete so the tenant's approvers still have it, cannot be confirmed by
re-reading the worker: the data has not changed yet. The event says where it got
to, and re-reading an event is safe where re-sending a write is not.

Element order for Get_Event_Details_Request:
    Request_References(Event_Reference) -> Response_Filter
"""

from __future__ import annotations

from typing import Any

from ..references import (
    WID,
    as_list,
    extract_descriptor,
    reference,
    summarize_reference,
)

OPERATION = "Get_Event_Details"


def build_get_event_request(event_id: str, id_type: str = WID) -> dict[str, Any]:
    return {
        "Request_References": {"Event_Reference": reference(id_type, event_id)},
    }


def parse_event(response: dict[str, Any]) -> dict[str, Any]:
    """Flatten the first event in a Get_Event_Details_Response."""
    response_data = response.get("Response_Data")
    if not isinstance(response_data, dict):
        return {}
    events = [e for e in as_list(response_data.get("Event")) if isinstance(e, dict)]
    if not events:
        return {}

    event = events[0]
    detail = event.get("Event_Detail_Data")
    detail = detail if isinstance(detail, dict) else {}

    awaiting = detail.get("Awaiting_Task_Data")
    awaiting_tasks = [
        extract_descriptor(task.get("Awaiting_Task_Reference") or task)
        for task in as_list(awaiting)
        if isinstance(task, dict)
    ]

    state = summarize_reference(detail.get("Event_State_Reference"))
    return {
        "event": summarize_reference(event.get("Event_Reference")),
        "state": state.get("descriptor") or state.get("id", ""),
        "target": summarize_reference(detail.get("Event_Target_Reference")),
        "category": summarize_reference(detail.get("Event_Category_Reference")).get(
            "descriptor", ""
        ),
        "initiated_by": summarize_reference(
            detail.get("Initiating_Person_Reference")
        ).get("descriptor", ""),
        "creation_date": detail.get("Creation_Date") or "",
        "awaiting_tasks": [task for task in awaiting_tasks if task],
    }
