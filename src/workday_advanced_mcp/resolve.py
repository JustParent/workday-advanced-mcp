"""Turning what a caller has into what Workday needs.

A caller usually holds an employee ID and a name. Workday writes want a position
reference, an organization reference ID and a tenant-defined reason code. These
helpers do that resolution once so the read tools, the write tools and the
preview all agree on what a given worker currently looks like.
"""

from __future__ import annotations

from typing import Any

from .client import SERVICE_HUMAN_RESOURCES, WorkdayClient
from .operations import workers
from .references import EMPLOYEE_ID, WORKER_ID_TYPES

MAX_SCAN_PAGES = 20
SCAN_PAGE_SIZE = 100


class WorkerNotFound(ValueError):
    """No worker matched, stated as something the caller can act on."""


async def fetch_worker(
    client: WorkdayClient,
    worker_id: str,
    id_type: str = EMPLOYEE_ID,
    *,
    detailed: bool = True,
    as_of_effective_date: str | None = None,
) -> dict[str, Any]:
    """Read one worker by reference and flatten it."""
    identifier = (worker_id or "").strip()
    if not identifier:
        raise ValueError("A worker id is required.")
    if id_type not in WORKER_ID_TYPES:
        raise ValueError(
            f"Unknown worker id type {id_type!r}. Use one of: "
            f"{', '.join(WORKER_ID_TYPES)}."
        )

    request = workers.build_get_worker_request(
        identifier,
        id_type,
        response_group=workers.GROUP_FULL if detailed else workers.GROUP_IDENTITY,
        as_of_effective_date=as_of_effective_date,
    )
    result = await client.call(
        SERVICE_HUMAN_RESOURCES, workers.OPERATION, request, is_read=True
    )
    found = workers.workers_in(result.response(workers.OPERATION))
    if not found:
        raise WorkerNotFound(
            f"No Workday worker has {id_type} {identifier!r}. Check the id, or search "
            "by email with workday_find_worker."
        )
    return workers.summarize_worker(found[0], detailed=detailed)


async def scan_for_email(
    client: WorkdayClient,
    email: str,
    *,
    max_pages: int = MAX_SCAN_PAGES,
    include_inactive: bool = False,
) -> tuple[list[dict[str, Any]], bool]:
    """Find workers by work email.

    Get_Workers cannot filter on email, so this pages through workers and matches
    locally. It is bounded: the second return value is False when the cap was hit
    before the tenant ran out of workers, so a caller is never told "not found"
    when the answer was simply beyond the scan.
    """
    needle = (email or "").strip().lower()
    if not needle:
        return [], True

    matches: list[dict[str, Any]] = []
    page = 1
    while page <= max_pages:
        request = workers.build_scan_request(
            page,
            SCAN_PAGE_SIZE,
            response_group=workers.GROUP_IDENTITY,
            exclude_inactive=not include_inactive,
        )
        result = await client.call(
            SERVICE_HUMAN_RESOURCES, workers.OPERATION, request, is_read=True
        )
        response = result.response(workers.OPERATION)
        entries = workers.workers_in(response)
        if not entries:
            return matches, True

        for entry in entries:
            summary = workers.summarize_worker(entry, detailed=False)
            emails = [str(e).lower() for e in summary.get("emails", [])]
            if needle in emails:
                matches.append(summary)

        # Trust Workday's own page count when it gives one; a short page only
        # means the end of the results when it does not.
        current_page, total_pages = workers.page_info(response)
        if total_pages:
            if current_page >= total_pages:
                return matches, True
        elif len(entries) < SCAN_PAGE_SIZE:
            return matches, True
        page += 1

    return matches, False


def assignments_by_type(worker_summary: dict[str, Any]) -> dict[str, list[str]]:
    """The worker's current organization assignments, keyed by organization type.

    This is the baseline a change merges into: Workday replaces the assignments
    for every organization type present in a Change_Organization_Assignments
    request, so anything left out of the request is at risk of being dropped.
    """
    current: dict[str, list[str]] = {}
    for assignment in worker_summary.get("organization_assignments", []) or []:
        organization_type = str(assignment.get("organization_type") or "")
        # Normalise the descriptor spelling ("Cost Center") to the schema's key.
        key = organization_type.replace(" ", "_")
        identifier = str(assignment.get("reference_id") or assignment.get("id") or "")
        if not key or not identifier:
            continue
        current.setdefault(key, [])
        if identifier not in current[key]:
            current[key].append(identifier)
    return current


def describe_worker(worker_summary: dict[str, Any]) -> str:
    """A short label for messages: ``Jane Doe (21001)``."""
    name = worker_summary.get("name") or worker_summary.get("descriptor") or "worker"
    identifier = worker_summary.get("worker_id") or worker_summary.get("wid") or "?"
    return f"{name} ({identifier})"
