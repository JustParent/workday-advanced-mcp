"""Masking for bank details.

Tool results are rendered into the agent's prompt and logs are drained by the
host, so full account numbers must never leave this process. Routing/sort codes
identify the bank rather than the account and stay visible.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from typing import Any

# Element names (and result keys) whose values are masked everywhere.
# Workday names these Account_Number / IBAN / BIC inside Worker_Bank_Account_Data.
SENSITIVE_FIELDS = frozenset(
    {
        "Account_Number",
        "Bank_Account_Number",
        "IBAN",
        "Roll_Number",
        "account_number",
        "iban",
        "roll_number",
    }
)

_VISIBLE_TAIL = 4
_FINGERPRINT_KEY = secrets.token_bytes(32)
SECRET_FIELDS = frozenset(
    {
        "Password",
        "Username",
        "BinarySecurityToken",
        "Nonce",
        "password",
        "client_secret",
        "refresh_token",
        "access_token",
    }
)


def mask_account(value: str | None) -> str:
    """``12345678`` -> ``****5678``; short or empty values are fully masked."""
    text = (value or "").strip()
    if not text:
        return ""
    tail = text[-_VISIBLE_TAIL:] if len(text) > _VISIBLE_TAIL else ""
    return f"****{tail}" if tail else "****"


def account_fingerprint(value: str | None) -> str:
    """A stable comparison key for an account number that is not the number.

    Used to decide whether a payment election already matches without ever
    holding the full value in a result.
    """
    text = re.sub(r"\s+", "", (value or "")).upper()
    # A process-local keyed digest avoids both last-four collisions and exposing
    # a brute-forceable hash of a short bank account number. Masked input cannot
    # establish account identity.
    if not text or any(c in text for c in "*•") or re.match(r"X{3,}", text):
        return ""
    return hmac.new(_FINGERPRINT_KEY, text.encode(), hashlib.sha256).hexdigest()


def redact_data(data: Any) -> Any:
    """Recursively mask sensitive values in a parsed response or tool result."""
    if isinstance(data, dict):
        return {
            key: (
                "[REDACTED]"
                if key in SECRET_FIELDS
                else mask_account(value if isinstance(value, str) else None)
                if key in SENSITIVE_FIELDS
                else redact_data(value)
            )
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [redact_data(item) for item in data]
    return data


_ELEMENT_RE = re.compile(
    r"(<(?:\w+:)?(?:" + "|".join(sorted(SENSITIVE_FIELDS)) + r")\b[^>]*>)(.*?)(</)",
    re.IGNORECASE | re.DOTALL,
)


def redact_xml(xml_text: str) -> str:
    """Mask sensitive element text in an envelope before it is logged or returned."""
    xml_text = re.sub(
        r"(<(?:\w+:)?(?:" + "|".join(sorted(SECRET_FIELDS)) + r")\b[^>]*>)(.*?)(</)",
        lambda m: f"{m.group(1)}[REDACTED]{m.group(3)}",
        xml_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return _ELEMENT_RE.sub(
        lambda m: f"{m.group(1)}{mask_account(m.group(2))}{m.group(3)}", xml_text
    )
