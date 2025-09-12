import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from app.permissions import Role, can


def test_admin_can_everything():
    assert can(Role.ADMIN, "anything")


def test_basic_permissions():
    assert can(Role.SELLER, "view_tasks")
    assert not can(Role.SELLER, "health")
    assert can(Role.SENIOR_BARISTA, "edit")
