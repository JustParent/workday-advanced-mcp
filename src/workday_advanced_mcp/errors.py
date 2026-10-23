"""Workday error handling.

Messages name what the caller must change: which security domain to grant, which
reference to correct, or that a write may have committed despite the error. They
are rendered straight into the agent's prompt, so they never carry a traceback.
"""

from __future__ import annotations

import httpx

from .config import (
    ENV_CLIENT_ID,
    ENV_CLIENT_SECRET,
    ENV_ISU_PASSWORD,
    ENV_ISU_USERNAME,
    ENV_REFRESH_TOKEN,
    ENV_TENANT,
    ConfigError,
)

__all__ = [
    "ConfigError",
    "WorkdayApiError",
    "WorkdayAuthError",
    "WorkdayFaultError",
    "WorkdayTimeoutError",
    "format_exception",
    "raise_for_http_error",
]

# Named in the 403 message so an operator can grant exactly what is missing.
DOMAIN_HINTS = {
    "Staffing": (
        "Worker Data: Public Worker Reports, Worker Data: Current Staffing "
        "Information, "
        "Worker Data: Organization Information, and the business process security "
        "policy "
        "for the event being submitted (Hire, Change Job, Change Organization "
        "Assignments, "
        "Termination)"
    ),
    "Human_Resources": (
        "Worker Data: Public Worker Reports, Manage: Organization Integration, "
        "Put access to Manage: Organization Update Integration for organization "
        "changes, and the Contact Change business process for home addresses"
    ),
    "Cash_Management": (
        "Worker Data: Payroll Interface (Payment Elections) or Worker Data: Payroll "
        "(Payment Elections), Manage: Payment Election, and the Payment Election "
        "Enrollment Event business process for changes"
    ),
    "Recruiting": (
        "Manage: Pre-Hire Process and Pre-Hire Process Data: Name and Contact "
        "Information"
    ),
    "Integrations": (
        "View All on the event's business process security policy, plus Cancel or "
        "Rescind there to recover one"
    ),
}


class WorkdayApiError(Exception):
    """A Workday call failed. ``str(err)`` is safe to show to a caller."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class WorkdayAuthError(WorkdayApiError):
    """Workday rejected the credentials. Never retried: ISUs lock out."""


class WorkdayFaultError(WorkdayApiError):
    """A SOAP fault, usually a validation error naming the offending field."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        fault_code: str | None = None,
        validation_errors: list[str] | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code)
        self.fault_code = fault_code
        self.validation_errors = validation_errors or []


class WorkdayTimeoutError(WorkdayApiError):
    """The call timed out. For a write this is ambiguous, never retry blindly."""


def auth_error_message(service: str, status: int, *, oauth: bool = False) -> str:
    if status == 401 and oauth:
        return (
            "Workday rejected the OAuth access token (401) even after a new one was "
            f"issued. Check that {ENV_REFRESH_TOKEN} has not been revoked or "
            f"regenerated, that the API client in {ENV_CLIENT_ID} is still active "
            "and that the Workday account the token was issued for is active."
        )
    if status == 401:
        return (
            "Workday rejected the credentials (401). Check that "
            f"{ENV_ISU_USERNAME} and {ENV_ISU_PASSWORD} match an active Integration "
            "System User, that its password has not expired, and that the account is "
            "not locked out. Do not retry: repeated failures lock the ISU."
        )
    hint = DOMAIN_HINTS.get(service, "the relevant Workday security domains")
    scope = (
        " Also include those functional areas in the API client's scope."
        if oauth
        else ""
    )
    return (
        f"Workday denied access (403) to the {service} service. Grant the Integration "
        f"System User's security group: {hint}.{scope} Activate pending security "
        "policy changes "
        "afterwards. If the tenant restricts API access by IP, also allow this "
        "server's "
        "outbound address (set WORKDAY_PROXY_URL to route through a fixed IP)."
    )


def auth_fault_advice(*, oauth: bool = False) -> str:
    """What to check after Workday faults an authentication.

    Workday sends bad credentials as HTTP 500 with a fault that says only
    "invalid username or password". A wrong tenant, a reset or expired password
    and a sign-in the authentication policy blocks all read the same.
    """
    if oauth:
        return (
            f"Check {ENV_CLIENT_ID}, {ENV_CLIENT_SECRET} and {ENV_REFRESH_TOKEN}, "
            "that the API client is active and that the refresh token has not been "
            "revoked or regenerated. Do not retry."
        )
    return (
        f"Check that {ENV_ISU_USERNAME} and {ENV_ISU_PASSWORD} belong to an active "
        f"Integration System User in the tenant named by {ENV_TENANT} (it signs in "
        "as user@tenant), that its password has not expired or been reset and that "
        "it is not locked out. If the tenant's authentication policy limits "
        "sign-in by IP address, allow this server's outbound address too. Do not "
        "retry: repeated failures lock the ISU."
    )


def raise_for_http_error(
    response: httpx.Response, *, service: str, oauth: bool = False
) -> None:
    """Raise for a transport-level failure that is not a SOAP fault."""
    status = response.status_code
    if status in (401, 403):
        raise WorkdayAuthError(
            auth_error_message(service, status, oauth=oauth), status_code=status
        )
    if status == 404:
        raise WorkdayApiError(
            f"Workday returned 404 for the {service} endpoint. Check WORKDAY_HOST, "
            "WORKDAY_TENANT and WORKDAY_API_VERSION - the version segment must exist "
            "in this tenant.",
            status_code=status,
        )
    if status == 429:
        retry_after = response.headers.get("Retry-After", "").strip() or "a few"
        raise WorkdayApiError(
            f"Workday rate limit exceeded (429). Wait {retry_after} seconds before "
            f"retrying.",
            status_code=status,
        )
    if status >= 500:
        raise WorkdayApiError(
            f"Workday returned a server error ({status}) with no SOAP fault. This is "
            "usually transient for reads. For a write, do not retry until you have "
            "checked with workday_get_event or workday_find_worker whether it "
            "committed.",
            status_code=status,
        )
    if response.is_error:
        raise WorkdayApiError(
            f"Workday request to {service} failed with status {status}.",
            status_code=status,
        )


def fault_to_error(
    *,
    operation: str,
    fault_code: str | None,
    fault_string: str | None,
    validation_errors: list[str],
    status_code: int | None,
    oauth: bool = False,
) -> WorkdayFaultError:
    """Turn a parsed SOAP fault into an actionable exception."""
    detail = "; ".join(validation_errors) or (fault_string or "").strip()
    code = (fault_code or "").strip()

    if validation_errors:
        message = (
            f"Workday rejected {operation}: {detail} Confirm reference IDs with the "
            "workday_list_* tools and that every field the tenant's business process "
            "requires is supplied."
        )
    elif "authentication" in code.lower():
        message = (
            f"Workday could not authenticate {operation}: {detail} "
            f"{auth_fault_advice(oauth=oauth)}"
        )
    else:
        reason = detail or code or "no detail"
        message = f"Workday returned a SOAP fault for {operation}: {reason}"

    return WorkdayFaultError(
        message,
        status_code=status_code,
        fault_code=code or None,
        validation_errors=validation_errors,
    )


def format_exception(exc: Exception) -> str:
    """Render an exception as a caller-facing message, never a traceback."""
    if isinstance(exc, (WorkdayApiError, ConfigError, ValueError)):
        return f"Error: {exc}"
    if isinstance(exc, httpx.TimeoutException):
        return (
            "Error: The request to Workday timed out. If this was a write it may still "
            "have committed - check with workday_get_event or workday_find_worker "
            "before "
            "sending it again."
        )
    if isinstance(exc, httpx.HTTPError):
        return f"Error: Could not reach Workday: {exc}"
    return f"Error: Unexpected {type(exc).__name__}: {exc}"
