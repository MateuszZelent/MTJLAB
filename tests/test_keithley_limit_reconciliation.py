from __future__ import annotations

from copy import deepcopy
import unittest

from app.safety.keithley_limit_reconciliation import (
    propose_keithley_limit_adjustments,
)
from app.settings import SettingsRepository
from tests.helpers import SETTINGS_TEMPLATE


class KeithleyLimitReconciliationTests(unittest.TestCase):
    def _channel_b_limits(self) -> dict[str, object]:
        raw = deepcopy(SettingsRepository(SETTINGS_TEMPLATE).load().raw)
        return raw["devices"]["keithley"]["safety"]["channels"]["B"]["lab_limits"]

    def test_current_expansion_proposes_synced_trip_and_power(self) -> None:
        proposal = propose_keithley_limit_adjustments(
            self._channel_b_limits(), ("source_current", "max"), "150 mA"
        )
        self.assertEqual(
            [(item.path, item.proposed) for item in proposal.adjustments],
            [
                (("source_current", "max_abs"), "150 mA"),
                (("measured_current_trip", "max"), "150 mA"),
                (("max_abs_power",), "10.05 mW"),
            ],
        )

    def test_power_reduction_keeps_requested_power_and_reduces_both_compliances(
        self,
    ) -> None:
        proposal = propose_keithley_limit_adjustments(
            self._channel_b_limits(), ("max_abs_power",), "500 uW"
        )

        self.assertEqual(proposal.primary_value, "500 uW")
        self.assertEqual(
            [(item.path, item.proposed) for item in proposal.adjustments],
            [
                (("voltage_compliance", "max"), "50 mV"),
                (("current_compliance", "max"), "7.46268657 mA"),
            ],
        )

    def test_power_reduction_syncs_max_abs_when_it_must_cap_the_source(self) -> None:
        limits = self._channel_b_limits()
        limits["source_voltage"]["enabled"] = False
        limits["current_compliance"]["enabled"] = False

        proposal = propose_keithley_limit_adjustments(limits, ("max_abs_power",), "50 uW")

        self.assertEqual(
            [(item.path, item.proposed) for item in proposal.adjustments],
            [
                (("source_current", "max"), "5 mA"),
                (("source_current", "max_abs"), "5 mA"),
                (("voltage_compliance", "max"), "10 mV"),
            ],
        )

    def test_current_trip_reduction_keeps_trip_and_caps_current_envelopes(self) -> None:
        proposal = propose_keithley_limit_adjustments(
            self._channel_b_limits(), ("measured_current_trip", "max"), "5 mA"
        )

        self.assertEqual(
            [(item.path, item.proposed) for item in proposal.adjustments],
            [
                (("source_current", "max"), "5 mA"),
                (("source_current", "max_abs"), "5 mA"),
                (("current_compliance", "max"), "5 mA"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
