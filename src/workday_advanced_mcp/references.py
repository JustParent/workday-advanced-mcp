"""Workday reference IDs.

Almost every field on a Workday request is a reference: an ID plus the *type* of
ID it is. Passing the right value with the wrong type is the most common cause of
a rejected write, so the types this server uses are named here rather than spelled
out at each call site.
"""

from __future__ import annotations

from typing import Any

# Worker identity
WID = "WID"
EMPLOYEE_ID = "Employee_ID"
CONTINGENT_WORKER_ID = "Contingent_Worker_ID"
WORKER_ID_TYPES = (EMPLOYEE_ID, WID, CONTINGENT_WORKER_ID)

# Organizations and staffing
ORGANIZATION_REFERENCE_ID = "Organization_Reference_ID"
ORGANIZATION_TYPE_ID = "Organization_Type_ID"
COST_CENTER_REFERENCE_ID = "Cost_Center_Reference_ID"
COMPANY_REFERENCE_ID = "Company_Reference_ID"
POSITION_ID = "Position_ID"
JOB_PROFILE_ID = "Job_Profile_ID"
LOCATION_ID = "Location_ID"
EMPLOYEE_TYPE_ID = "Employee_Type_ID"
POSITION_TIME_TYPE_ID = "Position_Time_Type_ID"
JOB_REQUISITION_ID = "Job_Requisition_ID"

# Event reasons
EVENT_CLASSIFICATION_SUBCATEGORY_ID = "Event_Classification_Subcategory_ID"
TERMINATION_SUBCATEGORY_ID = "Termination_Subcategory_ID"

# Payments
PAYMENT_TYPE_ID = "Payment_Type_ID"
BANK_ACCOUNT_TYPE_ID = "Bank_Account_Type_ID"
CURRENCY_ID = "Currency_ID"
COUNTRY_ISO_ALPHA2 = "ISO_3166-1_Alpha-2_Code"
COMMUNICATION_USAGE_TYPE_ID = "Communication_Usage_Type_ID"

# Organization types accepted by workday_list_organizations, keyed to the value
# Get_Organizations wants in Organization_Type_Reference.
ORGANIZATION_TYPES = (
    "Supervisory",
    "Cost_Center",
    "Company",
    "Region",
    "Business_Unit",
    "Cost_Center_Hierarchy",
    "Company_Hierarchy",
    "Region_Hierarchy",
    "Matrix",
    "Pay_Group",
)

# The reference ID type each organization type is addressed by on a write.
ORGANIZATION_ID_TYPES = {
    "Cost_Center": COST_CENTER_REFERENCE_ID,
    "Company": COMPANY_REFERENCE_ID,
}


def reference(id_type: str, value: str) -> dict[str, Any]:
    """Build a Workday reference element body: ``<ID type="...">value</ID>``."""
    return {"ID": [{"type": id_type, "value": value}]}


def optional_reference(id_type: str, value: str | None) -> dict[str, Any] | None:
    """A reference, or None when the value is absent so the caller can skip it."""
    text = (value or "").strip()
    return reference(id_type, text) if text else None


def organization_id_type(organization_type: str) -> str:
    """The ID type for an organization of this type.

    Cost centres and companies have their own reference ID types; everything else
    is addressed by the generic ``Organization_Reference_ID``.
    """
    return ORGANIZATION_ID_TYPES.get(organization_type, ORGANIZATION_REFERENCE_ID)


def _id_entries(ref: Any) -> list[dict[str, Any]]:
    if not isinstance(ref, dict):
        return []
    raw = ref.get("ID")
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def extract_id(ref: Any, id_type: str) -> str:
    """Pull one ID of the given type out of a parsed reference element."""
    for entry in _id_entries(ref):
        if entry.get("@type", entry.get("type")) == id_type:
            return str(entry.get("#text", entry.get("value")) or "")
    return ""


def extract_ids(ref: Any) -> dict[str, str]:
    """Every ID on a parsed reference, keyed by its type."""
    return {
        str(entry.get("@type", entry.get("type"))): str(
            entry.get("#text", entry.get("value")) or ""
        )
        for entry in _id_entries(ref)
        if entry.get("@type", entry.get("type"))
    }


def extract_descriptor(ref: Any) -> str:
    """The human-readable label Workday attaches when descriptors are requested."""
    if isinstance(ref, dict):
        return str(ref.get("@Descriptor") or "")
    return ""


def summarize_reference(ref: Any, *, preferred: tuple[str, ...] = ()) -> dict[str, Any]:
    """Render a parsed reference as ``{id, id_type, descriptor, ids}``.

    ``id`` is the first of ``preferred`` present, so a caller gets the ID type it
    can pass straight back into a write tool rather than an opaque WID.
    """
    ids = extract_ids(ref)
    if not ids:
        return {}
    chosen_type = next((t for t in preferred if ids.get(t)), "")
    if not chosen_type:
        chosen_type = next((t for t in ids if t != WID), "") or WID
    return {
        "id": ids.get(chosen_type, ""),
        "id_type": chosen_type,
        "descriptor": extract_descriptor(ref),
        "ids": ids,
    }


def as_list(value: Any) -> list[Any]:
    """Workday collapses a single-item collection to one element; re-expand it."""
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]
