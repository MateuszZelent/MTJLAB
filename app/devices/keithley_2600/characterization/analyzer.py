"""Scientific analysis and physical parameter extraction for Keithley characterization sweeps."""

from __future__ import annotations

import math
import numpy as np

from app.devices.keithley_2600.characterization.models import (
    CharacterizationDataset,
    ExtractedScientificParameters,
)


class KeithleyCharacterizationAnalyzer:
    """Extract physical, tunnel, and instrumentation metrics from IV sweeps."""

    # Physical constants for Brinkman-Dynes-Rowell tunnel barrier model
    _M_E = 9.1093837e-31       # Electron mass (kg)
    _HBAR = 1.0545718e-34      # Reduced Planck constant (J*s)
    _Q_E = 1.60217663e-19      # Elementary charge (C)

    @classmethod
    def analyze(cls, dataset: CharacterizationDataset) -> ExtractedScientificParameters:
        """Run complete scientific analysis on the dataset."""
        points = dataset.points
        if not points:
            return cls._empty_parameters()

        config = dataset.config
        i_meas = np.array([p.measured_current_a for p in points], dtype=np.float64)
        v_meas = np.array([p.measured_voltage_v for p in points], dtype=np.float64)
        comp_active = np.array([p.compliance_active for p in points], dtype=bool)

        # 1. Compliance detection & statistics
        clamped_count = int(np.sum(comp_active))
        clamped_fraction = clamped_count / len(points)
        compliance_detected = clamped_count > 0

        compliance_onset_point: tuple[float, float] | None = None
        if compliance_detected:
            onset_indices = np.where(comp_active)[0]
            first_idx = int(onset_indices[0])
            compliance_onset_point = (
                float(points[first_idx].measured_current_a),
                float(points[first_idx].measured_voltage_v),
            )

        # 2. Extract zero-bias resistance R0 and conductance G0
        r0 = cls._extract_zero_bias_resistance(i_meas, v_meas, comp_active)
        g0 = (1.0 / r0) if (math.isfinite(r0) and abs(r0) > 1e-12) else 0.0

        # 3. Resistance-Area product (RA)
        ra_product: float | None = None
        if (
            config.metadata.junction_area_um2 is not None
            and config.metadata.junction_area_um2 > 0
            and math.isfinite(r0)
        ):
            ra_product = r0 * config.metadata.junction_area_um2

        # 4. Maximum power, voltage, and current on DUT
        powers = np.abs(v_meas * i_meas)
        max_power = float(np.max(powers)) if len(powers) > 0 else 0.0
        max_voltage = float(np.max(np.abs(v_meas))) if len(v_meas) > 0 else 0.0
        max_current = float(np.max(np.abs(i_meas))) if len(i_meas) > 0 else 0.0

        # 5. Linearity (R^2 to Ohm's Law) in non-compliance region
        linearity_r2 = cls._calculate_linearity(i_meas, v_meas, comp_active, r0)

        # 6. Differential curves: dI/dV and dV/dI
        diff_cond_curve, diff_res_curve = cls._compute_differential_curves(i_meas, v_meas)

        # 7. Brinkman-Dynes-Rowell (BDR) tunnel barrier parameter fitting
        bdr_params, bdr_coeffs = cls._fit_bdr_tunnel_model(
            i_meas,
            v_meas,
            comp_active,
            nominal_thickness_nm=config.metadata.nominal_barrier_thickness_nm,
        )

        # 8. Rectification Ratio (asymmetry between positive and negative bias)
        rect_ratio = cls._compute_rectification_ratio(i_meas, v_meas, comp_active)

        phi_bar, delta_phi, thickness = bdr_params

        return ExtractedScientificParameters(
            zero_bias_resistance_ohm=r0,
            zero_bias_conductance_s=g0,
            ra_product_ohm_um2=ra_product,
            compliance_detected=compliance_detected,
            compliance_onset_point=compliance_onset_point,
            clamped_points_fraction=clamped_fraction,
            max_power_dissipated_w=max_power,
            rectification_ratio=rect_ratio,
            tunnel_barrier_height_ev=phi_bar,
            tunnel_barrier_asymmetry_ev=delta_phi,
            tunnel_barrier_thickness_nm=thickness,
            linearity_r2=linearity_r2,
            max_voltage_v=max_voltage,
            max_current_a=max_current,
            bdr_coefficients=bdr_coeffs,
            differential_resistance_curve=diff_res_curve,
            differential_conductance_curve=diff_cond_curve,
        )

    @classmethod
    def _extract_zero_bias_resistance(
        cls,
        i_arr: np.ndarray,
        v_arr: np.ndarray,
        comp_mask: np.ndarray,
    ) -> float:
        """Fit linear slope Delta V / Delta I around zero bias robustly."""
        valid = ~comp_mask
        i_val = i_arr[valid]
        v_val = v_arr[valid]

        # If all points are in compliance (e.g. open circuit or high megaohm), use all available points
        if len(i_val) < 2:
            i_val = i_arr
            v_val = v_arr

        if len(i_val) < 2:
            if len(i_val) == 1 and abs(i_val[0]) > 1e-15:
                return float(v_val[0] / i_val[0])
            return float("nan")

        abs_v = np.abs(v_val)
        max_v = np.max(abs_v) if len(abs_v) > 0 else 1.0
        cutoff = max(0.020, 0.15 * max_v)
        mask = abs_v <= cutoff

        # Ensure we have at least 3 points (or all points if fewer)
        if np.sum(mask) < 3 and len(v_val) >= 3:
            indices = np.argsort(abs_v)[: min(7, len(v_val))]
            mask = np.zeros(len(v_val), dtype=bool)
            mask[indices] = True
        elif np.sum(mask) < 2:
            indices = np.argsort(abs_v)[:2]
            mask = np.zeros(len(v_val), dtype=bool)
            mask[indices] = True

        x = i_val[mask]
        y = v_val[mask]

        if np.ptp(x) < 1e-15:
            non_zero = np.abs(x) > 1e-15
            if np.any(non_zero):
                return float(np.median(y[non_zero] / x[non_zero]))
            return float("nan")

        poly = np.polyfit(x, y, 1)
        return float(poly[0])

    @classmethod
    def _calculate_linearity(
        cls,
        i_arr: np.ndarray,
        v_arr: np.ndarray,
        comp_mask: np.ndarray,
        r0: float,
    ) -> float:
        """Compute R^2 correlation with ideal Ohm's law."""
        valid = ~comp_mask
        i_val = i_arr[valid]
        v_val = v_arr[valid]

        if len(v_val) < 3 or not math.isfinite(r0):
            return 1.0 if len(v_val) >= 1 else 0.0

        v_pred = r0 * i_val
        ss_res = np.sum((v_val - v_pred) ** 2)
        v_mean = np.mean(v_val)
        ss_tot = np.sum((v_val - v_mean) ** 2)

        if ss_tot < 1e-18:
            return 1.0
        r2 = 1.0 - (ss_res / ss_tot)
        return float(np.clip(r2, 0.0, 1.0))

    @classmethod
    def _compute_differential_curves(
        cls,
        i_arr: np.ndarray,
        v_arr: np.ndarray,
    ) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        """Calculate dI/dV (differential conductance) and dV/dI curves."""
        if len(v_arr) < 3:
            return [], []

        # Sort by voltage
        sort_idx = np.argsort(v_arr)
        v_sorted = v_arr[sort_idx]
        i_sorted = i_arr[sort_idx]

        # Filter duplicates in voltage
        unique_mask = np.concatenate(([True], np.diff(v_sorted) > 1e-12))
        v_unique = v_sorted[unique_mask]
        i_unique = i_sorted[unique_mask]

        if len(v_unique) < 3:
            return [], []

        di_dv = np.gradient(i_unique, v_unique)
        cond_curve: list[tuple[float, float]] = []
        res_curve: list[tuple[float, float]] = []

        for v, g in zip(v_unique, di_dv):
            cond_curve.append((float(v), float(g)))
            r_diff = (1.0 / g) if abs(g) > 1e-15 else float("nan")
            res_curve.append((float(v), float(r_diff)))

        return cond_curve, res_curve

    @classmethod
    def _fit_bdr_tunnel_model(
        cls,
        i_arr: np.ndarray,
        v_arr: np.ndarray,
        comp_mask: np.ndarray,
        nominal_thickness_nm: float = 1.0,
    ) -> tuple[tuple[float | None, float | None, float | None], tuple[float, float, float] | None]:
        """Fit Brinkman-Dynes-Rowell G(V) = c0 + c1*V + c2*V^2 and extract barrier parameters."""
        valid = ~comp_mask
        i_val = i_arr[valid]
        v_val = v_arr[valid]

        if len(v_val) < 5:
            return (None, None, None), None

        sort_idx = np.argsort(v_val)
        v_sorted = v_val[sort_idx]
        i_sorted = i_val[sort_idx]

        unique_mask = np.concatenate(([True], np.diff(v_sorted) > 1e-12))
        v_u = v_sorted[unique_mask]
        i_u = i_sorted[unique_mask]

        if len(v_u) < 5 or np.ptp(v_u) < 0.05:
            return (None, None, None), None

        di_dv = np.gradient(i_u, v_u)

        try:
            # Fit quadratic: di_dv = c2 * V^2 + c1 * V + c0
            poly = np.polyfit(v_u, di_dv, 2)
            c2, c1, c0 = float(poly[0]), float(poly[1]), float(poly[2])

            if c0 <= 0 or c2 <= 0:
                # Not a standard parabolic tunnel junction (e.g. metallic or ohmic)
                return (None, None, None), (c0, c1, c2)

            a1 = c1 / c0
            a2 = c2 / c0

            # BDR formulas:
            # a2 = (9/128) * (q_e * A0)^2 / Phi_bar_J
            # a1 = - (A0 * delta_Phi_J / (16 * Phi_bar_J^(3/2))) * q_e
            s_m = max(0.2, float(nominal_thickness_nm)) * 1e-9
            a0 = (4.0 / 3.0) * math.sqrt(2.0 * cls._M_E) / cls._HBAR * s_m

            phi_bar_j = (9.0 / 128.0) * ((cls._Q_E * a0) ** 2) / a2
            phi_bar_ev = phi_bar_j / cls._Q_E

            delta_phi_j = -a1 * 16.0 * (phi_bar_j ** 1.5) / (cls._Q_E * a0)
            delta_phi_ev = delta_phi_j / cls._Q_E

            if 0.1 <= phi_bar_ev <= 15.0:
                return (float(phi_bar_ev), float(delta_phi_ev), float(nominal_thickness_nm)), (c0, c1, c2)
            return (None, None, None), (c0, c1, c2)

        except Exception:
            return (None, None, None), None

    @classmethod
    def _compute_rectification_ratio(
        cls,
        i_arr: np.ndarray,
        v_arr: np.ndarray,
        comp_mask: np.ndarray,
    ) -> float | None:
        """Compute rectification ratio |I(+V_max)| / |I(-V_max)|."""
        valid = ~comp_mask
        i_val = i_arr[valid]
        v_val = v_arr[valid]

        if len(v_val) < 2:
            return None

        pos_mask = v_val > 0
        neg_mask = v_val < 0

        if not np.any(pos_mask) or not np.any(neg_mask):
            return None

        # Find closest symmetric voltages
        max_pos_v = np.max(v_val[pos_mask])
        min_neg_v = np.min(v_val[neg_mask])
        target_v = min(max_pos_v, abs(min_neg_v))

        if target_v < 0.01:
            return None

        # Sort by voltage to ensure monotonically increasing abscissa for np.interp
        sort_idx = np.argsort(v_val)
        v_sorted = v_val[sort_idx]
        i_sorted = i_val[sort_idx]

        # Interpolate currents at +target_v and -target_v
        i_pos = float(np.interp(target_v, v_sorted, i_sorted))
        i_neg = float(np.interp(-target_v, v_sorted, i_sorted))

        if abs(i_neg) < 1e-15:
            return None

        ratio = abs(i_pos / i_neg)
        return float(ratio)

    @classmethod
    def _empty_parameters(cls) -> ExtractedScientificParameters:
        return ExtractedScientificParameters(
            zero_bias_resistance_ohm=float("nan"),
            zero_bias_conductance_s=0.0,
            ra_product_ohm_um2=None,
            compliance_detected=False,
            compliance_onset_point=None,
            clamped_points_fraction=0.0,
            max_power_dissipated_w=0.0,
            rectification_ratio=None,
            tunnel_barrier_height_ev=None,
            tunnel_barrier_asymmetry_ev=None,
            tunnel_barrier_thickness_nm=None,
            linearity_r2=0.0,
        )
