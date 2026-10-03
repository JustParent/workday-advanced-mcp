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
    reference,
    summarize_reference,
)

OPERATION = "Get_Event_Detail"


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
        {
            "task": summarize_reference(
                task.get("Task_Reference") or task.get("Awaiting_Task_Reference")
            ),
            "status": summarize_reference(task.get("Task_Status_Reference")),
            "assigned_at": task.get("Assignment_Date", ""),
            "due_date": task.get("Due_Date", ""),
            "awaiting_people": [
                summarize_reference(p, preferred=("Employee_ID", WID))
                for p in as_list(task.get("Awaiting_Person_Reference"))
            ],
        }
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
        "completed_date": detail.get("Completed_Date") or "",
        "due_date": detail.get("Due_Date") or "",
        "state_reference": state,
        "process_history": detail.get("Process_History_Data") or [],
        "remaining_process": detail.get("Remaining_Process_Data") or [],
        "sub_events": detail.get("Sub-Event") or [],
        "awaiting_tasks": [task for task in awaiting_tasks if task],
    }


def build_worker_history(
    worker_id: str, updated_from: str | None = None, updated_through: str | None = None
) -> dict[str, Any]:
    from ..validation import iso_moment, required

    request: dict[str, Any] = {
        "Worker_Reference": {
            "Employee_Reference": {
                "Integration_ID_Reference": {
                    "ID": {
                        "@bsvc:System_ID": "WD-EMPLID",
                        "#text": required(worker_id, "worker_id"),
                    }
                }
            }
        }
    }
    dates = {}
    if updated_from:
        dates["Updated_From"] = iso_moment(updated_from, "updated_from")
    if updated_through:
        dates["Updated_Through"] = iso_moment(updated_through, "updated_through")
    if dates:
        request["Event_Date_Range_Data"] = dates
    return request


def parse_worker_history(response: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "event_id": e.get("Event_ID", ""),
            "description": e.get("Description", ""),
            "type": e.get("Event_Type_Reference") or {},
            "effective_date": e.get("Effective_Date", ""),
            "completed_at": e.get("Completed_DateTime", ""),
        }
        for e in as_list(
            (response.get("Worker_Event_History_Data") or {}).get("Event_Data")
        )
        if isinstance(e, dict)
    ]


def build_recovery(event_id: str, action: str, comment: str) -> dict[str, Any]:
    from ..validation import required

    if action not in {"cancel", "rescind"}:
        raise ValueError("action must be cancel or rescind.")
    return {
        "Event_Reference": reference(WID, required(event_id, "event_id")),
        f"{action.title()}_Business_Process_Data": {
            "Comment": required(comment, "comment"),
            "Suppress_Notifications": False,
        },
    }
