import faulthandler
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
from app.devices.anritsu_ms2830a.ui.manual_archive_worker import ManualArchiveWorker
import pytest
original = ManualArchiveWorker.run
def run(self):
    started = time.monotonic()
    print("ARCHIVE START", self.operation, flush=True)
    faulthandler.dump_traceback_later(8)
    try:
        original(self)
    finally:
        faulthandler.cancel_dump_traceback_later()
        print("ARCHIVE END", self.operation, time.monotonic()-started, flush=True)
ManualArchiveWorker.run = run
raise SystemExit(pytest.main(["tests/test_fluent_anritsu_moke_lakeshore_pages.py", "-k", "manual_archive_reconfiguration", "-s", "-q", "-p", "no:cacheprovider", "--basetemp=.tmp-manual-thread-stack"]))
