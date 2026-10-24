"""Roles, permissions and the object-level rules for leads.

Every dashboard route declares the permission it needs (``app.web.common.require``), and
routes that act on one lead also call ``can_on_lead`` with that lead, so a rep cannot act
on another rep's lead by changing the id in the URL (IDOR). The machine API token
(``ADMIN_TOKEN``) acts as an admin.

| Permission      | admin | rep                         | viewer |
| --------------- | ----- | --------------------------- | ------ |
| view dashboard  | yes   | yes                         | yes    |
| view leads      | yes   | yes (contact details masked) | yes (masked) |
| change stage    | yes   | own or unassigned leads     | no     |
| close tasks     | yes   | own or unassigned leads     | no     |
| reveal contact  | yes   | own leads only              | no     |
| assign to self  | yes   | unassigned leads            | no     |
| assign anyone   | yes   | no                          | no     |
| retry jobs, kill switch, users, settings, audit log | yes | no | no |
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Literal

from app.models import Lead, UserRole


class Permission(enum.StrEnum):
    VIEW_DASHBOARD = "view_dashboard"
    VIEW_LEADS = "view_leads"
    CHANGE_STAGE = "change_stage"
    CLOSE_TASK = "close_task"
    REVEAL_CONTACT = "reveal_contact"
    ASSIGN_SELF = "assign_self"
    ASSIGN_ANY = "assign_any"
    RETRY_JOBS = "retry_jobs"
    KILL_SWITCH = "kill_switch"
    MANAGE_USERS = "manage_users"
    VIEW_SETTINGS = "view_settings"
    VIEW_AUDIT = "view_audit"


ROLE_PERMISSIONS: dict[UserRole, frozenset[Permission]] = {
    UserRole.ADMIN: frozenset(Permission),
    UserRole.REP: frozenset(
        {
            Permission.VIEW_DASHBOARD,
            Permission.VIEW_LEADS,
            Permission.CHANGE_STAGE,
            Permission.CLOSE_TASK,
            Permission.REVEAL_CONTACT,
            Permission.ASSIGN_SELF,
        }
    ),
    UserRole.VIEWER: frozenset({Permission.VIEW_DASHBOARD, Permission.VIEW_LEADS}),
}


@dataclass(frozen=True)
class Principal:
    """Who is making a request: a dashboard user (session) or the machine API token."""

    kind: Literal["user", "token"]
    role: UserRole
    actor: str  # for the lead timeline: "human:<name>"
    audit_actor: str  # for the audit log: the user's email, or "api-token"
    user_id: int | None = None
    session_id: int | None = None
    session_hash: str | None = None  # binds CSRF tokens to this session
    mfa_enabled: bool = False

    @property
    def name(self) -> str:
        return self.actor.removeprefix("human:")

    @classmethod
    def api_token(cls) -> Principal:
        return cls(
            kind="token", role=UserRole.ADMIN, actor="human:api-token", audit_actor="api-token"
        )


def has(principal: Principal, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS[principal.role]


def can_on_lead(principal: Principal, permission: Permission, lead: Lead) -> bool:
    """``permission`` on this particular lead (role check plus the ownership rules)."""
    if not has(principal, permission):
        return False
    if principal.role is UserRole.ADMIN:
        return True
    mine = lead.assignee_id is not None and lead.assignee_id == principal.user_id
    if permission is Permission.REVEAL_CONTACT:
        return mine
    if permission in (Permission.CHANGE_STAGE, Permission.CLOSE_TASK):
        return mine or lead.assignee_id is None
    if permission is Permission.ASSIGN_SELF:
        return lead.assignee_id is None
    return True
