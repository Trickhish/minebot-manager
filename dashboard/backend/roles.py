"""Bot roles and their statuses.

A *role* is the job a bot is assigned (idle, survival, building); a *status* is
the specific activity within that role (survival -> exploring / mining / ...).
Each status maps to a behavior handler run by the ``BehaviorDirector``
(see ``behavior.py``):

  * builtin     -- implemented now (idle, explore).
  * placeholder -- part of the roadmap but not yet possible (needs mining /
                   building / eating primitives in the framework); announces
                   itself once then idles.
  * script      -- runs a user-authored script (see ``scripts.py``); every role
                   offers the shared ``scripted`` status.

This module is pure data + validation so it can be imported anywhere without
pulling in the runtime.
"""

from __future__ import annotations

# role id -> definition. Order is preserved for the UI.
ROLES: dict[str, dict] = {
    "idle": {
        "label": "Idle",
        "statuses": [
            {"id": "idle", "label": "Idle", "kind": "builtin",
             "behavior": "idle"},
        ],
    },
    "survival": {
        "label": "Survival",
        "statuses": [
            {"id": "exploring", "label": "Exploring", "kind": "builtin",
             "behavior": "explore"},
            {"id": "mining", "label": "Mining", "kind": "placeholder",
             "behavior": "placeholder"},
            {"id": "looking_for_food", "label": "Looking for food",
             "kind": "placeholder", "behavior": "placeholder"},
        ],
    },
    "building": {
        "label": "Building",
        "statuses": [
            {"id": "building", "label": "Building", "kind": "placeholder",
             "behavior": "placeholder"},
        ],
    },
}

# Every role also offers a custom-script status.
SCRIPTED_STATUS = {"id": "scripted", "label": "Custom script", "kind": "script",
                   "behavior": "script"}

DEFAULT_ROLE = "idle"
DEFAULT_STATUS = "idle"


def registry() -> list[dict]:
    """The full role/status catalogue for the UI (scripted status appended)."""
    out = []
    for role_id, role in ROLES.items():
        statuses = list(role["statuses"]) + [dict(SCRIPTED_STATUS)]
        out.append({"id": role_id, "label": role["label"], "statuses": statuses})
    return out


def status_def(role: str, status: str) -> dict | None:
    """The status definition for (role, status), or None if invalid."""
    role_def = ROLES.get(role)
    if role_def is None:
        return None
    if status == SCRIPTED_STATUS["id"]:
        return dict(SCRIPTED_STATUS)
    return next((s for s in role_def["statuses"] if s["id"] == status), None)


def normalize(role: str | None, status: str | None) -> tuple[str, str]:
    """Coerce (role, status) to a valid pair, falling back to defaults."""
    if role not in ROLES:
        return DEFAULT_ROLE, DEFAULT_STATUS
    if status_def(role, status) is None:
        # First status of the role is its natural default.
        status = ROLES[role]["statuses"][0]["id"]
    return role, status
