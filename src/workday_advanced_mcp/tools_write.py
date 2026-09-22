"""Write tools: the Workday business processes a standard integration lacks.

Three rules hold across all of them.

**Nothing is guessed.** Reference ids are tenant-defined, so every write takes
resolved ids and fails loudly rather than approximating one.

**Auto-complete is off by default.** A business process submitted without it
follows the tenant's own approval routing, which means the worker's data has not
changed when the call returns. Verification therefore reports the *event*, not a
re-read of the worker, unless auto-complete was used.

**Replace-all operations are made safe explicitly.** Change_Organization_
Assignments and Submit_Payment_Election_Enrollment both replace what they are
given. Organization assignments can be merged, because their ids are readable.
Payment elections cannot: account numbers come back masked, so an election that
would be lost is reported and the caller has to confirm rather than have this
server pretend it carried something forward.
"""

from __future__ import annotations

from typing import Any

from .client import (
    SERVICE_CASH_MANAGEMENT,
    SERVICE_RECRUITING,
    SERVICE_STAFFING,
    WorkdayClient,
    get_client,
)
from .config import ENV_ALLOW_AUTO_COMPLETE, load_settings
from .operations import payment_elections, recruiting, staffing
from .operations.common import event_reference_from
from .references import (
    EMPLOYEE_ID,
    EVENT_CLASSIFICATION_SUBCATEGORY_ID,
    TERMINATION_SUBCATEGORY_ID,
)
from .registry import ToolRegistrar
from .resolve import WorkerNotFound, assignments_by_type, describe_worker, fetch_worker

MAX_BATCH = 25


def _check_auto_complete(auto_complete: bool) -> None:
    """Auto-complete bypasses the tenant's approvals, so it is opt-in per deployment."""
    if auto_complete and not load_settings().allow_auto_complete:
        raise ValueError(
            "auto_complete is not permitted on this connector. Submitting without it "
            "lets the tenant's own approval routing run, which is the safe default. To "
            f"allow it for a supervised bulk load, set {ENV_ALLOW_AUTO_COMPLETE}=true."
        )


def _require_batch(
    changes: list[dict[str, Any]] | None, what: str
) -> list[dict[str, Any]]:
    entries = [c for c in (changes or []) if isinstance(c, dict)]
    if not entries:
        raise ValueError(f"No {what} were supplied.")
    if len(entries) > MAX_BATCH:
        raise ValueError(
            f"{len(entries)} {what} is more than this tool accepts at once "
            f"({MAX_BATCH}). Split them into smaller batches."
        )
    return entries


async def _submit(
    client: WorkdayClient,
    service: str,
    operation: str,
    request: dict[str, Any],
    *,
    auto_complete: bool,
    verify: Any = None,
) -> dict[str, Any]:
    """Send one business process and describe what came back."""
    result = await client.call(service, operation, request, is_read=False)
    response = result.response(operation)
    event = event_reference_from(response)

    submitted: dict[str, Any] = {
        "submitted": True,
        "operation": operation,
        "auto_complete": auto_complete,
        "event": event,
        "request_xml": result.request_xml,
    }

    if verify is None:
        submitted["verification"] = _verification_note(auto_complete, event)
        return submitted

    try:
        submitted["verification"] = await verify()
    except Exception as exc:
        submitted["verification_error"] = (
            f"The write succeeded but reading it back failed: {exc}"
        )
    return submitted


def _verification_note(auto_complete: bool, event: dict[str, Any]) -> dict[str, Any]:
    if auto_complete:
        return {"verified": False, "note": "No read-back was requested."}
    return {
        "verified": False,
        "note": (
            "Submitted without auto_complete, so the tenant's approval routing still "
            "has this event and the worker's data will not change until it completes. "
            "Check progress with workday_get_event"
            + (f" using event id {event.get('id')}." if event.get("id") else ".")
        ),
    }


# --------------------------------------------------------------------------
# Planners. Each resolves references and works out what would change; the write
# tool submits the plan, workday_preview_write returns it untouched.
# --------------------------------------------------------------------------


async def plan_change_organization_assignments(
    client: WorkdayClient,
    *,
    changes: list[dict[str, Any]],
    effective_date: str,
    merge_with_current: bool = True,
) -> list[dict[str, Any]]:
    """Work out each worker's new organization assignment set."""
    if not effective_date:
        raise ValueError("effective_date is required (YYYY-MM-DD).")

    plans: list[dict[str, Any]] = []
    for change in changes:
        worker_id = str(change.get("worker_id") or "").strip()
        if not worker_id:
            raise ValueError("Each change needs a worker_id.")
        id_type = str(change.get("worker_id_type") or EMPLOYEE_ID)
        requested = change.get("assignments")
        if not isinstance(requested, dict) or not requested:
            raise ValueError(
                f"Change for {worker_id} has no assignments. Supply, for example, "
                '{"Cost_Center": ["CC_100"]}.'
            )

        normalised = {
            key: [str(v) for v in (value if isinstance(value, list) else [value]) if v]
            for key, value in requested.items()
        }
        unknown = set(normalised) - set(staffing.ASSIGNMENT_ELEMENTS)
        if unknown:
            raise ValueError(
                "Cannot assign organization type(s) "
                + ", ".join(sorted(unknown))
                + f". Supported: {', '.join(staffing.ASSIGNABLE_TYPES)}. A supervisory "
                "organization move is a job change - use workday_change_job."
            )

        worker = await fetch_worker(client, worker_id, id_type, detailed=True)
        position = worker.get("position") or {}
        position_id = str(position.get("position_id") or "").strip()
        if not position_id:
            raise ValueError(
                f"{describe_worker(worker)} has no primary position id, which "
                "Change_Organization_Assignments addresses. Check the worker is active "
                "and holds a position."
            )

        current = assignments_by_type(worker)
        # Only types Workday can assign here matter for the merge.
        current_assignable = {
            key: values
            for key, values in current.items()
            if key in staffing.ASSIGNMENT_ELEMENTS
        }
        merged = dict(current_assignable) if merge_with_current else {}
        merged.update(normalised)

        diff = {
            key: {"from": current_assignable.get(key, []), "to": values}
            for key, values in merged.items()
            if current_assignable.get(key, []) != values
        }
        dropped = sorted(set(current_assignable) - set(merged))

        plans.append(
            {
                "worker": worker,
                "worker_id": worker_id,
                "worker_id_type": id_type,
                "position_id": position_id,
                "effective_date": effective_date,
                "current_assignments": current_assignable,
                "new_assignments": merged,
                "changes": diff,
                "assignments_dropped": dropped,
                "no_change": not diff and not dropped,
            }
        )
    return plans


async def plan_change_job(
    client: WorkdayClient,
    *,
    changes: list[dict[str, Any]],
    effective_date: str,
    reason_id: str,
    reason_id_type: str = EVENT_CLASSIFICATION_SUBCATEGORY_ID,
) -> list[dict[str, Any]]:
    """Resolve each worker and describe the job change to be submitted."""
    if not effective_date:
        raise ValueError("effective_date is required (YYYY-MM-DD).")
    if not reason_id:
        raise ValueError(
            "reason_id is required. Resolve one with workday_list_reference_ids using "
            f"{EVENT_CLASSIFICATION_SUBCATEGORY_ID}."
        )

    plans: list[dict[str, Any]] = []
    for change in changes:
        worker_id = str(change.get("worker_id") or "").strip()
        if not worker_id:
            raise ValueError("Each change needs a worker_id.")
        id_type = str(change.get("worker_id_type") or EMPLOYEE_ID)
        worker = await fetch_worker(client, worker_id, id_type, detailed=True)

        target = {
            key: str(change.get(key) or "").strip()
            for key in (
                "supervisory_organization_id",
                "job_profile_id",
                "location_id",
                "business_title",
                "position_title",
                "position_time_type_id",
                "employee_type_id",
            )
        }
        target = {key: value for key, value in target.items() if value}
        if not target:
            raise ValueError(
                f"Change for {worker_id} says nothing to change. Supply at least "
                f"one of "
                "supervisory_organization_id, job_profile_id, location_id or "
                "business_title."
            )

        position = worker.get("position") or {}
        current = {
            "supervisory_organization_id": (
                worker.get("supervisory_organization") or {}
            ).get("id", ""),
            "job_profile_id": (position.get("job_profile") or {}).get("id", ""),
            "location_id": (position.get("location") or {}).get("id", ""),
            "business_title": position.get("business_title", ""),
            "position_title": position.get("position_title", ""),
        }
        diff = {
            key: {"from": current.get(key, ""), "to": value}
            for key, value in target.items()
            if current.get(key, "") != value
        }

        plans.append(
            {
                "worker": worker,
                "worker_id": worker_id,
                "worker_id_type": id_type,
                "position_id": position.get("position_id", ""),
                "effective_date": str(change.get("effective_date") or effective_date),
                "reason_id": str(change.get("reason_id") or reason_id),
                "reason_id_type": reason_id_type,
                "target": target,
                "changes": diff,
                "no_change": not diff,
            }
        )
    return plans


async def plan_terminate_employee(
    client: WorkdayClient,
    *,
    worker_id: str,
    worker_id_type: str = EMPLOYEE_ID,
    termination_date: str,
    primary_reason_id: str,
) -> dict[str, Any]:
    if not termination_date:
        raise ValueError("termination_date is required (YYYY-MM-DD).")
    if not primary_reason_id:
        raise ValueError(
            "primary_reason_id is required. Resolve one with "
            "workday_list_reference_ids "
            f"using {TERMINATION_SUBCATEGORY_ID}."
        )

    worker = await fetch_worker(client, worker_id, worker_id_type, detailed=True)
    status = worker.get("status") or {}
    if status.get("terminated"):
        raise ValueError(
            f"{describe_worker(worker)} is already terminated in Workday"
            + (
                f" (termination date {status.get('termination_date')})."
                if status.get("termination_date")
                else "."
            )
        )
    return {
        "worker": worker,
        "termination_date": termination_date,
        "primary_reason_id": primary_reason_id,
        "currently_active": bool(status.get("active", True)),
    }


async def plan_set_payment_elections(
    client: WorkdayClient,
    *,
    worker_id: str,
    worker_id_type: str = EMPLOYEE_ID,
    country: str,
    currency: str,
    rule_groups: list[dict[str, Any]],
) -> dict[str, Any]:
    """Describe the replacement, including every election that would be lost.

    Workday replaces a worker's whole election set. Existing account numbers come
    back masked, so they cannot be silently carried forward; instead every current
    election that the new set does not reproduce is listed as ``removed``.
    """
    from .tools_read import _scan_enrollments

    if not country or not currency:
        raise ValueError(
            "country (ISO alpha-2) and currency (for example GBP) are required."
        )
    if not rule_groups:
        raise ValueError(
            "rule_groups is the complete set of elections the worker should end up "
            "with. Submitting an empty set would delete every election they have."
        )

    worker = await fetch_worker(client, worker_id, worker_id_type, detailed=False)
    worker_wid = worker.get("wid") or ""
    if not worker_wid:
        raise ValueError(
            f"Workday returned no WID for {worker_id!r}, which payment elections are "
            "keyed by."
        )

    request = payment_elections.build_get_enrollments_by_reference(worker_wid)
    result = await client.call(
        SERVICE_CASH_MANAGEMENT,
        payment_elections.GET_ENROLLMENTS,
        request,
        is_read=True,
    )
    entries = payment_elections.enrollments_in(
        result.response(payment_elections.GET_ENROLLMENTS)
    )
    if not entries:
        entries, _complete = await _scan_enrollments(client, worker_wid, 5)

    current = [payment_elections.parse_enrollment(entry) for entry in entries]
    current_fingerprints = {
        election["bank_account"].get("account_fingerprint", "")
        for enrollment in current
        for group in enrollment.get("rule_groups", [])
        for election in group.get("elections", [])
        if election.get("bank_account")
    }
    current_fingerprints.discard("")

    from .redact import account_fingerprint

    new_fingerprints = {
        account_fingerprint(
            (election.get("bank_account") or {}).get("account_number")
            or (election.get("bank_account") or {}).get("iban")
        )
        for group in rule_groups
        for election in group.get("elections") or []
    }
    new_fingerprints.discard("")

    removed = sorted(current_fingerprints - new_fingerprints)
    return {
        "worker": worker,
        "worker_wid": worker_wid,
        "country": country,
        "currency": currency,
        "current_enrollments": current,
        "accounts_removed": removed,
        "replaces_existing": bool(removed),
        "no_change": bool(current_fingerprints)
        and current_fingerprints == new_fingerprints,
    }


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------


def register_write_tools(registrar: ToolRegistrar) -> None:
    """Attach the write tools, plus the preview that should precede them."""

    async def workday_change_organization_assignments(
        changes: list[dict[str, Any]],
        effective_date: str,
        merge_with_current: bool = True,
        auto_complete: bool = False,
        comment: str | None = None,
    ) -> Any:
        _check_auto_complete(auto_complete)
        client = get_client()
        entries = _require_batch(changes, "organization assignment changes")
        plans = await plan_change_organization_assignments(
            client,
            changes=entries,
            effective_date=effective_date,
            merge_with_current=merge_with_current,
        )

        results: list[dict[str, Any]] = []
        for plan in plans:
            if plan["no_change"]:
                results.append(
                    {
                        "worker_id": plan["worker_id"],
                        "no_change": True,
                        "note": "Workday already has these organization assignments.",
                    }
                )
                continue

            request = staffing.build_change_organization_assignments_request(
                position_id=plan["position_id"],
                effective_date=plan["effective_date"],
                assignments=plan["new_assignments"],
                worker_id=plan["worker_id"],
                worker_id_type=plan["worker_id_type"],
                auto_complete=auto_complete,
                comment=comment,
            )

            async def verify(plan: dict[str, Any] = plan) -> dict[str, Any]:
                worker = await fetch_worker(
                    client, plan["worker_id"], plan["worker_id_type"], detailed=True
                )
                return {
                    "verified": True,
                    "organization_assignments": worker.get(
                        "organization_assignments", []
                    ),
                }

            outcome = await _submit(
                client,
                SERVICE_STAFFING,
                staffing.CHANGE_ORGANIZATION_ASSIGNMENTS,
                request,
                auto_complete=auto_complete,
                verify=verify if auto_complete else None,
            )
            outcome.update(
                {
                    "worker_id": plan["worker_id"],
                    "position_id": plan["position_id"],
                    "changes": plan["changes"],
                }
            )
            results.append(outcome)

        return {"count": len(results), "results": results}

    registrar.add(
        workday_change_organization_assignments,
        name="workday_change_organization_assignments",
        title="Change Workday organization assignments",
        description=(
            "Assign a worker's position to a cost centre, company, region or business "
            "unit. This is not the reporting structure: moving a worker to a different "
            "supervisory organization is a job change, so use workday_change_job for "
            "that. Takes up to 25 changes at once, each "
            '{"worker_id": "21001", "assignments": {"Cost_Center": ["CC_100"]}}, '
            "with a "
            "shared effective_date. Workday replaces the assignments for every "
            "organization type in the request, so by default the worker's current "
            "assignments are read and merged in first; setting merge_with_current to "
            "false sends only what you passed and risks clearing the rest. A worker "
            "whose assignments already match is reported as no_change and not "
            "submitted. Resolve cost centre ids with workday_list_organizations."
        ),
        read_only=False,
    )

    async def workday_change_job(
        changes: list[dict[str, Any]],
        effective_date: str,
        reason_id: str,
        auto_complete: bool = False,
        comment: str | None = None,
    ) -> Any:
        _check_auto_complete(auto_complete)
        client = get_client()
        entries = _require_batch(changes, "job changes")
        plans = await plan_change_job(
            client, changes=entries, effective_date=effective_date, reason_id=reason_id
        )

        results: list[dict[str, Any]] = []
        for plan in plans:
            if plan["no_change"]:
                results.append(
                    {
                        "worker_id": plan["worker_id"],
                        "no_change": True,
                        "note": "Workday already has these job details.",
                    }
                )
                continue

            target = plan["target"]
            request = staffing.build_change_job_request(
                worker_id=plan["worker_id"],
                worker_id_type=plan["worker_id_type"],
                effective_date=plan["effective_date"],
                reason_id=plan["reason_id"],
                reason_id_type=plan["reason_id_type"],
                position_id=plan["position_id"] or None,
                supervisory_organization_id=target.get("supervisory_organization_id"),
                job_profile_id=target.get("job_profile_id"),
                location_id=target.get("location_id"),
                business_title=target.get("business_title"),
                position_title=target.get("position_title"),
                position_time_type_id=target.get("position_time_type_id"),
                employee_type_id=target.get("employee_type_id"),
                auto_complete=auto_complete,
                comment=comment,
            )

            async def verify(plan: dict[str, Any] = plan) -> dict[str, Any]:
                worker = await fetch_worker(
                    client, plan["worker_id"], plan["worker_id_type"], detailed=True
                )
                return {
                    "verified": True,
                    "supervisory_organization": worker.get(
                        "supervisory_organization", {}
                    ),
                    "position": worker.get("position", {}),
                }

            outcome = await _submit(
                client,
                SERVICE_STAFFING,
                staffing.CHANGE_JOB,
                request,
                auto_complete=auto_complete,
                verify=verify if auto_complete else None,
            )
            outcome.update({"worker_id": plan["worker_id"], "changes": plan["changes"]})
            results.append(outcome)

        return {"count": len(results), "results": results}

    registrar.add(
        workday_change_job,
        name="workday_change_job",
        title="Change a Workday job",
        description=(
            "Move workers to a different supervisory organization, job profile, "
            "location or business title. A supervisory organization move is this tool, "
            "not workday_change_organization_assignments, which only covers cost "
            "centre "
            "and company. Takes up to 25 changes at once, each "
            '{"worker_id": "21001", "supervisory_organization_id": "SUP-3"}, with a '
            "shared effective_date and reason_id. The reason is tenant-defined: "
            "resolve "
            "it with workday_list_reference_ids using "
            "Event_Classification_Subcategory_ID. A worker whose job already "
            "matches is "
            "reported as no_change and not submitted."
        ),
        read_only=False,
    )

    async def workday_terminate_employee(
        worker_id: str,
        termination_date: str,
        primary_reason_id: str,
        worker_id_type: str = EMPLOYEE_ID,
        last_day_of_work: str | None = None,
        pay_through_date: str | None = None,
        resignation_date: str | None = None,
        notification_date: str | None = None,
        regrettable: bool | None = None,
        eligible_for_hire: bool | None = None,
        auto_complete: bool = False,
        comment: str | None = None,
    ) -> Any:
        _check_auto_complete(auto_complete)
        client = get_client()
        plan = await plan_terminate_employee(
            client,
            worker_id=worker_id,
            worker_id_type=worker_id_type,
            termination_date=termination_date,
            primary_reason_id=primary_reason_id,
        )

        request = staffing.build_terminate_employee_request(
            worker_id=worker_id,
            worker_id_type=worker_id_type,
            termination_date=termination_date,
            primary_reason_id=primary_reason_id,
            last_day_of_work=last_day_of_work,
            pay_through_date=pay_through_date,
            resignation_date=resignation_date,
            notification_date=notification_date,
            regrettable=regrettable,
            eligible_for_hire=eligible_for_hire,
            auto_complete=auto_complete,
            comment=comment,
        )

        async def verify() -> dict[str, Any]:
            worker = await fetch_worker(
                client, worker_id, worker_id_type, detailed=True
            )
            return {"verified": True, "status": worker.get("status", {})}

        outcome = await _submit(
            client,
            SERVICE_STAFFING,
            staffing.TERMINATE_EMPLOYEE,
            request,
            auto_complete=auto_complete,
            verify=verify if auto_complete else None,
        )
        outcome.update({"worker": plan["worker"], "termination_date": termination_date})
        return outcome

    registrar.add(
        workday_terminate_employee,
        name="workday_terminate_employee",
        title="Terminate a Workday employee",
        description=(
            "End a worker's employment. This cannot be undone from these tools: "
            "reversing it means rescinding the event in Workday. A worker Workday "
            "already shows as terminated is refused rather than terminated twice. The "
            "primary reason is tenant-defined - resolve it with "
            "workday_list_reference_ids using Termination_Subcategory_ID. Confirm the "
            "worker, the date and the reason with a person before calling this."
        ),
        read_only=False,
        destructive=True,
    )

    async def workday_create_pre_hire(
        first_name: str,
        last_name: str,
        email: str | None = None,
        phone: str | None = None,
        country_iso: str = "US",
        force: bool = False,
    ) -> Any:
        client = get_client()
        if not (first_name and last_name):
            raise ValueError("first_name and last_name are required.")
        if not (email or phone):
            raise ValueError(
                "Workday needs at least one contact method to create a pre-hire: pass "
                "an email or a phone number."
            )

        if email and not force:
            existing = await client.call(
                SERVICE_RECRUITING,
                recruiting.GET_APPLICANTS,
                recruiting.build_get_applicants_request(
                    1, recruiting.PAGE_SIZE, email=email
                ),
                is_read=True,
            )
            matches = [
                entry
                for entry in recruiting.parse_applicants(
                    existing.response(recruiting.GET_APPLICANTS)
                )
                if entry.get("email", "").lower() == email.lower()
            ]
            if matches:
                return {
                    "created": False,
                    "duplicate": True,
                    "existing": matches,
                    "note": (
                        f"Workday already has a pre-hire with the email {email}. Hire "
                        "that record with workday_hire_employee, or pass force to "
                        "create a second one anyway."
                    ),
                }

        request = recruiting.build_put_applicant_request(
            first_name=first_name,
            last_name=last_name,
            country_iso=country_iso,
            email=email,
            phone=phone,
        )
        result = await client.call(
            SERVICE_RECRUITING, recruiting.PUT_APPLICANT, request, is_read=False
        )
        response = result.response(recruiting.PUT_APPLICANT)
        return {
            "created": True,
            "pre_hire_wid": recruiting.applicant_wid_from_response(response),
            "request_xml": result.request_xml,
        }

    registrar.add(
        workday_create_pre_hire,
        name="workday_create_pre_hire",
        title="Create a Workday pre-hire",
        description=(
            "Create the pre-hire (applicant) record that a hire needs, returning the "
            "WID workday_hire_employee takes. Workday requires at least an email or a "
            "phone number. An email that already belongs to a pre-hire is reported "
            "as a "
            "duplicate and nothing is created, so a re-run does not produce two "
            "records "
            "for one person; pass force to override that."
        ),
        read_only=False,
    )

    async def workday_hire_employee(
        organization_id: str,
        hire_date: str,
        pre_hire_wid: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        email: str | None = None,
        country_iso: str = "US",
        employee_id: str | None = None,
        job_profile_id: str | None = None,
        location_id: str | None = None,
        position_time_type_id: str | None = None,
        employee_type_id: str | None = None,
        job_requisition_id: str | None = None,
        position_title: str | None = None,
        business_title: str | None = None,
        auto_complete: bool = False,
        comment: str | None = None,
        force: bool = False,
    ) -> Any:
        _check_auto_complete(auto_complete)
        client = get_client()
        if not organization_id or not hire_date:
            raise ValueError(
                "organization_id (the supervisory organization) and hire_date are "
                "required."
            )

        if employee_id and not force:
            try:
                existing = await fetch_worker(
                    client, employee_id, EMPLOYEE_ID, detailed=True
                )
            except WorkerNotFound:
                existing = None
            if existing is not None:
                status = existing.get("status") or {}
                if status.get("active", True):
                    return {
                        "submitted": False,
                        "duplicate": True,
                        "existing": existing,
                        "note": (
                            f"Workday already has an active worker with employee id "
                            f"{employee_id}. Hiring again would create a second "
                            f"record. "
                            "Pass force only if a second employment really is intended."
                        ),
                    }

        request = staffing.build_hire_employee_request(
            organization_id=organization_id,
            hire_date=hire_date,
            pre_hire_wid=pre_hire_wid,
            first_name=first_name,
            last_name=last_name,
            country_iso=country_iso,
            email=email,
            job_requisition_id=job_requisition_id,
            employee_type_id=employee_type_id,
            job_profile_id=job_profile_id,
            location_id=location_id,
            position_time_type_id=position_time_type_id,
            position_title=position_title,
            business_title=business_title,
            employee_id=employee_id,
            auto_complete=auto_complete,
            comment=comment,
        )

        async def verify() -> dict[str, Any]:
            if not employee_id:
                return {"verified": False, "note": "No employee_id to read back by."}
            worker = await fetch_worker(client, employee_id, EMPLOYEE_ID, detailed=True)
            return {"verified": True, "worker": worker}

        return await _submit(
            client,
            SERVICE_STAFFING,
            staffing.HIRE_EMPLOYEE,
            request,
            auto_complete=auto_complete,
            verify=verify if (auto_complete and employee_id) else None,
        )

    registrar.add(
        workday_hire_employee,
        name="workday_hire_employee",
        title="Hire a Workday employee",
        description=(
            "Hire somebody into a supervisory organization. Pass pre_hire_wid from "
            "workday_list_pre_hires or workday_create_pre_hire; passing a name instead "
            "creates the pre-hire inline. When employee_id is given and an active "
            "worker already holds it, the hire is refused rather than creating a "
            "second "
            "record. If the tenant's hire process requires compensation or a pay "
            "group, "
            "leave auto_complete off so its own routing collects them."
        ),
        read_only=False,
    )

    async def workday_set_payment_elections(
        worker_id: str,
        country: str,
        currency: str,
        rule_groups: list[dict[str, Any]],
        worker_id_type: str = EMPLOYEE_ID,
        confirm_replaces_existing: bool = False,
        retain_unused_bank_accounts: bool = True,
        auto_complete: bool = False,
        comment: str | None = None,
    ) -> Any:
        _check_auto_complete(auto_complete)
        client = get_client()
        plan = await plan_set_payment_elections(
            client,
            worker_id=worker_id,
            worker_id_type=worker_id_type,
            country=country,
            currency=currency,
            rule_groups=rule_groups,
        )

        if plan["no_change"]:
            return {
                "submitted": False,
                "no_change": True,
                "note": "Workday already has exactly these payment elections.",
            }
        if plan["replaces_existing"] and not confirm_replaces_existing:
            return {
                "submitted": False,
                "requires_confirmation": True,
                "accounts_removed": plan["accounts_removed"],
                "current_enrollments": plan["current_enrollments"],
                "note": (
                    "Submitting payment elections replaces the worker's entire "
                    "set, and "
                    f"{len(plan['accounts_removed'])} existing account(s) are not in "
                    "the set you passed, so they would be removed. Existing account "
                    "numbers are masked and cannot be carried forward automatically. "
                    "Include every election the worker should keep, then call again "
                    "with confirm_replaces_existing set to true."
                ),
            }

        request = payment_elections.build_submit_enrollment_request(
            worker_wid=plan["worker_wid"],
            country=country,
            currency=currency,
            rule_groups=rule_groups,
            retain_unused_bank_accounts=retain_unused_bank_accounts,
            auto_complete=auto_complete,
            comment=comment,
        )
        outcome = await _submit(
            client,
            SERVICE_CASH_MANAGEMENT,
            payment_elections.SUBMIT_ENROLLMENT,
            request,
            auto_complete=auto_complete,
        )
        outcome.update(
            {"worker": plan["worker"], "accounts_removed": plan["accounts_removed"]}
        )
        return outcome

    registrar.add(
        workday_set_payment_elections,
        name="workday_set_payment_elections",
        title="Set Workday payment elections",
        description=(
            "Replace a worker's payment elections with the complete set passed in "
            "rule_groups. Workday has no way to change one election in place: a "
            "submission replaces everything the worker has, so anything you leave out "
            "is deleted. Read the current set with workday_get_payment_elections "
            "first. "
            "Existing account numbers come back masked and cannot be carried forward "
            "automatically, so when the new set would drop an account the call is "
            "refused and lists what would go; supply those elections in full, then "
            "call "
            "again with confirm_replaces_existing. Each rule group needs "
            "payment_election_rule_id and elections, each election a payment_type_id, "
            "one of distribution_balance / distribution_amount / "
            "distribution_percentage, "
            "and a bank_account with account_number and bank_id_number, or iban."
        ),
        read_only=False,
        destructive=True,
    )

    # Preview registers alongside the writes: it exists to precede one, so a
    # read-only deployment has nothing for it to preview.
    async def workday_preview_write(
        operation: str, arguments: dict[str, Any] | None = None
    ) -> Any:
        client = get_client()
        args = dict(arguments or {})
        name = (operation or "").strip()

        if name == "workday_change_organization_assignments":
            plans = await plan_change_organization_assignments(
                client,
                changes=_require_batch(
                    args.get("changes"), "organization assignment changes"
                ),
                effective_date=str(args.get("effective_date") or ""),
                merge_with_current=bool(args.get("merge_with_current", True)),
            )
            return {"operation": name, "would_submit": True, "plans": plans}

        if name == "workday_change_job":
            plans = await plan_change_job(
                client,
                changes=_require_batch(args.get("changes"), "job changes"),
                effective_date=str(args.get("effective_date") or ""),
                reason_id=str(args.get("reason_id") or ""),
            )
            return {"operation": name, "would_submit": True, "plans": plans}

        if name == "workday_terminate_employee":
            plan = await plan_terminate_employee(
                client,
                worker_id=str(args.get("worker_id") or ""),
                worker_id_type=str(args.get("worker_id_type") or EMPLOYEE_ID),
                termination_date=str(args.get("termination_date") or ""),
                primary_reason_id=str(args.get("primary_reason_id") or ""),
            )
            return {"operation": name, "would_submit": True, "plan": plan}

        if name == "workday_set_payment_elections":
            plan = await plan_set_payment_elections(
                client,
                worker_id=str(args.get("worker_id") or ""),
                worker_id_type=str(args.get("worker_id_type") or EMPLOYEE_ID),
                country=str(args.get("country") or ""),
                currency=str(args.get("currency") or ""),
                rule_groups=list(args.get("rule_groups") or []),
            )
            return {"operation": name, "would_submit": True, "plan": plan}

        raise ValueError(
            f"Cannot preview {operation!r}. Previewable operations: "
            "workday_change_organization_assignments, workday_change_job, "
            "workday_terminate_employee, workday_set_payment_elections."
        )

    registrar.add(
        workday_preview_write,
        name="workday_preview_write",
        title="Preview a Workday write",
        description=(
            "Work out what a write would do without sending anything to Workday. Pass "
            "the name of the write tool and the arguments you would give it; the "
            "result "
            "resolves the worker and every reference, and states the current value and "
            "the new value for each field that would change, or that nothing would "
            "change. Use it before any write so the person approving sees what will "
            "happen. Covers workday_change_organization_assignments, "
            "workday_change_job, workday_terminate_employee and "
            "workday_set_payment_elections."
        ),
        read_only=True,
        idempotent=True,
    )
