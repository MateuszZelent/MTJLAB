"""Operating-system identity and deny-by-default station RBAC."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import getpass
import os
import platform

from app.domain.errors import AuthorizationError
from app.settings.models import StationSettings


class Role(StrEnum):
    OPERATOR = "operator"
    ENGINEER = "engineer"
    SERVICE = "service"


class Permission(StrEnum):
    VIEW = "view"
    CONNECT = "connect"
    PASSIVE_MEASURE = "passive_measure"
    OPERATE_OUTPUT = "operate_output"
    RUN_RECIPE = "run_recipe"
    EMERGENCY_STOP = "emergency_stop"
    EDIT_SETTINGS = "edit_settings"
    APPROVE_PROFILE = "approve_profile"
    ASSIGN_VISA = "assign_visa"
    MANAGE_ROLES = "manage_roles"
    SERVICE_DIAGNOSTICS = "service_diagnostics"


_ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.OPERATOR: frozenset(
        {
            Permission.VIEW,
            Permission.CONNECT,
            Permission.PASSIVE_MEASURE,
            Permission.OPERATE_OUTPUT,
            Permission.RUN_RECIPE,
            Permission.EMERGENCY_STOP,
        }
    ),
    Role.ENGINEER: frozenset(
        {
            Permission.VIEW,
            Permission.CONNECT,
            Permission.PASSIVE_MEASURE,
            Permission.OPERATE_OUTPUT,
            Permission.RUN_RECIPE,
            Permission.EMERGENCY_STOP,
            Permission.EDIT_SETTINGS,
            Permission.APPROVE_PROFILE,
            Permission.ASSIGN_VISA,
        }
    ),
    Role.SERVICE: frozenset(Permission),
}


@dataclass(frozen=True, slots=True)
class AuthenticatedIdentity:
    username: str
    provider: str
    host: str
    roles: frozenset[Role]

    @property
    def display_name(self) -> str:
        return f"{self.username} ({', '.join(sorted(role.value for role in self.roles))})"

    def as_context(self) -> dict[str, object]:
        return {
            "username": self.username,
            "provider": self.provider,
            "host": self.host,
            "roles": tuple(sorted(role.value for role in self.roles)),
        }


class AccessPolicy:
    """Resolve permissions once from an authenticated, immutable identity."""

    def __init__(self, identity: AuthenticatedIdentity) -> None:
        self.identity = identity
        permissions: set[Permission] = set()
        for role in identity.roles:
            permissions.update(_ROLE_PERMISSIONS[role])
        self._permissions = frozenset(permissions)

    @classmethod
    def from_settings(
        cls,
        settings: StationSettings,
        *,
        username: str | None = None,
        simulation: bool = False,
    ) -> "AccessPolicy":
        if simulation:
            identity = AuthenticatedIdentity(
                username="SIMULATION",
                provider="simulation",
                host=platform.node() or "simulation",
                roles=frozenset(Role),
            )
            return cls(identity)
        configured = settings.access_control
        if configured.identity_provider != "operating_system":
            raise AuthorizationError(
                f"Unsupported identity provider {configured.identity_provider!r}."
            )
        resolved_username = (username or cls._operating_system_username()).strip()
        if not resolved_username:
            raise AuthorizationError("The operating-system identity could not be resolved.")
        normalized = cls.normalize_username(resolved_username)
        assignment = next(
            (
                roles
                for candidate, roles in configured.user_roles.items()
                if cls.normalize_username(candidate) == normalized
            ),
            configured.default_roles,
        )
        roles = frozenset(Role(value) for value in assignment)
        return cls(
            AuthenticatedIdentity(
                username=resolved_username,
                provider="operating_system",
                host=platform.node() or "unknown-host",
                roles=roles,
            )
        )

    @staticmethod
    def _operating_system_username() -> str:
        domain = os.environ.get("USERDOMAIN", "").strip()
        username = getpass.getuser().strip()
        return f"{domain}\\{username}" if domain and "\\" not in username else username

    @staticmethod
    def normalize_username(username: str) -> str:
        return username.strip().replace("/", "\\").casefold()

    def allows(self, permission: Permission) -> bool:
        # E-STOP is intentionally never denied after application startup.
        return permission == Permission.EMERGENCY_STOP or permission in self._permissions

    def require(self, permission: Permission, *, action: str | None = None) -> None:
        if self.allows(permission):
            return
        label = action or permission.value.replace("_", " ")
        roles = ", ".join(sorted(role.value for role in self.identity.roles)) or "none"
        raise AuthorizationError(
            f"Access denied for {label}: OS identity {self.identity.username!r} has role(s) "
            f"{roles}; permission {permission.value!r} is required."
        )
