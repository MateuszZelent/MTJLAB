"""Opening a historical sweep reuses the archive snapshot already read."""
from app.storage import Hdf5RunWriter, ThatecRunReader
from app.ui.results.page import ResultsPage
from tests.shell_test_isolation import shell_qt_application as shell_qt_application  # noqa: PLC0414
from tests.test_spectrum_correction_controller import wait_until


def test_open_historical_sweep_never_reopens_hdf5_on_gui(shell_qt_application, tmp_path, monkeypatch):
    app = shell_qt_application
    path = tmp_path / "run.h5"
    writer = Hdf5RunWriter(path, recipe_source="name: snapshot\n", settings_source="schema_version: 1\n",
                           plan_hash="snapshot", device_idn={})
    writer.close("completed")
    page = ResultsPage(str(tmp_path))
    requested = []
    page.open_sweep_requested.connect(lambda run, tree: requested.append((run, tree)))
    try:
        page.runs.setCurrentItem(page.runs.topLevelItem(0))
        wait_until(app, lambda: page._result_task is None)
        assert page.open_sweep_button.isEnabled()
        original_run, original_tree = page._thatec_run, page._thatec_tree
        def forbidden(*args):
            raise AssertionError("Historical sweep must reuse the loaded tree")
        monkeypatch.setattr(ThatecRunReader, "tree", forbidden)
        page.open_sweep_button.click()
        assert len(requested) == 1
        assert requested[0][0] is original_run and requested[0][1] is original_tree
        page._on_file_selected(None)
        page._request_open_sweep()
        assert len(requested) == 1
        assert page._thatec_tree == ()
    finally:
        assert page.shutdown()
        page.close()
        page.deleteLater()
        app.processEvents()
