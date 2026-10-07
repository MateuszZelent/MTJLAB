"""Offscreen Qt cannot detect the Windows native-child backing-store defect."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native rendering regression")
def test_native_modal_scrolling(tmp_path):
    root = Path(__file__).resolve().parents[1]
    destination = os.environ.get("PYLAB_NATIVE_MODAL_SCREENSHOTS", str(tmp_path))
    result = subprocess.run(
        [sys.executable, "-m", "tests.native_modal_probe", destination],
        cwd=root, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OpenThemeData() failed" not in result.stderr, result.stderr
    assert "Signal source has been deleted" not in result.stderr, result.stderr
    assert "Traceback (most recent call last)" not in result.stderr, result.stderr
    assert result.stdout.count("PASS ") == 24, result.stdout
