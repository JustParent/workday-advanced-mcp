"""Lifecycle tools composed from narrow, documented Workday operations."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from .client import (
    SERVICE_HUMAN_RESOURCES,
    SERVICE_INTEGRATIONS,
    SERVICE_STAFFING,
    WorkdayClient,
    get_client,
)
from .errors import WorkdayNotFoundError
from .operations import events, lifecycle, organizations, workers
from .pagination import at_entry_moment, entry_moment
from .references import reference
from .registry import ToolRegistrar
from .resolve import fetch_worker
from .tools_write import _check_auto_complete, _submit, is_preview
from .validation import iso_date


async def fetch_organization(
    client: WorkdayClient,
    organization_id: str,
    effective_date: str | None = None,
    required: bool = True,
) -> dict[str, Any]:
    try:
        result = await client.call(
            SERVICE_HUMAN_RESOURCES,
            organizations.GET_ORGANIZATIONS,
            organizations.build_get_organization(
                organization_id, as_of_effective_date=effective_date
            ),
            is_read=True,
        )
    except WorkdayNotFoundError:
        found: list[dict[str, Any]] = []
    else:
        found = organizations.parse_organizations(
            result.response(organizations.GET_ORGANIZATIONS), "Supervisory"
        )
    if len(found) > 1:
        raise ValueError("Organization reference returned more than one match.")
    if not found:
        if required:
            raise ValueError(
                f"Organization {organization_id!r} was not found "
                "or is not visible to the ISU."
            )
        return {}
    return found[0]


async def check_parent(
    client: WorkdayClient, organization_id: str, superior_id: str, effective_date: str
) -> dict[str, Any]:
    parent = await fetch_organization(client, superior_id, effective_date)
    if parent.get("inactive") or parent.get("organization_type") not in (
        "Supervisory",
    ):
        raise ValueError("The superior must be an active supervisory organization.")
    ancestor = parent
    seen = {organization_id}
    for _ in range(100):
        identifier = str(ancestor.get("reference_id") or ancestor.get("id") or "")
        if identifier in seen:
            raise ValueError("The proposed hierarchy contains a cycle.")
        seen.add(identifier)
        superior = ancestor.get("superior") or {}
        if not superior:
            return parent
        if superior.get("id_type") != "Organization_Reference_ID":
            raise ValueError(
                "Ancestor has no organization reference ID; cannot prove "
                "hierarchy safety."
            )
        ancestor = await fetch_organization(client, superior["id"], effective_date)
    raise ValueError(
        "Hierarchy exceeds the 100-level safety limit; no write was submitted."
    )


def register_lifecycle_reads(registrar: ToolRegistrar) -> None:
    async def workday_get_organization(
        organization_id: str, as_of_effective_date: str | None = None
    ) -> Any:
        return await fetch_organization(
            get_client(), organization_id, as_of_effective_date
        )

    async def workday_get_manager_organizations(
        worker_id: str,
        worker_id_type: str = "Employee_ID",
        as_of_effective_date: str | None = None,
        max_pages: int = 20,
    ) -> Any:
        if not 1 <= max_pages <= 100:
            raise ValueError("max_pages must be between 1 and 100.")
        client = get_client()
        worker = await fetch_worker(
            client, worker_id, worker_id_type, as_of_effective_date=as_of_effective_date
        )
        identifiers = {worker.get("wid"), worker.get("worker_id")}
        found: list[dict[str, Any]] = []
        complete = False
        moment = entry_moment()
        for page in range(1, max_pages + 1):
            result = await client.call(
                SERVICE_HUMAN_RESOURCES,
                organizations.GET_ORGANIZATIONS,
                at_entry_moment(
                    organizations.build_get_organizations_request(
                        "Supervisory", page, as_of_effective_date=as_of_effective_date
                    ),
                    moment,
                ),
                is_read=True,
            )
            response = result.response(organizations.GET_ORGANIZATIONS)
            entries = organizations.parse_organizations(response, "Supervisory")
            for entry in entries:
                candidates = [entry.get("manager") or {}, *entry.get("leaders", [])]
                if any(
                    identifiers.intersection(ref.get("ids", {}).values())
                    for ref in candidates
                ):
                    found.append(entry)
            _, pages = workers.page_info(response)
            if (pages and page >= pages) or (
                not pages and len(entries) < organizations.PAGE_SIZE
            ):
                complete = True
                break
        return {
            "worker": worker,
            "managed_organizations": found,
            "scan_complete": complete,
            "membership_organization": worker.get("supervisory_organization", {}),
        }

    async def workday_list_organization_workers(
        organization_id: str,
        as_of_effective_date: str | None = None,
        include_subordinates: bool = False,
        page: int = 1,
        count: int = 100,
        as_of_entry_datetime: str | None = None,
    ) -> Any:
        if page < 1 or not 1 <= count <= 100:
            raise ValueError("page must be positive and count between 1 and 100.")
        request = workers.build_scan_request(
            page, count, response_group=workers.GROUP_FULL
        )
        request["Request_Criteria"] = {
            "Organization_Reference": reference(
                "Organization_Reference_ID", organization_id
            ),
            "Include_Subordinate_Organizations": include_subordinates,
            "Exclude_Inactive_Workers": True,
        }
        if as_of_effective_date:
            request["Response_Filter"] = {
                "As_Of_Effective_Date": iso_date(as_of_effective_date),
                "Page": page,
                "Count": count,
            }
        moment = entry_moment(as_of_entry_datetime)
        result = await get_client().call(
            SERVICE_HUMAN_RESOURCES,
            workers.OPERATION,
            at_entry_moment(request, moment),
            is_read=True,
        )
        response = result.response(workers.OPERATION)
        return {
            "workers": [
                workers.summarize_worker(w) for w in workers.workers_in(response)
            ],
            "page": page,
            "total_pages": workers.page_info(response)[1],
            "as_of_entry_datetime": moment,
        }

    async def workday_list_positions(
        organization_id: str | None = None,
        position_id: str | None = None,
        as_of_effective_date: str | None = None,
        page: int = 1,
        count: int = 100,
        exclude_filled: bool = True,
        as_of_entry_datetime: str | None = None,
    ) -> Any:
        if page < 1 or not 1 <= count <= 100:
            raise ValueError("page must be positive and count between 1 and 100.")
        request = lifecycle.build_get_positions(
            organization_id=organization_id,
            position_id=position_id,
            as_of_effective_date=as_of_effective_date,
            page=page,
            count=count,
            exclude_filled=exclude_filled,
        )
        moment = entry_moment(as_of_entry_datetime)
        try:
            result = await get_client().call(
                SERVICE_STAFFING,
                "Get_Positions",
                at_entry_moment(request, moment),
                is_read=True,
            )
        except WorkdayNotFoundError:
            # An exact position id that Workday does not hold lists nothing, but an
            # unknown organization is a mistake the caller has to see.
            if not position_id:
                raise
            response: dict[str, Any] = {}
        else:
            response = result.response("Get_Positions")
        return {
            "positions": lifecycle.parse_positions(response),
            "page": page,
            "total_pages": workers.page_info(response)[1],
            "as_of_entry_datetime": moment,
        }

    async def workday_get_worker_event_history(
        worker_id: str,
        updated_from: str | None = None,
        updated_through: str | None = None,
    ) -> Any:
        result = await get_client().call(
            SERVICE_HUMAN_RESOURCES,
            "Get_Worker_Event_History",
            events.build_worker_history(worker_id, updated_from, updated_through),
            is_read=True,
        )
        return {
            "events": events.parse_worker_history(
                result.response("Get_Worker_Event_History")
            ),
            "note": (
                "History is visibility-limited and is not proof that no pending "
                "event exists. Inspect candidate event IDs with "
                "workday_get_event."
            ),
        }

    for fn, description in [
        (
            workday_get_organization,
            (
                "Read an exact organization, manager, roles, superior, children "
                "and staffing model as of a date."
            ),
        ),
        (
            workday_get_manager_organizations,
            (
                "Resolve organizations led by a worker, distinct from their own "
                "membership organization. Never infer absence when scan_complete "
                "is false."
            ),
        ),
        (
            workday_list_organization_workers,
            (
                "Read members of an organization before a manager move or "
                "termination. Page through all results."
            ),
        ),
        (
            workday_list_positions,
            "Find available or filled positions in a position-management organization.",
        ),
        (
            workday_get_worker_event_history,
            (
                "Discover worker event IDs by update window, including recovery "
                "after a timeout with no returned event ID."
            ),
        ),
    ]:
        registrar.add(
            cast(Callable[..., Any], fn),
            name=fn.__name__,
            title=fn.__name__.replace("_", " "),
            description=description,
            read_only=True,
            idempotent=True,
        )


def register_lifecycle_writes(registrar: ToolRegistrar) -> None:
    async def workday_inactivate_supervisory_organization(
        organization_id: str,
        effective_date: str,
        confirm: bool = False,
        keep_in_hierarchy: bool = True,
        remove_role_assignments: bool = False,
    ) -> Any:
        client = get_client()
        current = await fetch_organization(client, organization_id, effective_date)
        if current.get("organization_type") != "Supervisory":
            raise ValueError("Only supervisory organizations can be inactivated here.")
        if current.get("inactive"):
            return {"submitted": False, "no_change": True}
        if current.get("subordinates"):
            raise ValueError("Reparent subordinate organizations before inactivation.")
        request = workers.build_scan_request(1, 1)
        request["Request_Criteria"] = {
            "Organization_Reference": reference(
                "Organization_Reference_ID", organization_id
            ),
            "Include_Subordinate_Organizations": False,
            "Exclude_Inactive_Workers": True,
        }
        request["Response_Filter"] = {
            "As_Of_Effective_Date": iso_date(effective_date),
            "Page": 1,
            "Count": 1,
        }
        result = await client.call(
            SERVICE_HUMAN_RESOURCES, workers.OPERATION, request, is_read=True
        )
        if workers.workers_in(result.response(workers.OPERATION)):
            raise ValueError(
                "Move or terminate active organization members before inactivation."
            )
        payload = organizations.build_inactivate_organization(
            current.get("ids", {}).get("WID", ""),
            effective_date,
            keep_in_hierarchy=keep_in_hierarchy,
            remove_role_assignments=remove_role_assignments,
        )
        if not confirm and not is_preview():
            return {
                "submitted": False,
                "requires_confirmation": True,
                "organization": current,
            }

        async def verify() -> dict[str, Any]:
            actual = await fetch_organization(client, organization_id, effective_date)
            return {"verified": actual.get("inactive") is True, "organization": actual}

        outcome = await _submit(
            client,
            SERVICE_HUMAN_RESOURCES,
            "Inactivate_Organization",
            payload,
            auto_complete=False,
            verify=verify,
        )
        outcome["before"] = current
        return outcome

    async def workday_create_supervisory_organization(
        organization_id: str,
        name: str,
        superior_id: str,
        effective_date: str,
        subtype_name: str,
        staffing_model: str,
        availability_date: str,
        location_id: str | None = None,
        visibility_name: str | None = None,
    ) -> Any:
        iso_date(availability_date, "availability_date")
        request = organizations.build_put_supervisory_organization(
            organization_id=organization_id,
            name=name,
            superior_id=superior_id,
            effective_date=effective_date,
            subtype_name=subtype_name,
            staffing_model=staffing_model,
            availability_date=availability_date,
            location_id=location_id,
            visibility_name=visibility_name,
        )
        if not subtype_name.strip():
            raise ValueError("subtype_name is required for a new organization.")
        client = get_client()
        existing = await fetch_organization(
            client, organization_id, effective_date, required=False
        )
        if existing:
            return {
                "submitted": False,
                "duplicate": True,
                "existing": existing,
                "note": (
                    "Use the update tool for an existing organization; creation "
                    "never overwrites it."
                ),
            }
        parent = await check_parent(
            client, organization_id, superior_id, effective_date
        )
        outcome = await _submit(
            client,
            SERVICE_HUMAN_RESOURCES,
            "Add_Update_Organization",
            request,
            auto_complete=False,
        )
        outcome.update(
            {
                "superior": parent,
                "note": (
                    "Organization maintenance is immediate and bypasses "
                    "business-process approvals. Assign manager/approver roles "
                    "separately before moving workers."
                ),
            }
        )
        if outcome.get("submitted"):
            try:
                actual = await fetch_organization(
                    client, organization_id, effective_date
                )
                outcome["verification"] = {
                    "verified": actual.get("superior", {}).get("id") == superior_id,
                    "organization": actual,
                }
                outcome["verification"]["verified"] = bool(
                    outcome["verification"]["verified"]
                    and actual.get("name") == name
                    and staffing_model in actual.get("staffing_model", "").lower()
                )
            except Exception as exc:
                outcome["verification"] = {"verified": False, "error": str(exc)}
        return outcome

    async def workday_update_supervisory_organization(
        organization_id: str,
        superior_id: str,
        effective_date: str,
        name: str | None = None,
    ) -> Any:
        client = get_client()
        current = await fetch_organization(client, organization_id, effective_date)
        if current.get("organization_type") != "Supervisory" or current.get("inactive"):
            raise ValueError(
                "Only active supervisory organizations can be updated here."
            )
        await check_parent(client, organization_id, superior_id, effective_date)
        target_name = name if name is not None else current["name"]
        if (
            current.get("superior", {}).get("id") == superior_id
            and current["name"] == target_name
        ):
            return {"submitted": False, "no_change": True}
        request = organizations.build_put_supervisory_organization(
            organization_id=organization_id,
            name=target_name,
            superior_id=superior_id,
            effective_date=effective_date,
        )

        async def verify() -> dict[str, Any]:
            actual = await fetch_organization(client, organization_id, effective_date)
            return {
                "verified": actual.get("superior", {}).get("id") == superior_id
                and actual.get("name") == target_name,
                "organization": actual,
            }

        outcome = await _submit(
            client,
            SERVICE_HUMAN_RESOURCES,
            "Add_Update_Organization",
            request,
            auto_complete=False,
            verify=verify,
        )
        outcome["before"] = current
        outcome["note"] = (
            "Immediate organization maintenance; worker membership and role "
            "assignments are separate changes."
        )
        return outcome

    async def workday_assign_organization_role(
        organization_id: str,
        role_id: str,
        assignee_id: str,
        effective_date: str,
        assignee_id_type: str = "Position_ID",
        role_id_type: str = "Organization_Role_ID",
        remove_assignee_ids: list[str] | None = None,
        add_assignee: bool = True,
        auto_complete: bool = False,
        comment: str | None = None,
    ) -> Any:
        _check_auto_complete(auto_complete)
        request = lifecycle.build_assign_roles(
            organization_id=organization_id,
            role_id=role_id,
            assignee_id=assignee_id,
            effective_date=effective_date,
            assignee_id_type=assignee_id_type,
            role_id_type=role_id_type,
            remove_assignee_ids=remove_assignee_ids,
            add_assignee=add_assignee,
            auto_complete=auto_complete,
            comment=comment,
        )
        current = await fetch_organization(
            get_client(), organization_id, effective_date
        )
        outcome = await _submit(
            get_client(),
            SERVICE_STAFFING,
            "Assign_Roles",
            request,
            auto_complete=auto_complete,
        )
        outcome["before"] = current.get("roles", [])
        outcome["note"] = (
            "Role assignment events cannot be rescinded; reverse with an "
            "explicit compensating role assignment. Role IDs and expense "
            "approval routing are tenant-defined."
        )
        return outcome

    async def workday_create_position(
        organization_id: str,
        position_id: str,
        title: str,
        availability_date: str,
        earliest_hire_date: str,
        job_profile_id: str | None = None,
        location_id: str | None = None,
        reason_id: str | None = None,
        auto_complete: bool = False,
        comment: str | None = None,
    ) -> Any:
        _check_auto_complete(auto_complete)
        request = lifecycle.build_create_position(
            organization_id=organization_id,
            position_id=position_id,
            title=title,
            availability_date=availability_date,
            earliest_hire_date=earliest_hire_date,
            job_profile_id=job_profile_id,
            location_id=location_id,
            reason_id=reason_id,
            auto_complete=auto_complete,
            comment=comment,
        )
        org = await fetch_organization(get_client(), organization_id, availability_date)
        if "position" not in org.get("staffing_model", "").lower():
            raise ValueError(
                "Create Position requires a position-management organization."
            )
        return await _submit(
            get_client(),
            SERVICE_STAFFING,
            "Create_Position",
            request,
            auto_complete=auto_complete,
        )

    async def workday_update_home_address(
        worker_id: str,
        effective_date: str,
        address: dict[str, Any],
        worker_id_type: str = "Employee_ID",
        address_id: str | None = None,
        create_if_missing: bool = False,
        auto_complete: bool = False,
        comment: str | None = None,
    ) -> Any:
        _check_auto_complete(auto_complete)
        worker = await fetch_worker(
            get_client(), worker_id, worker_id_type, as_of_effective_date=effective_date
        )
        candidates = [
            a
            for a in worker.get("addresses", [])
            if any(u["type"] == "HOME" for u in a["usages"])
        ]
        if address_id:
            candidates = [
                a
                for a in candidates
                if address_id in a["reference"].get("ids", {}).values()
            ]
        else:
            candidates = [
                a
                for a in candidates
                if any(u["type"] == "HOME" and u["primary"] for u in a["usages"])
            ]
        if len(candidates) > 1:
            raise ValueError("Multiple home addresses match; pass an exact address_id.")
        if not candidates and (address_id or not create_if_missing):
            raise ValueError(
                "No visible matching home address. To create one, verify ISU "
                "visibility and set create_if_missing."
            )
        baseline = candidates[0] if candidates else {}
        if any(u["type"] != "HOME" for u in baseline.get("usages", [])):
            raise ValueError(
                "This address is shared with another usage; edit it "
                "explicitly in Workday."
            )
        if baseline and not baseline.get("reference", {}).get("id"):
            raise ValueError(
                "Existing home address has no reference; cannot update it safely."
            )
        merged = {
            k: baseline.get(k, "")
            for k in ("country", "lines", "city", "region_id", "postal_code")
        }
        merged.update(address)
        if baseline and "lines" in address:
            merged["lines"] = {**baseline.get("lines", {}), **address["lines"]}
        if (
            baseline
            and all(merged.get(k) == baseline.get(k) for k in merged)
            and all(not u["public"] for u in baseline.get("usages", []))
        ):
            return {"submitted": False, "no_change": True, "address": baseline}
        if (
            baseline
            and address.get("country", baseline["country"]) != baseline["country"]
            and "region_id" not in address
        ):
            raise ValueError(
                "Changing country requires an explicit region_id (or an empty value)."
            )
        request = lifecycle.build_home_address(
            worker_id=worker_id,
            worker_id_type=worker_id_type,
            effective_date=effective_date,
            address=merged,
            address_id=baseline.get("reference", {}).get("id"),
            address_id_type=baseline.get("reference", {}).get("id_type", "Address_ID"),
            auto_complete=auto_complete,
            comment=comment,
            baseline=baseline,
        )
        outcome = await _submit(
            get_client(),
            SERVICE_HUMAN_RESOURCES,
            "Maintain_Contact_Information_for_Person_Event",
            request,
            auto_complete=auto_complete,
        )
        outcome.update(before=baseline, after=merged)
        return outcome

    async def workday_recover_event(
        event_id: str, action: str, comment: str, confirm: bool = False
    ) -> Any:
        request = events.build_recovery(event_id, action, comment)
        result = await get_client().call(
            SERVICE_INTEGRATIONS,
            events.OPERATION,
            events.build_get_event_request(event_id),
            is_read=True,
        )
        current = events.parse_event(result.response(events.OPERATION))
        if not current:
            raise ValueError("Event is not visible; cannot perform recovery.")
        if not confirm and not is_preview():
            return {"submitted": False, "requires_confirmation": True, "event": current}
        outcome = await _submit(
            get_client(),
            SERVICE_INTEGRATIONS,
            f"{action.title()}_Business_Process",
            request,
            auto_complete=False,
        )
        outcome["before"] = current
        outcome["note"] = (
            "Workday enforces event eligibility and dependencies. "
            "Cancellation/rescission is not a universal undo and does not "
            "reverse unrelated events."
        )
        return outcome

    for fn, description in [
        (
            workday_inactivate_supervisory_organization,
            "Immediately inactivate an empty supervisory organization after moving "
            "members and children. Requires confirmation. Keep hierarchy and direct "
            "roles by default; removing all direct roles including future assignments "
            "requires remove_role_assignments=true. Preview first.",
        ),
        (
            workday_create_supervisory_organization,
            (
                "Create a supervisory organization under an explicit superior, "
                "with tenant subtype and staffing model. IMMEDIATE write: "
                "Add_Update_Organization does not run business-process approval "
                "routing. Preview and obtain approval first."
            ),
        ),
        (
            workday_update_supervisory_organization,
            (
                "Reparent or rename an existing supervisory organization, "
                "checking for cycles. IMMEDIATE write; separate worker moves and "
                "role assignments are required."
            ),
        ),
        (
            workday_assign_organization_role,
            (
                "Assign a manager or expense-approver role using explicit tenant "
                "role IDs and assignees. Position IDs avoid multi-job worker "
                "ambiguity. Only named former assignees are removed."
            ),
        ),
        (
            workday_create_position,
            (
                "Create a stable-ID position before hiring into a "
                "position-management organization. Wait for its business process "
                "to complete before hiring."
            ),
        ),
        (
            workday_update_home_address,
            (
                "Patch one referenced home address, preserving unspecified fields"
                " and other addresses. HOME/private usage; approval routing "
                "remains enabled by default."
            ),
        ),
        (
            workday_recover_event,
            (
                "Cancel a pending or rescind a completed business process where "
                "Workday permits it. Requires confirmation and a comment; preview"
                " shows the event first."
            ),
        ),
    ]:
        registrar.add(
            cast(Callable[..., Any], fn),
            name=fn.__name__,
            title=fn.__name__.replace("_", " "),
            description=description,
            read_only=False,
            destructive=True,
        )
