import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows dialog catalogue")
def test_complete_modal_catalogue(tmp_path):
    root = Path(__file__).resolve().parents[1]
    destination = Path(os.environ.get("PYLAB_ALL_MODAL_SCREENSHOTS", str(tmp_path)))
    env = dict(os.environ)
    env.pop("PYLAB_MODAL_ONLY", None)
    result = subprocess.run(
        [sys.executable, "-m", "tests.all_modal_probe", str(destination)],
        cwd=root, env=env, capture_output=True, text=True, timeout=240,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    states = json.loads((destination / "catalogue.json").read_text(encoding="utf-8"))
    names = {state["dialog"] for state in states}
    assert len(names) >= 56  # AST discovery includes new classes automatically.
    assert len(states) == 4 * len(names)
