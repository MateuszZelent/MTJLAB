"""Capture DAC readback without ever sending output or gain commands.

Run from the repository: python -m tools.moke_readonly_probe HOST:PORT.
An invalid reply ends the probe; it never flushes, resynchronizes or retries it.
"""

from __future__ import annotations

import argparse
import json
import socket
import time
from datetime import UTC, datetime

from app.devices.moke_box.protocol import MokeFrame, readback_vout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("endpoint")
    parser.add_argument("--queries", type=int, choices=range(1, 11), default=1)
    args = parser.parse_args()
    host, separator, port = args.endpoint.rpartition(":")
    if not separator or not host:
        parser.error("Use HOST:PORT.")
    with socket.create_connection((host, int(port)), timeout=3) as connection:
        for index in range(args.queries):
            raw = bytearray()
            evidence = {"timestamp_utc": datetime.now(UTC).isoformat(),
                        "query": index, "request_hex": readback_vout().hex(" ")}
            try:
                connection.settimeout(3)
                connection.sendall(readback_vout())  # The only command this probe sends.
                deadline = time.monotonic() + 3
                while len(raw) < 32:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Incomplete VOUT reply")
                    connection.settimeout(remaining)
                    part = connection.recv(32 - len(raw))
                    if not part:
                        raise ConnectionError("TCP closed during VOUT reply")
                    raw.extend(part)
                frames = [MokeFrame.decode(bytes(raw[i:i + 4])) for i in range(0, 32, 4)]
                if any(frame.origin not in {0, 3} or frame.record_type != 2 for frame in frames):
                    raise ValueError("Unexpected VOUT record origin or type")
                if sorted(frame.channel for frame in frames) != list(range(8)):
                    raise ValueError("VOUT channels are missing or duplicated")
                evidence["valid"] = True
            except Exception as exc:  # noqa: BLE001 - retain the exact reply before exiting
                evidence.update(valid=False, error=str(exc))
            evidence["reply_hex"] = raw.hex(" ")
            print(json.dumps(evidence), flush=True)
            if not evidence["valid"]:
                return 1
            if index + 1 < args.queries:
                time.sleep(0.1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
