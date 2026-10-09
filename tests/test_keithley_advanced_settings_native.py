"""A modal slot must be tested outside pytest's GUI thread, with a deadline."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows window ownership regression")
def test_advanced_source_settings_real_click_keeps_shell_interactive(tmp_path):
    root = Path(__file__).resolve().parents[1]
    destination = os.environ.get("PYLAB_KEITHLEY_ADVANCED_SCREENSHOTS", str(tmp_path))
    result = subprocess.run(
        [sys.executable, "-m", "tests.keithley_advanced_settings_probe", destination],
        cwd=root, capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("PASS ") == 4, result.stdout
    assert "Traceback (most recent call last)" not in result.stderr, result.stderr
    assert "OpenThemeData() failed" not in result.stderr, result.stderr
    assert "Signal source has been deleted" not in result.stderr, result.stderr
