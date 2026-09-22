"""Pieces shared by every business-process write."""

from __future__ import annotations

from typing import Any

# Stamped into Process History so a Workday administrator can tell where a change
# came from without asking. Callers append their own context.
COMMENT_PREFIX = "Submitted by Harriet"


def business_process_parameters(
    *, auto_complete: bool = False, comment: str | None = None
) -> dict[str, Any]:
    """The Business_Process_Parameters block.

    ``Run_Now`` is always true so the response carries the outcome synchronously
    rather than queueing the event for a background run.

    ``Auto_Complete`` defaults to false, which means the tenant's own approval
    routing for the business process applies. Setting it true bypasses those
    approvals, so it is refused unless the deployment opted in with
    WORKDAY_ALLOW_AUTO_COMPLETE.
    """
    parameters: dict[str, Any] = {
        "Auto_Complete": bool(auto_complete),
        "Run_Now": True,
    }
    text = (comment or "").strip()
    parameters["Comment_Data"] = {
        "Comment": f"{COMMENT_PREFIX}: {text}" if text else COMMENT_PREFIX
    }
    return parameters


def event_reference_from(response: dict[str, Any]) -> dict[str, Any]:
    """The event a write created, so its progress can be looked up later.

    Every staffing response names the event slightly differently
    (``Event_Reference``, ``*_Event_Reference``); take whichever is present.
    """
    from ..references import summarize_reference

    for key, value in response.items():
        if key.endswith("Event_Reference") and isinstance(value, dict):
            summary = summarize_reference(value)
            if summary:
                return summary
    return {}
