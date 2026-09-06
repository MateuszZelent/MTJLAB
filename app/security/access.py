"""Operating-system identity and deny-by-default station RBAC."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import getpass
import os
import platform
import sys

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
            Permission.ASSIGN_VISA,
            Permission.MANAGE_ROLES,
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
        exact_assignment = next(
            (
                roles
                for candidate, roles in configured.user_roles.items()
                if cls.normalize_username(candidate) == normalized
            ),
            None,
        )
        assignment = exact_assignment
        if assignment is None and "\\" not in normalized:
            # Some Windows launchers omit USERDOMAIN even though getpass still
            # returns the authenticated local account. Accept a domain-qualified
            # configuration only when its account component is unambiguous;
            # never guess when two domains configure the same username.
            local_matches = [
                roles
                for candidate, roles in configured.user_roles.items()
                if cls.normalize_username(candidate).rsplit("\\", 1)[-1]
                == normalized
            ]
            if len(local_matches) == 1:
                assignment = local_matches[0]
        if assignment is None:
            assignment = configured.default_roles
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
        """Resolve the authenticated OS account directly from platform security tokens.

        Avoids relying solely on environment variables (LOGNAME, USERNAME, USERDOMAIN)
        which can be spoofed in subprocesses or controlled launch environments.
        """
        if sys.platform == "win32":
            try:
                import ctypes
                import ctypes.wintypes

                size = ctypes.wintypes.DWORD(256)
                buf = ctypes.create_unicode_buffer(size.value)
                # NameSamCompatible = 2 returns DOMAIN\username directly from SAM/LSA
                if ctypes.windll.secur32.GetUserNameExW(2, buf, ctypes.byref(size)) and buf.value:
                    return buf.value.strip()
            except Exception:
                pass

            try:
                import ctypes
                import ctypes.wintypes

                size = ctypes.wintypes.DWORD(256)
                buf = ctypes.create_unicode_buffer(size.value)
                if ctypes.windll.advapi32.GetUserNameW(buf, ctypes.byref(size)) and buf.value:
                    return buf.value.strip()
            except Exception:
                pass

        try:
            import pwd
            return pwd.getpwuid(os.getuid()).pw_name
        except Exception:
            pass

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
