from __future__ import annotations

from copy import deepcopy
import unittest

from app.domain.errors import AuthorizationError
from app.security import AccessPolicy, Permission, Role
from app.settings import SettingsRepository
from app.settings.models import StationSettings
from tests.helpers import SETTINGS_TEMPLATE


class AccessControlTests(unittest.TestCase):
    def _settings(self, assignments: dict[str, list[str]] | None = None) -> StationSettings:
        raw = deepcopy(SettingsRepository(SETTINGS_TEMPLATE).load().raw)
        raw["access_control"]["user_roles"] = assignments or {}
        return StationSettings.model_validate(raw)

    def test_unassigned_os_user_is_operator_and_cannot_change_safety_profile(self) -> None:
        policy = AccessPolicy.from_settings(self._settings(), username="LAB\\alice")
        self.assertEqual(policy.identity.roles, frozenset({Role.OPERATOR}))
        self.assertTrue(policy.allows(Permission.OPERATE_OUTPUT))
        self.assertTrue(policy.allows(Permission.RUN_RECIPE))
        self.assertFalse(policy.allows(Permission.EDIT_SETTINGS))
        with self.assertRaisesRegex(AuthorizationError, "Access denied"):
            policy.require(Permission.APPROVE_PROFILE, action="profile approval")

    def test_exact_case_insensitive_assignment_grants_engineer_permissions(self) -> None:
        policy = AccessPolicy.from_settings(
            self._settings({"lab/alice": ["engineer"]}),
            username="LAB\\ALICE",
        )
        self.assertEqual(policy.identity.roles, frozenset({Role.ENGINEER}))
        self.assertTrue(policy.allows(Permission.EDIT_SETTINGS))
        self.assertTrue(policy.allows(Permission.APPROVE_PROFILE))
        self.assertFalse(policy.allows(Permission.MANAGE_ROLES))
        self.assertFalse(policy.allows(Permission.SERVICE_DIAGNOSTICS))

    def test_simulation_identity_has_all_roles_but_estop_is_never_denied(self) -> None:
        policy = AccessPolicy.from_settings(self._settings(), simulation=True)
        self.assertEqual(policy.identity.roles, frozenset(Role))
        self.assertTrue(policy.allows(Permission.SERVICE_DIAGNOSTICS))
        operator = AccessPolicy.from_settings(self._settings(), username="operator")
        self.assertTrue(operator.allows(Permission.EMERGENCY_STOP))


if __name__ == "__main__":
    unittest.main()
