"""Callback utilities for TasksBot.

Provides signing and validation helpers for callback payloads. Each
payload is a JSON object with at least an ``a`` field describing the
action. A short SHA1 signature protects the payload from tampering.

Public helpers:
    mk_cb(action, **kwargs) -> str
        Create a signed callback data string.
    parse_cb(data: str) -> dict | None
        Validate signature and return payload dictionary. ``None`` is
        returned for malformed or tampered data.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional, cast

CALLBACK_SECRET = os.getenv("CALLBACK_SECRET", "change-me").encode("utf-8")


def _cb_sign(payload: str) -> str:
    """Return short SHA1 signature for ``payload``.

    The signature is deterministic and is used only to detect tampering of
    callback data. Only first six hex digits are used to keep callback
    strings compact.
    """

    return hashlib.sha1(CALLBACK_SECRET + payload.encode("utf-8")).hexdigest()[:6]


def mk_cb(action: str, **kwargs: Any) -> str:
    """Create signed callback data string.

    Parameters
    ----------
    action:
        Callback action prefix. Should be short and ascii only.
    **kwargs:
        Additional payload fields. Values must be JSON serialisable.

    Returns
    -------
    str
        A string that can be safely used as ``callback_data`` in Telegram
        buttons. Includes signature and JSON payload separated by ``|``.
    """

    payload: Dict[str, Any] = {"v": 1, "a": action, **kwargs}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    sig = _cb_sign(raw)
    return f"{sig}|{raw}"


def parse_cb(data: str) -> Optional[Dict[str, Any]]:
    """Validate callback ``data`` and return payload.

    Parameters
    ----------
    data:
        Raw ``callback_data`` string received from Telegram.

    Returns
    -------
    dict | None
        Parsed payload if signature is valid, otherwise ``None``.
    """

    try:
        sig, raw = data.split("|", 1)
        if _cb_sign(raw) != sig:
            return None
        return cast(Dict[str, Any], json.loads(raw))
    except Exception:
        return None


@dataclass
class CallbackResult:
    """Result of validating callback data.

    Attributes
    ----------
    ok:
        ``True`` if payload is valid.
    payload:
        Parsed payload when ``ok`` is ``True``.
    error:
        Optional error message for logging/debugging.
    """

    ok: bool
    payload: Optional[Dict[str, Any]] = None
    error: str | None = None


def validate_callback(data: str) -> CallbackResult:
    """High level validation helper returning structured result."""

    payload = parse_cb(data)
    if payload is None:
        return CallbackResult(False, error="bad-signature")
    if "a" not in payload:
        return CallbackResult(False, error="no-action")
    return CallbackResult(True, payload)
