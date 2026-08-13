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

from colour_profile import srgb_profile
from scan import open_preview, raw_decoder_status

from .project import project_directory


class PhotoError(ValueError):
    """A photograph cannot be resolved or decoded safely."""


class PhotoStore:
    SIZES = {"thumb": 520, "detail": 2400}

    STATS_TTL = 5.0

    def __init__(self, root: Path, cache: Path):
        self.root = root.expanduser().resolve()
        if not self.root.is_dir():
            raise PhotoError(f"photo directory does not exist: {self.root}")
        self.cache = cache.expanduser().resolve()
        self.cache.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._stats_guard = threading.Lock()
        self._stats: dict[str, int] | None = None
        self._stats_at = 0.0

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
            managed = project_directory(self.root)
            relocated = []
            for category in ("Rejected", "RAW Reserve"):
                category_root = (managed / category).resolve()
                path = (category_root / relative).resolve()
                if path == category_root or category_root in path.parents:
                    relocated.append(path)
            candidate = next(
                (path for path in relocated if path.is_file()), candidate)
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

    def preview_revision(self, name: str, size_name: str) -> str:
        """Return the immutable identity of a generated preview artifact."""
        if size_name not in self.SIZES:
            raise PhotoError(f"unsupported preview size: {size_name}")
        return self._cache_path(self.resolve(name), size_name).stem

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

    def _write_preview(
        self, image: Image.Image, destination: Path, size_name: str
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(
            f".{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            image.save(
                temporary,
                format="JPEG",
                quality=88 if size_name == "detail" else 80,
                optimize=True,
                icc_profile=srgb_profile(),
            )
            temporary.replace(destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        self._record_generated(destination)

    def generate_preview(self, name: str, size_name: str) -> Path:
        """Decode the source once and write every preview size from it.

        Decoding a RAW dominates preview cost, so deriving the smaller sizes
        from the largest downscale rather than re-decoding per size halves the
        work for the grid-then-detail sequence the reviewer actually performs.
        """
        if size_name not in self.SIZES:
            raise PhotoError(f"unsupported preview size: {size_name}")
        source = self.resolve(name)
        destination = self._cache_path(source, size_name)
        cached = self.cached_preview(name, size_name)
        if cached:
            return cached
        # Keyed by source, not by target: one decode now serves every size, so
        # concurrent requests for thumb and detail must not both decode.
        with self._locks_guard:
            lock = self._locks.setdefault(str(source), threading.Lock())
        with lock:
            cached = self.cached_preview(name, size_name)
            if cached:
                return cached
            try:
                decoded = open_preview(source)
                image = (ImageOps.exif_transpose(decoded) or decoded).convert("RGB")
            except Exception as exc:
                raise PhotoError(
                    f"could not create preview for {name}: {exc}") from exc
            # Largest first, so each smaller size downscales the previous
            # result in place instead of the full-resolution decode.
            for other_name, edge in sorted(
                self.SIZES.items(), key=lambda item: -item[1]
            ):
                image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
                target = self._cache_path(source, other_name)
                if other_name != size_name and target.is_file():
                    continue
                try:
                    self._write_preview(image, target, other_name)
                except Exception as exc:
                    if other_name == size_name:
                        raise PhotoError(
                            f"could not create preview for {name}: {exc}") from exc
                    # A size the caller did not ask for is opportunistic.
        return destination

    def _scan_cache_stats(self) -> dict[str, int]:
        files = list(self.cache.rglob("*.jpg")) if self.cache.exists() else []
        total = 0
        count = 0
        for path in files:
            try:
                total += path.stat().st_size
            except OSError:  # Removed between listing and stat.
                continue
            count += 1
        return {"files": count, "bytes": total}

    def _record_generated(self, destination: Path) -> None:
        """Fold one new preview into the cached totals.

        Keeps ``cache_stats`` off the filesystem in the common case; the
        periodic rescan corrects any drift from external deletions.
        """
        try:
            size = destination.stat().st_size
        except OSError:
            return
        with self._stats_guard:
            if self._stats is not None:
                self._stats = {
                    "files": self._stats["files"] + 1,
                    "bytes": self._stats["bytes"] + size,
                }

    def cache_stats(self) -> dict[str, int]:
        """Report generated-preview totals.

        This is read by the preview status payload, which the interface polls
        continuously, so a full directory walk per call cost O(library) on every
        tick. Totals are maintained incrementally and rescanned on a timer.
        """
        now = time.monotonic()
        with self._stats_guard:
            fresh = (
                self._stats is not None
                and now - self._stats_at < self.STATS_TTL
            )
            if fresh:
                return dict(self._stats or {})
        scanned = self._scan_cache_stats()
        with self._stats_guard:
            self._stats = scanned
            self._stats_at = now
            return dict(scanned)

    def clear_cache(self) -> dict[str, int]:
        before = self.cache_stats()
        with self._stats_guard:
            self._stats = {"files": 0, "bytes": 0}
            self._stats_at = time.monotonic()
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

    @staticmethod
    def default_workers() -> int:
        """Pick a worker count from the machine rather than a fixed 2.

        Two was chosen when every RAW preview was a full decode holding a
        full-resolution array. Reading the embedded preview costs far less
        memory, so leaving cores idle only slows the first screen.
        """
        return max(2, min(6, (os.cpu_count() or 4) - 2))

    def __init__(
        self,
        store: PhotoStore,
        workers: int | None = None,
        max_pending: int = 256,
        stall_timeout: float = 60.0,
    ):
        self.store = store
        if workers is None:
            workers = self.default_workers()
        self.workers = max(1, min(8, int(workers)))
        self.max_pending = max(16, int(max_pending))
        self.stall_timeout = max(0.01, float(stall_timeout))
        self._queue: queue.PriorityQueue[PreviewTask] = queue.PriorityQueue()
        self._lock = threading.RLock()
        self._states: dict[str, dict[str, Any]] = {}
        self._sequence = 0
        self._focus_epoch = 0
        self._worker_sequence = 0
        self._retired_workers: set[int] = set()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        for _ in range(self.workers):
            self._start_worker()

    def _start_worker(self) -> None:
        self._worker_sequence += 1
        worker_id = self._worker_sequence
        thread = threading.Thread(
            target=self._worker,
            args=(worker_id,),
            name=f"opencull-preview-{worker_id}",
            daemon=True,
        )
        self._threads.append(thread)
        thread.start()

    def _recover_stalled_locked(self) -> None:
        """Retire decoder workers that stopped making progress.

        Python cannot safely interrupt a native image decoder in another thread.
        Invalid RAW files and decoder bugs can therefore leave a call blocked.
        Retiring that worker lets the preview queue continue; its eventual result
        is ignored through the bumped task version.
        """
        now = time.time()
        stalled_workers: set[int] = set()
        for state in self._states.values():
            worker_id = state.get("worker_id")
            if (
                state.get("status") == "generating"
                and worker_id
                and now - float(state.get("updated_at", now))
                >= self.stall_timeout
            ):
                state.update({
                    "status": "failed",
                    "error": (
                        "preview decoding stalled; the decoder was restarted. "
                        "Choose Retry to try this photo again."
                    ),
                    "updated_at": now,
                    "version": int(state.get("version", 0)) + 1,
                    "worker_id": None,
                })
                stalled_workers.add(int(worker_id))
        for worker_id in stalled_workers - self._retired_workers:
            self._retired_workers.add(worker_id)
            self._start_worker()

    @staticmethod
    def key(name: str, size: str) -> str:
        return f"{size}:{name}"

    def _public(self, key: str) -> dict[str, Any]:
        state = self._states.get(key, {})
        public = {
            field: state.get(field)
            for field in ("name", "size", "status", "error", "updated_at")
        }
        if state.get("status") == "ready":
            try:
                public["revision"] = self.store.preview_revision(
                    str(state.get("name", "")), str(state.get("size", "")))
            except PhotoError:
                public["revision"] = None
        else:
            public["revision"] = None
        return public

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
            self._recover_stalled_locked()
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
            self._recover_stalled_locked()
            return {
                "previews": {
                    name: self._public(self.key(name, size))
                    for name in names
                },
                "progress": self.progress(),
            }

    def progress(self) -> dict[str, Any]:
        with self._lock:
            self._recover_stalled_locked()
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
                # A degraded RAW decoder is the difference between previews
                # that appear instantly and previews that take minutes, so the
                # interface reports it instead of merely feeling slow.
                "decoder": raw_decoder_status(),
            }

    def _worker(self, worker_id: int) -> None:
        while not self._stop.is_set():
            with self._lock:
                if worker_id in self._retired_workers:
                    return
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
                        "worker_id": worker_id,
                    })
                try:
                    self.store.generate_preview(task.name, task.size)
                except Exception as exc:
                    with self._lock:
                        current = self._states.get(task.key)
                        if current and current.get("version") == task.version:
                            current.update({
                                "status": "failed",
                                "error": (
                                    str(exc) or
                                    f"{type(exc).__name__} while decoding preview"
                                ),
                                "updated_at": time.time(),
                                "worker_id": None,
                            })
                else:
                    with self._lock:
                        current = self._states.get(task.key)
                        if current and current.get("version") == task.version:
                            current.update({
                                "status": "ready", "error": "",
                                "updated_at": time.time(),
                                "worker_id": None,
                            })
            finally:
                self._queue.task_done()
            with self._lock:
                if worker_id in self._retired_workers:
                    return

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
