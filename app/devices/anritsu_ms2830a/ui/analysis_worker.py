"""Coalescing CPU worker for display-only spectrum analysis."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot

from app.spectrum import SpectrumCleanupResult, SpectrumPeak, clean_spectrum_dbm, detect_spectrum_peaks


@dataclass(frozen=True, slots=True)
class SpectrumAnalysisRequest:
    generation: int
    frequencies_hz: tuple[float, ...]
    powers_dbm: tuple[float, ...]
    mode: str
    history_dbm: tuple[tuple[float, ...], ...]
    detect_peaks: bool


@dataclass(frozen=True, slots=True)
class SpectrumAnalysisOutcome:
    generation: int
    cleanup: SpectrumCleanupResult
    peaks: tuple[SpectrumPeak, ...] | None


class _SpectrumAnalysisWorker(QObject):
    completed = Signal(object)
    failed = Signal(int, str)

    @Slot(object)
    def analyze(self, request: object) -> None:
        if not isinstance(request, SpectrumAnalysisRequest):
            self.failed.emit(-1, "Invalid spectrum-analysis request.")
            return
        try:
            cleanup = clean_spectrum_dbm(
                request.powers_dbm,
                mode=request.mode,
                history_dbm=request.history_dbm,
            )
            peaks: tuple[SpectrumPeak, ...] | None = (
                detect_spectrum_peaks(
                    request.frequencies_hz,
                    cleanup.values_dbm,
                    min_snr_db=6.0,
                    min_prominence_db=3.0,
                    max_peaks=20,
                    fit=True,
                )
                if request.detect_peaks
                else None
            )
            self.completed.emit(
                SpectrumAnalysisOutcome(request.generation, cleanup, peaks)
            )
        except Exception as exc:
            self.failed.emit(request.generation, str(exc))


class SpectrumAnalysisController(QObject):
    """Run at most one analysis and retain only the newest pending frame."""

    _request = Signal(object)
    result = Signal(object)
    error = Signal(int, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread = QThread(self)
        self._thread.setObjectName("anritsu-spectrum-analysis")
        self._worker = _SpectrumAnalysisWorker()
        self._worker.moveToThread(self._thread)
        self._thread.finished.connect(self._worker.deleteLater)
        self._request.connect(
            self._worker.analyze, Qt.ConnectionType.QueuedConnection
        )
        self._worker.completed.connect(self._completed)
        self._worker.failed.connect(self._failed)
        self._busy = False
        self._pending: SpectrumAnalysisRequest | None = None
        self._thread.start()

    @property
    def busy(self) -> bool:
        return self._busy

    def submit(self, request: SpectrumAnalysisRequest) -> None:
        if self._busy:
            self._pending = request
            return
        self._busy = True
        self._request.emit(request)

    @Slot(object)
    def _completed(self, outcome: object) -> None:
        self._busy = False
        self.result.emit(outcome)
        self._start_pending()

    @Slot(int, str)
    def _failed(self, generation: int, message: str) -> None:
        self._busy = False
        self.error.emit(generation, message)
        self._start_pending()

    def _start_pending(self) -> None:
        pending, self._pending = self._pending, None
        if pending is not None:
            self.submit(pending)

    def close(self) -> None:
        self._pending = None
        if not self._thread.isRunning():
            return
        self._thread.quit()
        if not self._thread.wait(3_000):
            self._thread.requestInterruption()
            self._thread.wait()
