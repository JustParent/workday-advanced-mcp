"""Preview context and an atomic, durable journal for non-idempotent writes.

The database stores keyed request digests and redacted response references, never
credentials or request payloads. Mount WORKDAY_STATE_PATH on persistent storage
when deploying a short-lived MCP container.
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import inspect
import json
import os
import secrets
import sqlite3
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .redact import redact_data


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


@dataclass
class Execution:
    preview: bool = False
    request_id: str | None = None
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
        request_id: str | None = None,
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
                request_id=request_id,
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
    for name in ("request_id", "expected_state_token"):
        parameters.append(
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                default=None,
                annotation=str | None,
            )
        )
    wrapped.__signature__ = signature.replace(parameters=parameters)  # type: ignore[attr-defined]
    return wrapped


class WriteJournal:
    def __init__(self) -> None:
        default = Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser()
        self.path = Path(
            os.environ.get(
                "WORKDAY_STATE_PATH",
                str(default / "workday-advanced-mcp/writes.sqlite3"),
            )
        )
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.path, timeout=10)
        os.chmod(self.path, 0o600)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)"
        )
        self.db.execute(
            "INSERT OR IGNORE INTO metadata VALUES ('secret', ?)",
            (secrets.token_hex(32),),
        )
        self.db.execute("""CREATE TABLE IF NOT EXISTS writes (
            id TEXT PRIMARY KEY, digest TEXT NOT NULL, status TEXT NOT NULL,
            response TEXT NOT NULL DEFAULT '{}')""")
        self.db.commit()
        self.secret = bytes.fromhex(
            self.db.execute("SELECT value FROM metadata WHERE key='secret'").fetchone()[
                0
            ]
        )

    def close(self) -> None:
        self.db.close()

    def digest(self, value: Any) -> str:
        return hmac.new(self.secret, canonical(value), hashlib.sha256).hexdigest()

    def claim(
        self,
        scope: str,
        operation: str,
        request: dict[str, Any],
        request_id: str | None,
    ) -> tuple[str, dict[str, Any] | None]:
        # Comments may change between retries without making this a new write.
        payload = json.loads(canonical(request))
        payload.get("Business_Process_Parameters", {}).pop("Comment_Data", None)
        digest = self.digest([scope, operation, payload])
        key = self.digest([scope, request_id]) if request_id else digest
        self.db.execute("BEGIN IMMEDIATE")
        row = self.db.execute(
            "SELECT digest,status,response FROM writes WHERE id=?", (key,)
        ).fetchone()
        if row:
            self.db.commit()
            if row[0] != digest:
                raise ValueError("request_id was already used for a different payload.")
            return key, {"status": row[1], "body": json.loads(row[2])}
        self.db.execute(
            "INSERT INTO writes(id,digest,status) VALUES (?,?,'in_progress')",
            (key, digest),
        )
        self.db.commit()
        return key, None

    def finish(self, key: str, status: str, body: Any = None) -> None:
        self.db.execute(
            "UPDATE writes SET status=?,response=? WHERE id=?",
            (status, json.dumps(redact_data(body or {})), key),
        )
        self.db.commit()

    def release(self, key: str) -> None:
        self.db.execute("DELETE FROM writes WHERE id=?", (key,))
        self.db.commit()

    def get(self, key: str) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT status,response FROM writes WHERE id=?", (key,)
        ).fetchone()
        if not row:
            raise ValueError("Unknown write tracking_id in this deployment's journal.")
        return {"tracking_id": key, "status": row[0], "response": json.loads(row[1])}
