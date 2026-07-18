"""Deterministic, run-scoped data sources for simulated instruments."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import random


@dataclass(frozen=True, slots=True)
class SimulationContext:
    """Create stable independent pseudo-random streams for one simulated run.

    The stable digest deliberately avoids Python's process-randomised ``hash``.
    A consumer retains the returned ``Random`` instance for its own sequence.
    """

    seed: int
    model_version: str = "1"
    time_scale: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.seed, int) or not 0 <= self.seed < 2**64:
            raise ValueError("Simulation seed must be an unsigned 64-bit integer.")
        if self.time_scale < 0:
            raise ValueError("Simulation time scale cannot be negative.")

    def random_stream(self, device_key: str, stream_key: str) -> random.Random:
        material = (
            f"{self.seed}:{self.model_version}:{device_key}:{stream_key}".encode("utf-8")
        )
        stream_seed = int.from_bytes(hashlib.sha256(material).digest()[:16], "big")
        return random.Random(stream_seed)

    def metadata(self, devices: list[str] | tuple[str, ...]) -> dict[str, object]:
        return {
            "enabled": True,
            "seed": self.seed,
            "model_version": self.model_version,
            "time_scale": self.time_scale,
            "devices": sorted(set(devices)),
        }
