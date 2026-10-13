"""Preview context and the state token that binds an approval to what it saw.

Nothing is stored. The host (Harriet's engine) owns approval and duplicate
suppression, and may preview in one short-lived sandbox and submit in another, so
a state token must be reproducible by any process configured for the same tenant.
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import inspect
import json
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import Settings

STATE_TOKEN_CONTEXT = "workday-advanced-mcp/state-token/v1"


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode()


def stable_read_state(body: dict[str, Any]) -> dict[str, Any]:
    """Ignore query clock metadata, retaining all business data and page counts.

    Workday defaults As_Of_Entry_DateTime to the current moment. Including its
    response echo would reject unchanged data on every preview-to-write call.
    The caller's explicit date/time arguments remain bound into the token.
    """
    result = dict(body)
    for key, value in body.items():
        if isinstance(value, dict):
            if key == "Response_Filter":
                result[key] = {
                    k: v for k, v in value.items() if k != "As_Of_Entry_DateTime"
                }
            else:
                result[key] = stable_read_state(value)
    return result


def state_token(settings: Settings, value: Any) -> str:
    """A keyed digest of the inputs and Workday state a preview saw.

    The key derives from the connector's own credential, so the token reproduces
    in any sandbox with the same configuration and nobody without that credential
    can use it to test guesses at the private data it covers. Rotating the
    credential invalidates outstanding previews, which fails safe.
    """
    from .config import AUTH_MODE_OAUTH

    secret = (
        settings.client_secret
        if settings.auth_mode == AUTH_MODE_OAUTH
        else settings.isu_password
    )
    key = hmac.new(
        secret.encode(),
        canonical([STATE_TOKEN_CONTEXT, settings.endpoint_base]),
        hashlib.sha256,
    ).digest()
    return hmac.new(key, canonical(value), hashlib.sha256).hexdigest()


@dataclass
class Execution:
    preview: bool = False
    expected_state_token: str | None = None
    reads: list[Any] = field(default_factory=list)
    writes: int = 0
    state_token: str = ""
    arguments: Any = None


current_execution: ContextVar[Execution | None] = ContextVar("execution", default=None)


def controlled(fn: Any) -> Any:
    """Expose execution controls in the MCP schema without duplicating every tool."""

    @functools.wraps(fn)
    async def wrapped(
        *args: Any,
        expected_state_token: str | None = None,
        **kwargs: Any,
    ) -> Any:
        bound = inspect.signature(fn).bind(*args, **kwargs)
        bound.apply_defaults()
        approved = {
            k: v
            for k, v in bound.arguments.items()
            if k not in {"confirm", "confirm_replaces_existing"}
        }
        existing = current_execution.get()
        if existing is not None:
            existing.arguments = [fn.__name__, approved]
            return await fn(*args, **kwargs)
        token = current_execution.set(
            Execution(
                expected_state_token=expected_state_token,
                arguments=[fn.__name__, approved],
            )
        )
        try:
            return await fn(*args, **kwargs)
        finally:
            current_execution.reset(token)

    signature = inspect.signature(fn, eval_str=True)
    parameters = list(signature.parameters.values())
    parameters.append(
        inspect.Parameter(
            "expected_state_token",
            inspect.Parameter.KEYWORD_ONLY,
            default=None,
            annotation=str | None,
        )
    )
    wrapped.__signature__ = signature.replace(parameters=parameters)  # type: ignore[attr-defined]
    return wrapped
