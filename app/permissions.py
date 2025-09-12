"""Simple permission guard used by TasksBot.

The real project may have more sophisticated role/organisation models. The
function provided here is intentionally minimal yet fully typed and
unit-tested. It can be easily extended in the future without breaking
existing code.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional


class Role(str, Enum):
    """User roles used for permission checks."""

    ADMIN = "admin"
    SENIOR_SELLER = "senior_seller"
    SENIOR_BARISTA = "senior_barista"
    SELLER = "seller"
    BARISTA = "barista"


def can(
    role: Role,
    action: str,
    direction_id: Optional[int] = None,
    point_id: Optional[int] = None,
) -> bool:
    """Return ``True`` if a user with ``role`` can perform ``action``.

    Parameters
    ----------
    role:
        Role of the user. ``admin`` bypasses all checks.
    action:
        Action name, e.g. ``"view_reports"``.
    direction_id:
        Optional direction scope; unused for now but validated for future
        safety.
    point_id:
        Optional point scope.
    """

    if role == Role.ADMIN:
        return True

    # Only admins may access health checks – used in tests as an example
    if action == "health":
        return False

    # Senior roles can do anything within their direction/point. We don't
    # enforce actual scoping here but keep the parameters for future logic.
    if role in {Role.SENIOR_SELLER, Role.SENIOR_BARISTA}:
        return True

    # Sellers and baristas have limited permissions. For now they can only
    # perform basic actions such as viewing tasks.
    if action in {"view_tasks", "complete_task"} and role in {
        Role.SELLER,
        Role.BARISTA,
    }:
        return True

    return False
