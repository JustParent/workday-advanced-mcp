"""Read tools: resolve what a caller has into what a write needs.

None of these change anything in Workday, so none are confirmation-gated. They
exist mostly to make guessing unnecessary: reference IDs are tenant-specific, and
a write built from a guessed ID either fails validation or, worse, succeeds
against the wrong record.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .client import (
    SERVICE_CASH_MANAGEMENT,
    SERVICE_HUMAN_RESOURCES,
    SERVICE_INTEGRATIONS,
    SERVICE_RECRUITING,
    WorkdayClient,
    get_client,
)
from .errors import WorkdayFaultError, WorkdayNotFoundError
from .operations import events, organizations, payment_elections, recruiting, workers
from .pagination import at_entry_moment, entry_moment
from .references import EMPLOYEE_ID, ORGANIZATION_TYPES, WID, WORKER_ID_TYPES
from .registry import ToolRegistrar
from .resolve import fetch_worker, scan_for_email

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000


async def _paged(
    client: WorkdayClient,
    service: str,
    operation: str,
    *,
    build: Callable[[int, int], dict[str, Any]],
    parse: Callable[[dict[str, Any]], list[dict[str, Any]]],
    limit: int,
    query: str = "",
    page_size: int = organizations.PAGE_SIZE,
) -> dict[str, Any]:
    """Page a list operation until the limit is reached or Workday runs out."""
    if limit < 1:
        raise ValueError("limit must be positive.")
    items: list[dict[str, Any]] = []
    page = 1
    truncated = False
    moment = entry_moment()

    while True:
        result = await client.call(
            service,
            operation,
            at_entry_moment(build(page, page_size), moment),
            is_read=True,
        )
        response = result.response(operation)
        entries = parse(response)

        for entry in entries:
            if not organizations.matches_query(entry, query):
                continue
            items.append(entry)
            if len(items) >= limit:
                truncated = True
                break
        if truncated:
            break

        current_page, total_pages = workers.page_info(response)
        if total_pages:
            if current_page >= total_pages:
                break
        elif len(entries) < page_size:
            break
        page += 1

    return {"count": len(items), "truncated": truncated, "entries": items}


def register_read_tools(registrar: ToolRegistrar) -> None:
    """Attach every read tool the deployment allows."""

    async def workday_find_worker(
        query: str,
        id_type: str | None = None,
        include_inactive: bool = False,
        max_scan_pages: int = 20,
    ) -> Any:
        client = get_client()
        text = (query or "").strip()
        if not text:
            raise ValueError("Pass an employee id, a WID, or a work email address.")

        if id_type:
            worker = await fetch_worker(client, text, id_type, detailed=False)
            return {
                "match_type": "reference",
                "candidates": [worker],
                "scan_complete": True,
            }

        if "@" in text and " " not in text:
            matches, complete = await scan_for_email(
                client,
                text,
                max_pages=max_scan_pages,
                include_inactive=include_inactive,
            )
            return {
                "match_type": "email",
                "candidates": matches,
                "scan_complete": complete,
                "note": (
                    ""
                    if complete
                    else "The scan stopped at its page cap before reaching the "
                    "end of the tenant. A worker not listed here may still "
                    "exist - search by "
                    "employee id instead, or raise max_scan_pages."
                ),
            }

        # Not an email: try each worker id type in turn.
        for candidate_type in WORKER_ID_TYPES:
            try:
                worker = await fetch_worker(
                    client, text, candidate_type, detailed=False
                )
            except ValueError:
                continue
            return {
                "match_type": candidate_type,
                "candidates": [worker],
                "scan_complete": True,
            }
        return {
            "match_type": "none",
            "candidates": [],
            "scan_complete": True,
            "note": (
                f"Nothing in Workday matched {text!r} as an "
                f"{', '.join(WORKER_ID_TYPES)}. If this is a name, search by work "
                f"email "
                "or employee id instead."
            ),
        }

    registrar.add(
        workday_find_worker,
        name="workday_find_worker",
        title="Find a Workday worker",
        description=(
            "Resolve a worker by employee id, WID, or work email address, returning "
            "the candidates rather than guessing between them. Pass id_type to look a "
            "known id up directly. An email address has to be matched by scanning, "
            "because Workday's worker search cannot filter on it: the result reports "
            "scan_complete, and a false value means the scan hit its page cap rather "
            "than that the worker does not exist. Use this before any write, then read "
            "the full record with workday_get_worker."
        ),
        read_only=True,
        idempotent=True,
    )

    async def workday_get_worker(
        worker_id: str,
        id_type: str = EMPLOYEE_ID,
        as_of_effective_date: str | None = None,
    ) -> Any:
        return await fetch_worker(
            get_client(),
            worker_id,
            id_type,
            detailed=True,
            as_of_effective_date=as_of_effective_date,
        )

    registrar.add(
        workday_get_worker,
        name="workday_get_worker",
        title="Get a Workday worker",
        description=(
            "Full detail for one worker: name and work email, employment status with "
            "hire and termination dates, the primary position (including the position "
            "id that organization-assignment changes address), job profile, location, "
            "and every organization the position is assigned to, such as its cost "
            "centre and supervisory organization. Read this before changing a worker: "
            "the write tools need the position id and the current assignments, and the "
            "result is the baseline any change is described against."
        ),
        read_only=True,
        idempotent=True,
    )

    async def workday_list_organizations(
        organization_type: str = "Supervisory",
        query: str | None = None,
        limit: int = DEFAULT_LIMIT,
        include_inactive: bool = False,
        as_of_effective_date: str | None = None,
    ) -> Any:
        if organization_type not in ORGANIZATION_TYPES:
            raise ValueError(
                f"Unknown organization type {organization_type!r}. Supported: "
                f"{', '.join(ORGANIZATION_TYPES)}."
            )
        return await _paged(
            get_client(),
            SERVICE_HUMAN_RESOURCES,
            organizations.GET_ORGANIZATIONS,
            build=lambda page, size: organizations.build_get_organizations_request(
                organization_type,
                page,
                size,
                include_inactive=include_inactive,
                as_of_effective_date=as_of_effective_date,
            ),
            parse=lambda response: organizations.parse_organizations(
                response, organization_type
            ),
            limit=min(limit, MAX_LIMIT),
            query=query or "",
        )

    registrar.add(
        workday_list_organizations,
        name="workday_list_organizations",
        title="List Workday organizations",
        description=(
            "List organizations of one type, with the reference id a write needs. "
            f"Supported types: {', '.join(ORGANIZATION_TYPES)}. Use Supervisory to "
            f"find "
            "the organization a worker should report into for workday_change_job, and "
            "Cost_Center to find the cost centre for "
            "workday_change_organization_assignments. Filter with query, which matches "
            "the id, name, code and descriptor case-insensitively."
        ),
        read_only=True,
        idempotent=True,
    )

    async def workday_list_reference_ids(
        reference_id_type: str,
        query: str | None = None,
        limit: int = 200,
    ) -> Any:
        identifier = (reference_id_type or "").strip()
        if not identifier:
            raise ValueError(
                "Pass a reference id type, for example Termination_Subcategory_ID or "
                "Event_Classification_Subcategory_ID."
            )
        return await _paged(
            get_client(),
            SERVICE_INTEGRATIONS,
            organizations.GET_REFERENCES,
            build=lambda page, size: organizations.build_get_references_request(
                identifier, page, size
            ),
            parse=lambda response: organizations.parse_references(response, identifier),
            limit=min(limit, MAX_LIMIT),
            query=query or "",
        )

    registrar.add(
        workday_list_reference_ids,
        name="workday_list_reference_ids",
        title="List Workday reference ids",
        description=(
            "List the values of one Workday Reference ID Type. These vocabularies are "
            "defined per tenant, so their ids cannot be guessed or carried over from "
            "another customer. Use Termination_Subcategory_ID for the reason a "
            "termination needs, and Event_Classification_Subcategory_ID for the reason "
            "a job change needs. Filter with query."
        ),
        read_only=True,
        idempotent=True,
    )

    async def workday_list_job_profiles(
        query: str | None = None, limit: int = DEFAULT_LIMIT
    ) -> Any:
        return await _paged(
            get_client(),
            SERVICE_HUMAN_RESOURCES,
            organizations.GET_JOB_PROFILES,
            build=organizations.build_get_job_profiles_request,
            parse=organizations.parse_job_profiles,
            limit=min(limit, MAX_LIMIT),
            query=query or "",
        )

    registrar.add(
        workday_list_job_profiles,
        name="workday_list_job_profiles",
        title="List Workday job profiles",
        description=(
            "List job profiles with the Job_Profile_ID a hire or job change needs. "
            "Filter with query, which matches the id and the profile name."
        ),
        read_only=True,
        idempotent=True,
    )

    async def workday_list_locations(
        query: str | None = None, limit: int = DEFAULT_LIMIT
    ) -> Any:
        return await _paged(
            get_client(),
            SERVICE_HUMAN_RESOURCES,
            organizations.GET_LOCATIONS,
            build=organizations.build_get_locations_request,
            parse=organizations.parse_locations,
            limit=min(limit, MAX_LIMIT),
            query=query or "",
        )

    registrar.add(
        workday_list_locations,
        name="workday_list_locations",
        title="List Workday locations",
        description=(
            "List locations with the Location_ID a hire or job change needs. Filter "
            "with query, which matches the id and the location name."
        ),
        read_only=True,
        idempotent=True,
    )

    async def workday_list_pre_hires(
        query: str | None = None, limit: int = DEFAULT_LIMIT
    ) -> Any:
        text = (query or "").strip()
        email = text if "@" in text and " " not in text else None
        return await _paged(
            get_client(),
            SERVICE_RECRUITING,
            recruiting.GET_APPLICANTS,
            build=lambda page, size: recruiting.build_get_applicants_request(
                page, size, email=email
            ),
            parse=recruiting.parse_applicants,
            limit=min(limit, MAX_LIMIT),
            query="" if email else text,
        )

    registrar.add(
        workday_list_pre_hires,
        name="workday_list_pre_hires",
        title="List Workday pre-hires",
        description=(
            "List pre-hire (applicant) records with the WID that workday_hire_employee "
            "takes. Search by email address to check whether somebody already exists "
            "before creating them again; passing a name filters the listing locally."
        ),
        read_only=True,
        idempotent=True,
    )

    async def workday_get_payment_elections(
        worker_id: str,
        id_type: str = EMPLOYEE_ID,
        scan_if_not_found: bool = True,
        max_scan_pages: int = 5,
    ) -> Any:
        client = get_client()
        worker = await fetch_worker(client, worker_id, id_type, detailed=False)
        worker_wid = worker.get("wid") or ""
        if not worker_wid:
            raise ValueError(
                f"Workday returned no WID for {worker_id!r}, which payment elections "
                "are keyed by."
            )

        entries = await _direct_enrollments(client, worker_wid)

        scan_complete = True
        if not entries and scan_if_not_found:
            entries, scan_complete = await _scan_enrollments(
                client, worker_wid, max_scan_pages
            )

        parsed = [payment_elections.parse_enrollment(entry) for entry in entries]
        return {
            "worker": worker,
            "enrollments": [p for p in parsed if p],
            "scan_complete": scan_complete,
            "note": (
                "Account numbers are masked to their last four digits. Pass the full "
                "number only when setting a new election."
            ),
        }

    registrar.add(
        workday_get_payment_elections,
        name="workday_get_payment_elections",
        title="Get Workday payment elections",
        description=(
            "Read a worker's current payment elections: the rule groups, the order and "
            "distribution of each election, and the bank account behind it with the "
            "account number masked to its last four digits. Always read this before "
            "workday_set_payment_elections, because submitting elections replaces "
            "every "
            "election the worker has, so anything not carried forward is deleted."
        ),
        read_only=True,
        idempotent=True,
    )

    async def workday_get_event(event_id: str, id_type: str = WID) -> Any:
        identifier = (event_id or "").strip()
        if not identifier:
            raise ValueError(
                "Pass the event id returned by the write you are checking."
            )
        client = get_client()
        try:
            result = await client.call(
                SERVICE_INTEGRATIONS,
                events.OPERATION,
                events.build_get_event_request(identifier, id_type),
                is_read=True,
            )
        except WorkdayNotFoundError:
            parsed: dict[str, Any] = {}
        else:
            parsed = events.parse_event(result.response(events.OPERATION))
        if not parsed:
            raise ValueError(
                f"Workday returned no event for {identifier!r}. Check the id from the "
                "write's result."
            )
        return parsed

    registrar.add(
        workday_get_event,
        name="workday_get_event",
        title="Get a Workday business process event",
        description=(
            "Look up a business process event by the id a write returned: its state, "
            "what it targeted, who started it and which approval tasks it is waiting "
            "on. Use this after a write submitted without auto_complete, where the "
            "worker's data does not change until the tenant's approvers finish, and "
            "after any write that timed out, to find out whether it committed before "
            "considering sending it again."
        ),
        read_only=True,
        idempotent=True,
    )


async def _direct_enrollments(
    client: WorkdayClient, worker_wid: str
) -> list[dict[str, Any]]:
    """The worker's enrollments by reference, or none if the tenant cannot resolve it.

    Some tenants answer a worker WID used as an enrollment reference with a
    validation fault rather than an empty response. The capped scan takes over
    either way.
    """
    try:
        result = await client.call(
            SERVICE_CASH_MANAGEMENT,
            payment_elections.GET_ENROLLMENTS,
            payment_elections.build_get_enrollments_by_reference(worker_wid),
            is_read=True,
        )
    except WorkdayFaultError as exc:
        if not exc.validation_errors:
            raise
        return []
    return payment_elections.enrollments_in(
        result.response(payment_elections.GET_ENROLLMENTS)
    )


async def _scan_enrollments(
    client: WorkdayClient, worker_wid: str, max_pages: int
) -> tuple[list[dict[str, Any]], bool]:
    """Fall back to paging enrollments when the direct reference lookup misses.

    Some tenants do not resolve a worker WID as an enrollment reference. The scan
    is capped, and an exhausted cap is reported rather than read as "none".
    """
    from .references import extract_ids

    page = 1
    moment = entry_moment()
    while page <= max_pages:
        request = payment_elections.build_get_enrollments_page(page)
        result = await client.call(
            SERVICE_CASH_MANAGEMENT,
            payment_elections.GET_ENROLLMENTS,
            at_entry_moment(request, moment),
            is_read=True,
        )
        response = result.response(payment_elections.GET_ENROLLMENTS)
        entries = payment_elections.enrollments_in(response)
        if not entries:
            return [], True

        for entry in entries:
            data = entry.get("Payment_Election_Enrollment_Data")
            if not isinstance(data, dict):
                continue
            if worker_wid in extract_ids(data.get("Role_ID_Reference")).values():
                return [entry], True

        current_page, total_pages = workers.page_info(response)
        if total_pages and current_page >= total_pages:
            return [], True
        if not total_pages and len(entries) < payment_elections.PAGE_SIZE:
            return [], True
        page += 1

    return [], False
