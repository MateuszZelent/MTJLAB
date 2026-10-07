"""Explicit offline fixtures for every application-owned dialog class."""
import ast
import importlib
import inspect
from types import SimpleNamespace as NS

from PySide6.QtWidgets import QWidget, QMessageBox
from PySide6.QtGui import QImage

from tests.helpers import simulation_settings


def catalogue(root, scratch):
    from app.inventory.models import Sample
    from app.recipes import RecipeNode
    from app.storage.thatec_reader import ThatecRun
    from app.safety.keithley_limit_reconciliation import KeithleyLimitProposal
    from app.devices.keithley_2600.adapter import KeithleyConfigurationReadback, KeithleyChannelConfigurationReadback
    from app.devices.keithley_2600.ui.page import KeithleyConfigurationSnapshot
    from app.devices.anritsu_ms2830a.adapter import AnritsuFullConfigurationReadback
    from app.devices.anritsu_ms2830a.ui.correction_card import SpectrumCorrectionWorkspace
    from app.devices.anritsu_ms2830a.ui.spectrum_controls import PlotRangeController
    from app.ui.widgets import SpectrumPlotWidget

    settings = simulation_settings()
    workspace = SpectrumCorrectionWorkspace(settings, single_sweep_available=True, simulation_mode=True)
    page = QWidget()
    page.correction_workspace = workspace
    page._preview_statistics = None
    from app.spectrum.analysis import SpectrumAnalysisParameters
    page._analysis_parameters = SpectrumAnalysisParameters()
    page._reset_preview_average = lambda: None
    plot = SpectrumPlotWidget()
    # Keep fixture owners alive until the subprocess exits.
    owners = [workspace, page, plot]
    image = QImage(100, 100, QImage.Format.Format_RGB32)
    image.fill(0xff446688)
    image_path = scratch / "fixture.png"
    image.save(str(image_path))
    sample = Sample("review", "Modal review", rows=("1", "2"), cols=("1", "2"))
    def channel(name):
        return KeithleyChannelConfigurationReadback(
            name, False, "normal", "current", 0, .02, False, .01, 1, "2wire", True, 1, True, .01)
    keithley = KeithleyConfigurationReadback((channel("A"), channel("B")))
    anritsu = AnritsuFullConfigurationReadback(
        1e6, 10e6, 5.5e6, 9e6, 0, 1001, True, 1e3, True, "VID", 1e3,
        True, .1, True, 10, "NORM", True, 1)
    from app.devices.keithley_2600.characterization.field_scenario import build_field_scenario
    from tests.test_keithley_field_worker import make_worker
    worker, device = make_worker(scratch)
    scenario = build_field_scenario(worker.config, worker.settings, device.policies, device.policies)
    common = dict(
        parent=None, settings=settings, title="Modal review", text="Review the requested change.",
        header_type="Row", key="1", current_label="Row 1", default_key="3", current_root=scratch,
        image_path=image_path, sample=sample, selected=(), favorites=(),
        current_policy="unchanged", current_window_s=60, ramp_channels={"A", "B"}, deadline="5 s",
        minimum="0 V", maximum="1 V", proposal=KeithleyLimitProposal(("voltage",), "1 V", ()),
        required_devices=("keithley",), display_names={"keithley": "Keithley 2600"},
        primary=QMessageBox.StandardButton.Ok, secondary=None, default_button=QMessageBox.StandardButton.Ok,
        node=RecipeNode("review", "wait", {"duration": "1 s"}),
        definition={"device": "MOKE", "label": "Voltage", "target": "moke_box.vout0.voltage", "dimension": "voltage"},
        path=scratch / "empty.h5", run=ThatecRun(scratch / "empty.h5", {}, (), (), ()), tree=(),
        trace_choices=(("raw", "Raw"),), metadata_values=(), default_destination=scratch / "manual.h5",
        workspace=workspace, directory=scratch, page=page,
        configured={name: KeithleyConfigurationSnapshot(channel=name) for name in ("A", "B")}, form_values={},
        scenario=scenario, curve=NS(index=0, current_a=0, dataset=NS(points=(0, 1, 2))),
    )
    factories = {}
    for path in (root / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            bases = {ast.unparse(base) for base in node.bases}
            if not bases.intersection({"StationDialog", "FluentRecipeDialog", "QDialog", "SweepGeneratorDialog"}):
                continue
            if node.name == "FluentRecipeDialog":
                continue
            module = ".".join(path.relative_to(root).with_suffix("").parts)
            cls = getattr(importlib.import_module(module), node.name)
            kwargs = {}
            for name, param in inspect.signature(cls).parameters.items():
                if param.default is not inspect.Parameter.empty:
                    continue
                if name == "panel":
                    kwargs[name] = QWidget()
                    owners.append(kwargs[name])
                elif name == "readback":
                    kwargs[name] = keithley if "Keithley" in node.name else anritsu
                elif name == "controller":
                    kwargs[name] = PlotRangeController(plot.plot.getViewBox(), plot)
                else:
                    assert name in common, (node.name, name)
                    kwargs[name] = common[name]
            if node.name == "SampleProgrammingDialog":
                kwargs["sample"] = sample
            if node.name == "ElabUploadEditorDialog":
                kwargs["node"] = RecipeNode("upload", "elab_upload", {})
            def construct(cls=cls, kwargs=kwargs):
                values = dict(kwargs)
                if "panel" in values:
                    values["panel"] = QWidget()
                return cls(**values)
            factories[node.name] = construct
    return factories, owners


def inline_factories(scratch, owners):
    """Exercise dialogs built inside methods, which class discovery cannot see."""
    from unittest.mock import MagicMock, patch
    from app.ui.dialogs import StationDialog
    from app.devices.anritsu_ms2830a.ui.page import AnritsuPage
    from app.devices.keithley_2600.ui.page import KeithleyConfigurationPanel
    from app.ui.settings_page import SettingsPage
    from app.settings import SettingsRepository
    from app.ui.inventory.programming_dialog import SampleProgrammingDialog
    from app.ui.dashboard.page import StationDashboardController
    from tests.helpers import SETTINGS_TEMPLATE

    def anritsu(name):
        controller = MagicMock()
        controller.is_connected = False
        controller.visa_address = "SIM::ANRITSU"
        page = AnritsuPage(controller, simulation_settings(), single_sweep_available=True)
        owners.append(page)
        return getattr(page, name)

    def advanced_keithley():
        panel = KeithleyConfigurationPanel(simulation_settings())
        owners.append(panel)
        return panel.advanced_ranges_dialog

    def capture(action):
        dialogs = []
        def cancelled(dialog):
            dialogs.append(dialog)
            return 0
        with patch.object(StationDialog, "exec", cancelled):
            action()
        assert len(dialogs) == 1
        return dialogs[0]

    settings_path = scratch / "settings.yml"
    settings_path.write_text(SETTINGS_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    def settings(method):
        page = SettingsPage(SettingsRepository(settings_path))
        owners.append(page)
        return capture(getattr(page, method))

    def renumber():
        page = SampleProgrammingDialog()
        owners.append(page)
        return capture(page._on_renumber_rows_clicked)

    def trace():
        parent = QWidget()
        owners.append(parent)
        owner = NS(discovery_page=parent, _format_protocol_bytes=StationDashboardController._format_protocol_bytes)
        return capture(lambda: StationDashboardController._show_moke_test_trace(
            owner, "SIM::MOKE", False, "Offline render fixture", b"query", b"reply"))

    return {
        "InlineAnritsuAdvanced": lambda: anritsu("_advanced_dialog"),
        "InlineAnritsuReference": lambda: anritsu("reference_dialog"),
        "InlineAnritsuRecording": lambda: anritsu("recording_dialog"),
        "InlineKeithleyAdvanced": advanced_keithley,
        "InlineSettingsChanges": lambda: settings("show_changes"),
        "InlineUserRoles": lambda: settings("_choose_roles"),
        "InlineRenumberRows": renumber,
        "InlineMokeProtocolTrace": trace,
    }
