"""Preview loading for the native review page.

The web interface needed a scheduler, a priority queue and a polling endpoint
because the browser could only ask over HTTP. A Qt page can hold the pixmaps
itself, so this is a thread pool, a cache, and a signal.

Decoding stays where it already was: PhotoStore writes the same disk cache the
rest of the application uses, so a preview generated here costs nothing the
next time it is asked for.
"""

from __future__ import annotations

import os

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QPixmap

from opencull_gui.photos import PhotoError, PhotoStore


class _Signals(QObject):
    ready = Signal(str, str, QPixmap)   # name, size, pixmap
    failed = Signal(str, str, str)      # name, size, reason


class _Job(QRunnable):
    def __init__(self, store: PhotoStore, name: str, size: str,
                 signals: _Signals, generation: int, current):
        super().__init__()
        self.store = store
        self.name = name
        self.size = size
        self.signals = signals
        self.generation = generation
        self._current = current
        self.setAutoDelete(True)

    def run(self) -> None:
        # A cluster the reviewer has already moved past is not worth decoding.
        if self._current() != self.generation:
            return
        try:
            path = self.store.preview(self.name, self.size)
        except (PhotoError, OSError) as exc:
            self.signals.failed.emit(self.name, self.size, str(exc))
            return
        if self._current() != self.generation:
            return
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self.signals.failed.emit(
                self.name, self.size, "the generated preview could not be read")
            return
        self.signals.ready.emit(self.name, self.size, pixmap)


class PreviewLoader(QObject):
    """Decode previews off the interface thread, and remember them."""

    ready = Signal(str, str, QPixmap)
    failed = Signal(str, str, str)

    def __init__(self, store: PhotoStore, parent: QObject | None = None,
                 cache_size: int = 240):
        super().__init__(parent)
        self.store = store
        self.cache_size: int = cache_size
        self._cache: dict[tuple[str, str], QPixmap] = {}
        self._order: list[tuple[str, str]] = []
        self._generation = 0
        self._signals = _Signals()
        self._signals.ready.connect(self._remember)
        self._signals.failed.connect(self.failed)
        self._pool = QThreadPool(self)
        # Decoding is CPU-bound; leave the machine room to stay responsive.
        self._pool.setMaxThreadCount(max(2, min(6, (os.cpu_count() or 4) - 2)))

    def cached(self, name: str, size: str) -> QPixmap | None:
        return self._cache.get((name, size))

    def request(self, name: str, size: str = "thumb") -> QPixmap | None:
        """Return the preview if it is known, and otherwise fetch it."""
        hit = self._cache.get((name, size))
        if hit is not None:
            return hit
        self._pool.start(
            _Job(self.store, name, size, self._signals,
                 self._generation, lambda: self._generation))
        return None

    def abandon(self) -> None:
        """Stop caring about work in flight, without stopping the pool.

        Moving between clusters would otherwise queue every frame of every
        cluster passed through.
        """
        self._generation += 1

    def _remember(self, name: str, size: str, pixmap: QPixmap) -> None:
        key = (name, size)
        if key not in self._cache:
            self._order.append(key)
        self._cache[key] = pixmap
        while len(self._order) > self.cache_size:
            self._cache.pop(self._order.pop(0), None)
        self.ready.emit(name, size, pixmap)

    def shutdown(self) -> None:
        self._generation += 1
        self._pool.clear()
        self._pool.waitForDone(3000)


def scaled(pixmap: QPixmap, width: int, height: int) -> QPixmap:
    """Fit a preview into a box without distorting the photograph."""
    return pixmap.scaled(
        width, height,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation)
