"""Safe photo resolution and lazy preview caching."""

from __future__ import annotations

import hashlib
import os
import queue
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from scan import open_preview


class PhotoError(ValueError):
    """A photograph cannot be resolved or decoded safely."""


class PhotoStore:
    SIZES = {"thumb": 520, "detail": 2400}

    def __init__(self, root: Path, cache: Path):
        self.root = root.expanduser().resolve()
        if not self.root.is_dir():
            raise PhotoError(f"photo directory does not exist: {self.root}")
        self.cache = cache.expanduser().resolve()
        self.cache.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def resolve(self, name: str) -> Path:
        if not self.root.is_dir():
            raise PhotoError(
                f"photo directory is unavailable; reconnect the drive: {self.root}")
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise PhotoError(f"unsafe photo path: {name!r}")
        candidate = (self.root / relative).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise PhotoError(f"photo escapes source directory: {name!r}")
        if not candidate.is_file():
            raise PhotoError(f"photo is missing: {name}")
        return candidate

    def _cache_path(self, source: Path, size_name: str) -> Path:
        stat = source.stat()
        identity = "\0".join([
            os.fspath(source),
            str(stat.st_size),
            str(stat.st_mtime_ns),
            size_name,
            str(self.SIZES[size_name]),
        ])
        digest = hashlib.sha256(identity.encode()).hexdigest()
        return self.cache / size_name / f"{digest}.jpg"

    def preview(self, name: str, size_name: str) -> Path:
        """Synchronous compatibility wrapper; GUI requests use PreviewManager."""
        cached = self.cached_preview(name, size_name)
        return cached or self.generate_preview(name, size_name)

    def cached_preview(self, name: str, size_name: str) -> Path | None:
        if size_name not in self.SIZES:
            raise PhotoError(f"unsupported preview size: {size_name}")
        source = self.resolve(name)
        destination = self._cache_path(source, size_name)
        if not destination.is_file():
            return None
        try:
            with Image.open(destination) as image:
                image.verify()
        except Exception:
            destination.unlink(missing_ok=True)
            return None
        return destination

    def generate_preview(self, name: str, size_name: str) -> Path:
        if size_name not in self.SIZES:
            raise PhotoError(f"unsupported preview size: {size_name}")
        source = self.resolve(name)
        destination = self._cache_path(source, size_name)
        cached = self.cached_preview(name, size_name)
        if cached:
            return cached
        with self._locks_guard:
            lock = self._locks.setdefault(str(destination), threading.Lock())
        with lock:
            cached = self.cached_preview(name, size_name)
            if cached:
                return cached
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(".tmp")
            try:
                image = open_preview(source)
                image = ImageOps.exif_transpose(image).convert("RGB")
                image.thumbnail(
                    (self.SIZES[size_name], self.SIZES[size_name]),
                    Image.Resampling.LANCZOS,
                )
                image.save(
                    temporary,
                    format="JPEG",
                    quality=88 if size_name == "detail" else 80,
                    optimize=True,
                )
                temporary.replace(destination)
            except Exception as exc:
                temporary.unlink(missing_ok=True)
                raise PhotoError(
                    f"could not create preview for {name}: {exc}") from exc
        return destination

    def cache_stats(self) -> dict[str, int]:
        files = list(self.cache.rglob("*.jpg")) if self.cache.exists() else []
        return {
            "files": len(files),
            "bytes": sum(path.stat().st_size for path in files if path.is_file()),
        }

    def clear_cache(self) -> dict[str, int]:
        before = self.cache_stats()
        if self.cache.exists():
            for child in self.cache.iterdir():
                if child.is_dir():
                    shutil.rmtree(child)
                elif child.is_file():
                    child.unlink()
        return before


@dataclass(order=True)
class PreviewTask:
    priority: int
    sequence: int
    key: str
    name: str
    size: str
    version: int
    focus_epoch: int
    prefetch: bool


class PreviewManager:
    """Bounded priority scheduler for lazy preview generation."""

    def __init__(
        self,
        store: PhotoStore,
        workers: int = 2,
        max_pending: int = 256,
    ):
        self.store = store
        self.workers = max(1, min(8, int(workers)))
        self.max_pending = max(16, int(max_pending))
        self._queue: queue.PriorityQueue[PreviewTask] = queue.PriorityQueue()
        self._lock = threading.RLock()
        self._states: dict[str, dict[str, Any]] = {}
        self._sequence = 0
        self._focus_epoch = 0
        self._stop = threading.Event()
        self._threads = [
            threading.Thread(
                target=self._worker,
                name=f"opencull-preview-{number + 1}",
                daemon=True,
            )
            for number in range(self.workers)
        ]
        for thread in self._threads:
            thread.start()

    @staticmethod
    def key(name: str, size: str) -> str:
        return f"{size}:{name}"

    def _public(self, key: str) -> dict[str, Any]:
        state = self._states.get(key, {})
        return {
            field: state.get(field)
            for field in ("name", "size", "status", "error", "updated_at")
        }

    def _enqueue(
        self,
        name: str,
        size: str,
        priority: int,
        prefetch: bool,
        force: bool = False,
    ) -> dict[str, Any]:
        key = self.key(name, size)
        try:
            cached = self.store.cached_preview(name, size)
        except PhotoError as exc:
            cached = None
            initial_error = str(exc)
        else:
            initial_error = ""
        with self._lock:
            if cached:
                self._states[key] = {
                    "name": name, "size": size, "status": "ready",
                    "error": "", "updated_at": time.time(), "version": 0,
                }
                return self._public(key)
            current = self._states.get(key)
            if (
                current
                and current.get("status") in {"queued", "generating", "ready"}
                and not force
                and not (
                    current.get("status") == "queued"
                    and current.get("prefetch")
                    and not prefetch
                )
            ):
                return self._public(key)
            pending = sum(
                item.get("status") in {"queued", "generating"}
                for item in self._states.values()
            )
            if prefetch and pending >= self.max_pending:
                self._states[key] = {
                    "name": name, "size": size, "status": "deferred",
                    "error": "preview queue is full", "updated_at": time.time(),
                    "version": (current or {}).get("version", 0),
                }
                return self._public(key)
            version = int((current or {}).get("version", 0)) + 1
            self._sequence += 1
            self._states[key] = {
                "name": name, "size": size, "status": "queued",
                "error": initial_error, "updated_at": time.time(),
                "version": version, "prefetch": prefetch,
                "focus_epoch": self._focus_epoch,
            }
            self._queue.put(PreviewTask(
                priority=priority,
                sequence=self._sequence,
                key=key,
                name=name,
                size=size,
                version=version,
                focus_epoch=self._focus_epoch,
                prefetch=prefetch,
            ))
            return self._public(key)

    def focus(
        self,
        visible: list[str],
        prefetch: list[str],
        size: str = "thumb",
    ) -> dict[str, Any]:
        if size not in self.store.SIZES:
            raise PhotoError(f"unsupported preview size: {size}")
        with self._lock:
            self._focus_epoch += 1
        results = {}
        for name in visible:
            results[name] = self._enqueue(name, size, 0, False)
        for name in prefetch:
            if name not in results:
                results[name] = self._enqueue(name, size, 20, True)
        return {"previews": results, "progress": self.progress()}

    def request(
        self, name: str, size: str, priority: int = 0
    ) -> dict[str, Any]:
        return self._enqueue(name, size, priority, False)

    def retry(self, name: str, size: str) -> dict[str, Any]:
        return self._enqueue(name, size, 0, False, force=True)

    def status(self, names: list[str], size: str) -> dict[str, Any]:
        with self._lock:
            return {
                "previews": {
                    name: self._public(self.key(name, size))
                    for name in names
                },
                "progress": self.progress(),
            }

    def progress(self) -> dict[str, Any]:
        with self._lock:
            counts = {
                status: sum(
                    item.get("status") == status
                    for item in self._states.values()
                )
                for status in (
                    "queued", "generating", "ready", "failed",
                    "deferred", "cancelled",
                )
            }
            return {
                **counts,
                "workers": self.workers,
                "max_pending": self.max_pending,
                "cache": self.store.cache_stats(),
            }

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                task = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                with self._lock:
                    current = self._states.get(task.key)
                    if not current or current.get("version") != task.version:
                        continue
                    if task.prefetch and task.focus_epoch != self._focus_epoch:
                        current.update({
                            "status": "cancelled",
                            "error": "stale prefetch",
                            "updated_at": time.time(),
                        })
                        continue
                    current.update({
                        "status": "generating", "error": "",
                        "updated_at": time.time(),
                    })
                try:
                    self.store.generate_preview(task.name, task.size)
                except PhotoError as exc:
                    with self._lock:
                        current = self._states.get(task.key)
                        if current and current.get("version") == task.version:
                            current.update({
                                "status": "failed",
                                "error": str(exc),
                                "updated_at": time.time(),
                            })
                else:
                    with self._lock:
                        current = self._states.get(task.key)
                        if current and current.get("version") == task.version:
                            current.update({
                                "status": "ready", "error": "",
                                "updated_at": time.time(),
                            })
            finally:
                self._queue.task_done()

    def clear_cache(self) -> dict[str, Any]:
        with self._lock:
            if any(
                state.get("status") == "generating"
                for state in self._states.values()
            ):
                raise PhotoError(
                    "wait for active preview generation before clearing cache")
            removed = self.store.clear_cache()
            self._states.clear()
            return {"removed": removed, "progress": self.progress()}

    def shutdown(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2)
