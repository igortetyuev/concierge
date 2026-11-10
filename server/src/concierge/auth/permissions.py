from typing import Any

_SETTINGS_LEVELS = {"none": 0, "r": 1, "rw": 2}


def has_permission(permissions: dict[str, Any], name: str) -> bool:
    if permissions.get("*"):
        return True
    return bool(permissions.get(name))


def settings_allowed(
    permissions: dict[str, Any], scope: str, *, write: bool
) -> bool:
    if permissions.get("*"):
        return True
    conf = permissions.get("settings")
    if isinstance(conf, str):
        # Bare "rw"/"r"/"none" applies to the user's own scope only.
        level = conf if scope == "user" else "none"
    elif isinstance(conf, dict):
        level = conf.get(scope, "none")
    else:
        level = "none"
    required = 2 if write else 1
    return _SETTINGS_LEVELS.get(str(level), 0) >= required
