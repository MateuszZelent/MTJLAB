"""Bounded, ordered CPU/storage worker for quantitative spectrum sessions."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from threading import Event, Lock
import time

import numpy as np
from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot

from app.devices.anritsu_ms2830a.adapter import SpectrumTrace
from app.domain.models import MeasurementPoint
from app.domain.spectrum_decisions import SpectrumDecisionOperation, SpectrumProcessingChange
from app.domain.spectrum_finalization import (
    SpectrumFinalizationBatchRequest, SpectrumFinalizationRequest,
    SpectrumFinalizationResumeRequest, SpectrumResumeInspectionRequest,
)
from app.domain.errors import ProcessingCancelled
from app.domain.spectrum_correction import (
    BackgroundProfile, CorrectedSpectrumFrame, CorrectionConfig,
    SpectrumAcquisitionContext, SpectrumFrameEnvelope,
)
from app.domain.spectrum_interference import SpectrumInterferenceCalibration
from app.domain.spectrum_interleaved import InterleavedSpectrumConfig, SpectrumOperatorStateConfirmation
from app.spectrum.realtime_processor import RealtimeSpectrumProcessor
from app.spectrum.resonance_bootstrap import ResonanceBootstrapConfig
from app.spectrum.spectral_difference_test import SpectralDifferenceTestConfig
from app.spectrum.reference_diagnostics import ReferenceDiagnosticConfig
from app.storage.hdf5_writer import Hdf5RunWriter
from app.storage.background_profile_store import BackgroundProfileHdf5Store


@dataclass(frozen=True, slots=True)
class CorrectionSessionRequest:
    context: SpectrumAcquisitionContext
    config: CorrectionConfig
    archive_path: Path
    settings_source: str
    device_idn: str
    profile: BackgroundProfile | None = None
    reference_state: str | None = None
    signal_free_qualified: bool = False
    simulation_mode: bool | None = None
    interference_calibration: SpectrumInterferenceCalibration | None = None
    interleaved_config: InterleavedSpectrumConfig | None = None
    operator_confirmation: SpectrumOperatorStateConfirmation | None = None

    def __post_init__(self):
        if self.simulation_mode is not None and type(self.simulation_mode) is not bool:
            raise ValueError("Spectrum session simulation mode must be an explicit boolean or unknown.")
        if self.interleaved_config is not None:
            if (not isinstance(self.interleaved_config, InterleavedSpectrumConfig)
                    or self.profile is not None or self.interference_calibration is not None
                    or self.reference_state is None or self.signal_free_qualified
                    or not isinstance(self.operator_confirmation, SpectrumOperatorStateConfirmation)
                    or self.operator_confirmation.role.value != "reference"
                    or self.operator_confirmation.description != self.reference_state
                    or self.config.minimum_reference_sweeps != self.interleaved_config.minimum_reference_sweeps):
                raise ValueError("Interleaved sessions begin with an operator-confirmed, unqualified REF block.")
        elif self.operator_confirmation is not None:
            raise ValueError("Initial operator confirmation belongs to the interleaved protocol.")


@dataclass(frozen=True, slots=True)
class CorrectionViewSnapshot:
    corrected: CorrectedSpectrumFrame
    source_raw: SpectrumTrace


@dataclass(frozen=True, slots=True)
class SpectrumFinalizationSourcesRequest:
    signal_path: Path
    before_path: Path
    after_path: Path


@dataclass(frozen=True, slots=True)
class SpectrumInterferenceImportRequest:
    path: Path
    context: SpectrumAcquisitionContext
    profile: BackgroundProfile
    config: CorrectionConfig


@dataclass(frozen=True, slots=True)
class SpectrumResonanceBootstrapRequest:
    reference_path: Path
    signal_path: Path
    destination: Path
    block_sweeps: int
    initial_center_hz: float
    initial_fwhm_hz: float
    shape: str = "gaussian"
    discard_partial_tail: bool = False
    config: ResonanceBootstrapConfig = ResonanceBootstrapConfig()


@dataclass(frozen=True, slots=True)
class SpectrumDifferenceRequest:
    reference_path: Path
    signal_path: Path
    destination: Path
    block_sweeps: int
    search_start_hz: float
    search_stop_hz: float
    discard_partial_tail: bool = False
    config: SpectralDifferenceTestConfig = SpectralDifferenceTestConfig()


@dataclass(frozen=True, slots=True)
class SpectrumInterferenceTrainingRequest:
    reference_path: Path
    destination: Path
    specification_json: str

    def __post_init__(self):
        if not isinstance(self.specification_json, str) or len(self.specification_json.encode("utf-8")) > 65536:
            raise ValueError("Training specification must be bounded immutable JSON text.")
        if not isinstance(json.loads(self.specification_json), dict):
            raise ValueError("Training specification must be a JSON object.")


@dataclass(frozen=True, slots=True)
class SpectrumInterferenceValidationRequest:
    model_path: Path
    reference_path: Path
    destination: Path
    model_id: str | None = None
    validation_regions_json: str | None = None

    def __post_init__(self):
        if self.model_id is not None and (not isinstance(self.model_id, str) or not self.model_id.strip()):
            raise ValueError("Model identifier must be nonempty text or unspecified.")
        if self.validation_regions_json is not None:
            value = self.validation_regions_json
            if not isinstance(value, str) or len(value.encode("utf-8")) > 65536:
                raise ValueError("Validation regions must be bounded immutable JSON text.")
            regions = json.loads(value)
            if (not isinstance(regions, list) or not 1 <= len(regions) <= 32
                    or any(not isinstance(pair, list) or len(pair) != 2
                           or any(not isinstance(bound, str) for bound in pair) for pair in regions)):
                raise ValueError("Validation regions require 1 to 32 explicit quantity pairs.")


@dataclass(frozen=True, slots=True)
class SpectrumReferenceDiagnosticRequest:
    reference_path: Path
    destination: Path
    bin_indices: tuple[int, ...] | None = None
    config: ReferenceDiagnosticConfig = ReferenceDiagnosticConfig()

    def __post_init__(self):
        if not isinstance(self.config, ReferenceDiagnosticConfig):
            raise ValueError("Reference diagnostics require a typed bounded configuration.")
        bins = self.bin_indices
        if bins is not None and (not isinstance(bins, tuple) or not 1 <= len(bins) <= self.config.maximum_bins
                or any(type(index) is not int or index < 0 for index in bins) or len(set(bins)) != len(bins)):
            raise ValueError("Diagnostic bins must be a bounded immutable tuple of unique nonnegative indices.")


class _CorrectionWorker(QObject):
    completed = Signal(str, object)
    failed = Signal(str, str)
    cancelled = Signal(str)
    shutdown = Signal()
    processing_progress = Signal(str, int, int)

    def __init__(self, queue, lock, wake, queue_state, processing_cancel) -> None:
        super().__init__()
        self._queue = queue
        self._lock = lock
        self._wake = wake
        self._queue_state = queue_state
        self._processing_cancel = processing_cancel
        self._processor: RealtimeSpectrumProcessor | None = None
        self._writer: Hdf5RunWriter | None = None
        self._archive_path: Path | None = None
        self._latest_signal_raw: SpectrumTrace | None = None

    @Slot()
    def drain(self) -> None:
        # Return to the event loop after a bounded batch so queued stop and
        # shutdown requests are not starved by a never-ending processing loop.
        for _ in range(4):
            with self._lock:
                if not self._queue:
                    self._queue_state["wake_pending"] = False
                    return
                operation, payload = self._queue.popleft()
                if operation == "snapshot":
                    self._queue_state["snapshot_pending"] = False
            try:
                result = self._execute(operation, payload)
                self.completed.emit(operation, result)
            except ProcessingCancelled:
                self.cancelled.emit(operation)
            except Exception as exc:
                try:
                    self._close_writer("faulted")
                except Exception as close_error:
                    self.failed.emit("archive_close", str(close_error))
                self._processor = None
                self.failed.emit(operation, str(exc))
            if operation == "shutdown":
                self.shutdown.emit()
                return
        with self._lock:
            if not self._queue:
                self._queue_state["wake_pending"] = False
                return
        self._wake.emit()

    def _check_processing_cancel(self):
        if self._processing_cancel.is_set():
            raise ProcessingCancelled("Offline spectrum processing canceled.")

    def _execute(self, operation, payload):
        if operation == "diagnose_reference":
            from app.storage.reference_diagnostic_store import diagnose_reference_archive

            if self._writer is not None or not isinstance(payload, SpectrumReferenceDiagnosticRequest):
                raise ValueError("Reference diagnostics require an idle worker and typed closed archive paths.")
            self._check_processing_cancel()
            self.processing_progress.emit(operation, 0, 0)
            _diagnostics, report = diagnose_reference_archive(payload.reference_path, destination=payload.destination,
                bin_indices=payload.bin_indices, config=payload.config, cancellation_check=self._check_processing_cancel)
            return payload.destination, report
        if operation == "validate_interference":
            from app.storage.interference_validation_store import validate_interference_archive

            if self._writer is not None or not isinstance(payload, SpectrumInterferenceValidationRequest):
                raise ValueError("Model validation requires an idle worker and typed closed archive paths.")
            self._check_processing_cancel()
            self.processing_progress.emit(operation, 0, 0)
            report = validate_interference_archive(payload.model_path, payload.reference_path, payload.destination,
                model_id=payload.model_id,
                validation_regions=(json.loads(payload.validation_regions_json)
                                    if payload.validation_regions_json is not None else None),
                cancellation_check=self._check_processing_cancel)
            return payload.destination, report
        if operation == "train_interference":
            from app.storage.interference_training_store import train_from_specification

            if self._writer is not None or not isinstance(payload, SpectrumInterferenceTrainingRequest):
                raise ValueError("Reference training requires an idle worker and a typed immutable specification.")
            self._check_processing_cancel()
            self.processing_progress.emit(operation, 0, 0)
            calibration = train_from_specification(payload.reference_path, payload.destination,
                json.loads(payload.specification_json), cancellation_check=self._check_processing_cancel)
            return payload.destination, calibration
        if operation in {"bootstrap_resonance", "spectral_difference"}:
            from app.storage.resonance_bootstrap_store import bootstrap_resonance_archives
            from app.storage.spectral_difference_store import analyze_spectral_difference_archives

            request_type = SpectrumResonanceBootstrapRequest if operation == "bootstrap_resonance" else SpectrumDifferenceRequest
            if self._writer is not None or not isinstance(payload, request_type):
                raise ValueError("Spectrum analysis requires an idle worker and typed closed archive paths.")
            self._check_processing_cancel()
            steps = payload.config.resamples if operation == "bootstrap_resonance" else payload.config.permutations
            self.processing_progress.emit(operation, 0, steps)
            last_progress = 0.

            def progress(done, total):
                nonlocal last_progress
                now = time.monotonic()
                if now - last_progress >= .1 or done == total:
                    self.processing_progress.emit(operation, done, total)
                    last_progress = now

            if operation == "bootstrap_resonance":
                report = bootstrap_resonance_archives(payload.reference_path, payload.signal_path,
                    payload.destination, block_sweeps=payload.block_sweeps,
                    initial_center_hz=payload.initial_center_hz, initial_fwhm_hz=payload.initial_fwhm_hz,
                    shape=payload.shape, discard_partial_tail=payload.discard_partial_tail,
                    config=payload.config, cancellation_check=self._check_processing_cancel, progress_callback=progress)
            else:
                report = analyze_spectral_difference_archives(payload.reference_path, payload.signal_path,
                    payload.destination, block_sweeps=payload.block_sweeps,
                    search_start_hz=payload.search_start_hz, search_stop_hz=payload.search_stop_hz,
                    discard_partial_tail=payload.discard_partial_tail, config=payload.config,
                    cancellation_check=self._check_processing_cancel, progress_callback=progress)
            return payload.destination, report
        if operation == "import_interference":
            from app.storage.hdf5_reader import Hdf5RunReader

            if self._writer is not None or not isinstance(payload, SpectrumInterferenceImportRequest):
                raise ValueError("Model import requires an idle worker and typed reference context.")
            processor = RealtimeSpectrumProcessor(payload.context, payload.config)
            processor.set_background_profile(payload.profile)
            compatible, rejected = [], []
            for calibration in Hdf5RunReader.interference_calibrations(payload.path):
                try:
                    processor.set_interference_calibration(calibration)
                except ValueError as exc:
                    rejected.append((calibration.model_id, str(exc)))
                else:
                    compatible.append(calibration)
            if not compatible:
                detail = rejected[0][1] if rejected else "No committed interference models in this archive."
                raise ValueError(f"No usable interference calibration: {detail}")
            return payload.path, tuple(compatible), tuple(rejected)
        if operation == "inspect_finalization":
            from app.storage.finalized_spectrum_store import inspect_finalization_sources

            if self._writer is not None or not isinstance(payload, SpectrumFinalizationSourcesRequest):
                raise ValueError("Profile selection requires idle processing and typed source paths.")
            return inspect_finalization_sources((payload.signal_path, payload.before_path, payload.after_path),
                cancellation_check=self._check_processing_cancel)
        if operation == "inspect_interleaved_blocks":
            from app.storage.finalized_spectrum_store import inspect_interleaved_blocks

            if self._writer is not None or not isinstance(payload, Path):
                raise ValueError("Interleaved block selection requires an idle worker and an explicit path.")
            return inspect_interleaved_blocks(payload, cancellation_check=self._check_processing_cancel)
        if operation == "finalize":
            from app.storage.finalized_spectrum_store import finalize_spectrum_archives
            from app.storage.hdf5_reader import Hdf5RunReader

            if self._writer is not None or not isinstance(payload, SpectrumFinalizationRequest):
                raise ValueError("Finalization requires an idle worker and typed source paths.")
            block = finalize_spectrum_archives(payload.signal_path, payload.before_path,
                                              payload.after_path, payload.destination,
                                              point_indices=payload.point_indices,
                                              before_profile_id=payload.before_profile_id,
                                              after_profile_id=payload.after_profile_id,
                                              signal_profile_id=payload.signal_profile_id,
                                              cancellation_check=self._check_processing_cancel)
            profiles = Hdf5RunReader.background_profiles(payload.destination)
            return payload.destination, block, profiles
        if operation == "inspect_batch_resume":
            from app.storage.spectrum_finalization_batch_store import inspect_spectrum_resume

            if self._writer is not None or not isinstance(payload, SpectrumResumeInspectionRequest):
                raise ValueError("Resume inspection requires idle processing and a typed journal selection.")
            return inspect_spectrum_resume(payload, cancellation_check=self._check_processing_cancel)
        if operation in {"finalize_batch", "resume_batch"}:
            from app.storage.spectrum_finalization_batch_store import finalize_spectrum_batch, resume_spectrum_batch
            from app.storage.hdf5_reader import Hdf5RunReader

            expected_type = SpectrumFinalizationBatchRequest if operation == "finalize_batch" else SpectrumFinalizationResumeRequest
            if self._writer is not None or not isinstance(payload, expected_type):
                raise ValueError("Batch finalization requires idle processing and typed block selections.")
            process = finalize_spectrum_batch if operation == "finalize_batch" else resume_spectrum_batch
            records = process(payload, cancellation_check=self._check_processing_cancel,
                progress_callback=lambda done, total, _path: self.processing_progress.emit(operation, done, total))
            destination = Path(records[-1]["destination"])
            block = Hdf5RunReader.finalized_spectrum_blocks(destination)[0][0]
            profiles = Hdf5RunReader.background_profiles(destination)
            return payload.journal_path, records, (destination, block, profiles)
        if operation == "import_profile":
            return BackgroundProfileHdf5Store.load(payload)
        if operation == "export_profile":
            context, profile, destination = payload
            BackgroundProfileHdf5Store.save(destination, context, profile)
            return destination
        if operation == "start":
            if self._writer is not None:
                raise ValueError("A spectrum correction session is already open.")
            if not isinstance(payload, CorrectionSessionRequest):
                raise ValueError("Invalid spectrum correction session.")
            processor = RealtimeSpectrumProcessor(payload.context, payload.config)
            if payload.profile is not None:
                processor.set_background_profile(payload.profile)
            if payload.reference_state is not None:
                processor.begin_reference(
                    payload.reference_state, signal_free_qualified=payload.signal_free_qualified,
                )
            if payload.interference_calibration is not None:
                processor.set_interference_calibration(payload.interference_calibration)
            writer = Hdf5RunWriter(
                payload.archive_path,
                recipe_source="schema_version: 1\nname: Quantitative spectrum session\nsteps: []\n",
                settings_source=payload.settings_source, plan_hash=payload.context.context_id,
                device_idn={"anritsu": payload.device_idn or "ANRITSU,UNKNOWN"},
                simulation_metadata={
                    "enabled": payload.simulation_mode,
                    "mode": ("unknown" if payload.simulation_mode is None
                             else "simulation" if payload.simulation_mode else "hardware"),
                    "mode_source": "not_provided" if payload.simulation_mode is None else "application_runtime",
                    "manual_spectrum": True,
                },
                run_attributes={
                    "spectrum_correction_schema": "spectrum-correction-v1",
                    "spectrum_processing_decision_schema": "spectrum-processing-decisions-v1",
                    "spectrum_correction_config_json": json.dumps(asdict(payload.config), sort_keys=True),
                    "spectrum_correction_initial_profile_id": (
                        payload.profile.profile_id if payload.profile is not None else ""
                    ),
                    "spectrum_correction_initial_reference_state": payload.reference_state or "",
                    "spectrum_correction_initial_signal_free_qualified": payload.signal_free_qualified,
                    "spectrum_correction_initial_interference_model_id": (
                        payload.interference_calibration.model_id if payload.interference_calibration else ""
                    ),
                    "spectrum_correction_acquisition_mode": (
                        "operator-interleaved-v1" if payload.interleaved_config is not None else "manual-refresh-v1"
                    ),
                    "spectrum_correction_interleaved_config_json": json.dumps(
                        asdict(payload.interleaved_config) if payload.interleaved_config is not None else None,
                        sort_keys=True,
                    ),
                },
            )
            self._writer = writer
            self._processor = processor
            self._archive_path = payload.archive_path
            self._latest_signal_raw = None
            if payload.profile is not None:
                writer.store_background_profile(payload.context, payload.profile)
            if payload.interference_calibration is not None:
                writer.store_interference_calibration(payload.interference_calibration)
            writer.initialize_spectrum_decisions(payload.context, payload.config)
            writer.record_spectrum_decision("initialize", {
                "profile_id": None if payload.profile is None else payload.profile.profile_id,
                "profile_hash": None if payload.profile is None else payload.profile.content_hash,
                "reference_state": payload.reference_state,
                "signal_free_qualified": payload.signal_free_qualified,
                "model_id": None if payload.interference_calibration is None else payload.interference_calibration.model_id,
                "model_hash": None if payload.interference_calibration is None else payload.interference_calibration.content_hash,
            })
            if payload.operator_confirmation is not None:
                self._record_operator_confirmation(payload.operator_confirmation)
            return payload.archive_path
        if operation in {"stop", "shutdown"}:
            self._close_writer("incomplete" if operation == "shutdown" else str(payload))
            self._processor = None
            return self._archive_path
        if operation == "snapshot" and (self._processor is None or self._writer is None):
            # A GUI timer may already have queued a read when Stop closes the
            # session. This is an empty mailbox, not an acquisition fault.
            return None
        if self._processor is None or self._writer is None:
            raise ValueError("No quantitative spectrum session is active.")
        if operation == "operator_state_confirmation":
            self._record_operator_confirmation(payload)
            return payload
        if operation == "reference":
            profile = self._processor.finish_reference()
            self._writer.store_background_profile(self._processor.context, profile)
            self._writer.record_spectrum_decision("finish_reference", {
                "profile_id": profile.profile_id, "profile_hash": profile.content_hash,
            })
            return profile
        if operation == "processing_change":
            if not isinstance(payload, SpectrumProcessingChange):
                raise ValueError("Processing changes require a typed immutable request.")
            change, params = payload.operation, {}
            if change == SpectrumDecisionOperation.SET_PROFILE:
                self._writer.store_background_profile(self._processor.context, payload.profile)
                self._processor.set_background_profile(payload.profile)
                params = {"profile_id": payload.profile.profile_id, "profile_hash": payload.profile.content_hash}
            elif change == SpectrumDecisionOperation.BEGIN_REFERENCE:
                self._processor.begin_reference(payload.reference_state, signal_free_qualified=payload.signal_free_qualified)
                params = {"reference_state": payload.reference_state, "signal_free_qualified": payload.signal_free_qualified}
            elif change == SpectrumDecisionOperation.CANCEL_REFERENCE:
                self._processor.cancel_reference()
            elif change == SpectrumDecisionOperation.RESET_SEGMENT:
                self._processor.reset_segment()
            elif change == SpectrumDecisionOperation.SET_MODEL:
                self._writer.store_interference_calibration(payload.calibration)
                self._processor.set_interference_calibration(payload.calibration)
                params = {"model_id": payload.calibration.model_id, "model_hash": payload.calibration.content_hash}
            self._writer.record_spectrum_decision(change.value, params)
            self._latest_signal_raw = None
            return change.value
        if operation == "snapshot":
            if payload is not None:
                if type(payload) not in (float, int) or not np.isfinite(payload) or payload <= 0:
                    raise ValueError("Snapshot status requires a finite positive timestamp.")
                previous_quality = self._processor.quality
                self._processor.status_at(float(payload))
                if previous_quality != self._processor.quality:
                    self._writer.record_spectrum_decision("status_at", {
                        "at_s": float(payload), "quality": self._processor.quality.value,
                    })
            result = self._processor.snapshot()
            return (
                CorrectionViewSnapshot(result, self._latest_signal_raw)
                if result is not None and self._latest_signal_raw is not None else None
            )
        if operation != "frame":
            raise ValueError(f"Unknown correction operation {operation!r}.")
        envelope, trace = payload
        if not isinstance(envelope, SpectrumFrameEnvelope) or not isinstance(trace, SpectrumTrace):
            raise ValueError("Correction ingest requires a typed envelope and raw trace.")
        if not np.array_equal(trace.frequencies_hz, self._processor.context.frequencies_hz):
            raise ValueError("Raw trace frequency grid differs from its acquisition context.")
        accepted = self._processor.ingest(envelope, trace.powers_dbm)
        result = self._processor.snapshot()
        if result is not None and (not accepted or result.frame_id != envelope.frame_id):
            result = None
        index = self._writer.point_count
        self._writer.append(
            MeasurementPoint(index, {}, {}, metadata={
                "frame_id": envelope.frame_id, "segment_id": envelope.segment_id,
                "quantitative_accepted": accepted,
            }), trace, acquisition_envelope=envelope, corrected_frame=result,
            processed_values=result.values_w if result is not None else None,
            processed_unit="W" if result is not None else None,
            processing_operation="signed_reference" if result is not None else "none",
        )
        # Publish only after the raw/corrected checkpoint has committed. This
        # is the same immutable result already computed for storage; drawing
        # remains controlled by the GUI's independent render timer.
        if result is not None:
            self._latest_signal_raw = trace
        return {
            "frame_id": envelope.frame_id,
            "envelope": envelope,
            "reference_count": self._processor.calibration_count,
            "quality": str(self._processor.quality), "accepted": accepted,
            "committed_point_count": self._writer.point_count,
            "view": CorrectionViewSnapshot(result, trace) if result is not None else None,
        }

    def _record_operator_confirmation(self, confirmation):
        if not isinstance(confirmation, SpectrumOperatorStateConfirmation) or self._writer is None:
            raise ValueError("State confirmation requires a typed operator report and an open archive.")
        self._writer.append_event("spectrum_operator_state_confirmed", {
            "role": confirmation.role.value, "state_description": confirmation.description,
            "stable_state_confirmed": True, "evidence_source": "operator_report",
            "hardware_readback_verified": False, "signal_free_qualified": False,
            "before_point_index": self._writer.point_count,
        })

    def _close_writer(self, status):
        writer = self._writer
        if writer is not None:
            writer.close(status)
            self._writer = None


class SpectrumCorrectionController(QObject):
    """FIFO processing/archive; only snapshot requests may be coalesced.

    No instrument calls happen in this worker. The producer may request the
    next sweep after the `frame` completion acknowledgement, providing natural
    backpressure while the GUI renders at an independent rate.
    """

    _wake = Signal()
    completed = Signal(str, object)
    failed = Signal(str, str)
    cancelled = Signal(str)
    processing_progress = Signal(str, int, int)

    def __init__(self, parent=None, *, queue_frames=8) -> None:
        super().__init__(parent)
        if not 1 <= queue_frames <= 256:
            raise ValueError("Correction FIFO limit must be in 1..256.")
        self._queue = deque()
        self._lock = Lock()
        self._queue_limit = queue_frames
        self._queue_state = {"wake_pending": False, "snapshot_pending": False}
        self._closing = False
        self._processing_cancel = Event()
        self._offline_busy = False
        self._session_pending = False
        self._thread = QThread(self)
        self._thread.setObjectName("anritsu-spectrum-correction")
        self._worker = _CorrectionWorker(self._queue, self._lock, self._wake, self._queue_state,
                                         self._processing_cancel)
        self._worker.moveToThread(self._thread)
        self._wake.connect(self._worker.drain, Qt.ConnectionType.QueuedConnection)
        self._worker.completed.connect(self._offline_finished)
        self._worker.completed.connect(self.completed)
        self._worker.failed.connect(self._offline_finished)
        self._worker.failed.connect(self._acquisition_failed)
        self._worker.failed.connect(self.failed)
        self._worker.cancelled.connect(self._offline_finished)
        self._worker.cancelled.connect(self.cancelled)
        self._worker.processing_progress.connect(self.processing_progress)
        self._worker.shutdown.connect(self._thread.quit, Qt.ConnectionType.DirectConnection)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.start()

    @property
    def queued_count(self):
        with self._lock:
            return len(self._queue)

    def _submit(self, operation, payload=None):
        if self._closing:
            raise ValueError("Spectrum correction worker is shutting down.")
        wake = False
        with self._lock:
            if operation == "snapshot" and self._queue_state["snapshot_pending"]:
                return
            if len(self._queue) >= self._queue_limit:
                raise BufferError("Spectrum correction FIFO is full; pause acquisition.")
            self._queue.append((operation, payload))
            if operation == "snapshot":
                self._queue_state["snapshot_pending"] = True
            if not self._queue_state["wake_pending"]:
                self._queue_state["wake_pending"] = True
                wake = True
        if wake:
            self._wake.emit()

    def start_session(self, request: CorrectionSessionRequest):
        if self._offline_busy or self._session_pending:
            raise ValueError("Spectrum worker already has acquisition or offline processing pending.")
        self._session_pending = True
        try:
            self._submit("start", request)
        except (ValueError, BufferError):
            self._session_pending = False
            raise

    def ingest(self, envelope: SpectrumFrameEnvelope, trace: SpectrumTrace):
        self._submit("frame", (envelope, trace))

    def request_snapshot(self, at_s: float | None = None):
        self._submit("snapshot", at_s)

    def finish_reference(self):
        self._submit("reference")

    def apply_processing_change(self, change: SpectrumProcessingChange):
        if not self._session_pending or self._offline_busy:
            raise ValueError("Processing changes require an active acquisition session.")
        self._submit("processing_change", change)

    def confirm_operator_state(self, confirmation: SpectrumOperatorStateConfirmation):
        if not self._session_pending or self._offline_busy:
            raise ValueError("State confirmation requires an active acquisition session.")
        if not isinstance(confirmation, SpectrumOperatorStateConfirmation):
            raise ValueError("State confirmation must be an immutable typed report.")
        self._submit("operator_state_confirmation", confirmation)

    def stop_session(self, status="completed"):
        if status not in {"completed", "incomplete", "faulted", "aborted"}:
            raise ValueError("Unknown spectrum session terminal status.")
        self._submit("stop", status)

    def load_profile(self, path: Path):
        self._submit("import_profile", path)

    def save_profile(self, context, profile, destination: Path):
        self._submit("export_profile", (context, profile, destination))

    def load_interference_calibrations(self, request: SpectrumInterferenceImportRequest):
        self._submit("import_interference", request)

    def finalize_archives(self, request: SpectrumFinalizationRequest):
        self._submit_offline("finalize", request)

    def finalize_batch(self, request: SpectrumFinalizationBatchRequest):
        self._submit_offline("finalize_batch", request)

    def resume_batch(self, request: SpectrumFinalizationResumeRequest):
        self._submit_offline("resume_batch", request)

    def inspect_batch_resume(self, request: SpectrumResumeInspectionRequest):
        self._submit_offline("inspect_batch_resume", request)

    def inspect_finalization_sources(self, request: SpectrumFinalizationSourcesRequest):
        self._submit_offline("inspect_finalization", request)

    def inspect_interleaved_blocks(self, path: Path):
        if not isinstance(path, Path):
            raise ValueError("Interleaved block selection requires an explicit path.")
        self._submit_offline("inspect_interleaved_blocks", path)

    def bootstrap_archives(self, request: SpectrumResonanceBootstrapRequest):
        self._submit_offline("bootstrap_resonance", request)

    def difference_archives(self, request: SpectrumDifferenceRequest):
        self._submit_offline("spectral_difference", request)

    def train_interference_archive(self, request: SpectrumInterferenceTrainingRequest):
        self._submit_offline("train_interference", request)

    def validate_interference_archive(self, request: SpectrumInterferenceValidationRequest):
        self._submit_offline("validate_interference", request)

    def diagnose_reference_archive(self, request: SpectrumReferenceDiagnosticRequest):
        self._submit_offline("diagnose_reference", request)

    def _submit_offline(self, operation, request):
        if self._closing:
            raise ValueError("Spectrum correction worker is shutting down.")
        if self._offline_busy or self._session_pending:
            raise ValueError("Offline spectrum processing is already pending.")
        self._processing_cancel.clear()
        self._offline_busy = True
        try:
            self._submit(operation, request)
        except (ValueError, BufferError):
            self._offline_busy = False
            raise

    def _offline_finished(self, operation, *args):
        if operation in {"finalize", "finalize_batch", "resume_batch", "inspect_batch_resume",
                         "bootstrap_resonance", "spectral_difference", "train_interference", "validate_interference",
                         "diagnose_reference", "inspect_finalization", "inspect_interleaved_blocks"}:
            self._offline_busy = False
        if operation == "stop":
            self._session_pending = False

    def _acquisition_failed(self, *_args):
        # Worker failures close the writer before publishing their failure.
        self._session_pending = False

    def cancel_processing(self):
        """Interrupt offline computation; acquisition checkpoints still drain."""
        self._processing_cancel.set()

    def close(self, *, wait_ms=500) -> bool:
        """Drain accepted frames and release HDF5; never wait indefinitely in GUI."""
        if not self._closing:
            self.cancel_processing()
            self._closing = True
            with self._lock:
                self._queue.append(("shutdown", None))
                self._queue_state["wake_pending"] = True
            self._wake.emit()
        return self._thread.wait(wait_ms)
