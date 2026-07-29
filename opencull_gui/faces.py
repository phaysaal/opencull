"""Local-only face indexing, anonymous grouping, and private identity review."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import threading
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from scan import open_preview

from .photos import PhotoStore
from .report import ReportIndex
from .reviews import ReviewStore


FACE_DB_FORMAT = "opencull-private-faces-v1"
YUNET_NAME = "face_detection_yunet_2023mar.onnx"
SFACE_NAME = "face_recognition_sface_2021dec.onnx"
YUNET_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
SFACE_SHA256 = "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"
COSINE_THRESHOLD = 0.55


class FaceError(ValueError):
    """Face models, private state, or requested identity operation is invalid."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def default_face_db(report_path: Path) -> Path:
    return report_path.with_suffix(".faces.sqlite3")


class FaceEngine:
    """Pinned OpenCV YuNet detector and SFace embedding engine."""

    def __init__(self, model_root: Path):
        self.model_root = model_root.expanduser().resolve()
        self.detector_path = self.model_root / YUNET_NAME
        self.recognizer_path = self.model_root / SFACE_NAME
        self._verify(self.detector_path, YUNET_SHA256)
        self._verify(self.recognizer_path, SFACE_SHA256)
        try:
            import cv2
        except ImportError as exc:
            raise FaceError(
                "OpenCV is unavailable; install opencv-python-headless==4.11.0.86"
            ) from exc
        self.cv2 = cv2
        self.detector = cv2.FaceDetectorYN.create(
            str(self.detector_path), "", (320, 320), 0.75, 0.3, 5000)
        self.recognizer = cv2.FaceRecognizerSF.create(
            str(self.recognizer_path), "")
        self._lock = threading.Lock()

    @staticmethod
    def _verify(path: Path, expected: str) -> None:
        if not path.is_file():
            raise FaceError(f"required local face model is missing: {path}")
        actual = _sha256(path)
        if actual != expected:
            raise FaceError(
                f"face model hash mismatch for {path.name}: "
                f"expected {expected}, got {actual}")

    def analyze(self, path: Path) -> tuple[tuple[int, int], list[dict[str, Any]]]:
        image = open_preview(path).convert("RGB")
        original_width, original_height = image.size
        scale = min(1.0, 1920 / max(original_width, original_height))
        if scale < 1:
            image = image.resize(
                (round(original_width * scale), round(original_height * scale)),
                Image.Resampling.LANCZOS)
        rgb = np.asarray(image)
        bgr = self.cv2.cvtColor(rgb, self.cv2.COLOR_RGB2BGR)
        height, width = bgr.shape[:2]
        with self._lock:
            self.detector.setInputSize((width, height))
            _, detected = self.detector.detect(bgr)
            results = []
            for row in detected if detected is not None else []:
                aligned = self.recognizer.alignCrop(bgr, row)
                feature = self.recognizer.feature(aligned).flatten().astype(
                    np.float32)
                norm = float(np.linalg.norm(feature))
                if not math.isfinite(norm) or norm <= 0:
                    continue
                feature /= norm
                inverse = 1 / scale
                results.append({
                    "box": [
                        max(0, round(float(row[index]) * inverse))
                        for index in range(4)
                    ],
                    "landmarks": [
                        round(float(value) * inverse)
                        for value in row[4:14]
                    ],
                    "confidence": round(float(row[14]), 6),
                    "embedding": feature,
                })
        return (original_width, original_height), results


class FaceStore:
    """Report-bound private database and one bounded local indexing worker."""

    def __init__(
        self,
        path: Path,
        report: ReportIndex,
        photos: PhotoStore,
        reviews: ReviewStore,
        model_root: Path,
        engine: FaceEngine | Any | None = None,
    ):
        self.path = path.expanduser().resolve()
        self.report = report
        self.photos = photos
        self.reviews = reviews
        self.model_root = model_root.expanduser().resolve()
        self._engine = engine
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._status = {
            "state": "idle", "processed": 0,
            "total": len(report.photo_names), "faces": 0, "error": "",
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        os.chmod(self.path, 0o600)
        self._db.row_factory = sqlite3.Row
        self._schema()
        self._bind()

    def _schema(self) -> None:
        self._db.executescript("""
        PRAGMA foreign_keys = ON;
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS photos (
            name TEXT PRIMARY KEY,
            source_size INTEGER NOT NULL,
            source_mtime_ns INTEGER NOT NULL,
            width INTEGER NOT NULL,
            height INTEGER NOT NULL,
            indexed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS people (
            id TEXT PRIMARY KEY,
            private_name TEXT NOT NULL DEFAULT '',
            confirmed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS faces (
            id TEXT PRIMARY KEY,
            photo_name TEXT NOT NULL REFERENCES photos(name) ON DELETE CASCADE,
            x INTEGER NOT NULL, y INTEGER NOT NULL,
            width INTEGER NOT NULL, height INTEGER NOT NULL,
            confidence REAL NOT NULL,
            landmarks TEXT NOT NULL,
            embedding BLOB NOT NULL,
            person_id TEXT REFERENCES people(id) ON DELETE SET NULL,
            cluster_confidence REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS faces_photo ON faces(photo_name);
        CREATE INDEX IF NOT EXISTS faces_person ON faces(person_id);
        """)
        self._db.commit()

    def _bind(self) -> None:
        expected = {
            "format": FACE_DB_FORMAT,
            "report_sha256": self.report.sha256,
            "photos_root": str(self.photos.root),
            "yunet_sha256": YUNET_SHA256,
            "sface_sha256": SFACE_SHA256,
        }
        current = {
            row["key"]: row["value"]
            for row in self._db.execute("SELECT key, value FROM metadata")
        }
        if current:
            mismatch = [
                key for key, value in expected.items()
                if current.get(key) != value
            ]
            if mismatch:
                raise FaceError(
                    "private face database belongs to different evidence: "
                    + ", ".join(mismatch))
        else:
            self._db.executemany(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                expected.items())
            self._db.commit()

    def _get_engine(self) -> Any:
        if self._engine is None:
            self._engine = FaceEngine(self.model_root)
        return self._engine

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise FaceError("face indexing is already running")
            self._cancel.clear()
            self._status.update(state="running", error="")
            self._thread = threading.Thread(
                target=self._run, name="opencull-local-faces", daemon=True)
            self._thread.start()
            return self.public()

    def cancel(self) -> dict[str, Any]:
        self._cancel.set()
        return self.public()

    def _run(self) -> None:
        try:
            engine = self._get_engine()
            names = list(self.report.photo_names)
            for position, name in enumerate(names, start=1):
                if self._cancel.is_set():
                    with self._lock:
                        self._status["state"] = "paused"
                    return
                source = self.photos.resolve(name)
                stat = source.stat()
                with self._lock:
                    prior = self._db.execute(
                        "SELECT source_size, source_mtime_ns FROM photos "
                        "WHERE name = ?", (name,)).fetchone()
                if (
                    prior and prior["source_size"] == stat.st_size
                    and prior["source_mtime_ns"] == stat.st_mtime_ns
                ):
                    with self._lock:
                        self._status["processed"] = position
                    continue
                dimensions, faces = engine.analyze(source)
                with self._lock:
                    self._db.execute("DELETE FROM photos WHERE name = ?", (name,))
                    self._db.execute(
                        "INSERT INTO photos VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            name, stat.st_size, stat.st_mtime_ns,
                            dimensions[0], dimensions[1], _now(),
                        ))
                    for face in faces:
                        self._db.execute(
                            "INSERT INTO faces VALUES "
                            "(?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, ?)",
                            (
                                uuid.uuid4().hex,
                                name,
                                *face["box"],
                                face["confidence"],
                                json.dumps(face["landmarks"]),
                                face["embedding"].tobytes(),
                                _now(),
                            ))
                    self._db.commit()
                    self._status.update(
                        processed=position,
                        faces=self._db.execute(
                            "SELECT COUNT(*) FROM faces").fetchone()[0],
                    )
            with self._lock:
                self._cluster_unassigned()
                self._status["state"] = "completed"
        except Exception as exc:
            with self._lock:
                self._status.update(state="failed", error=str(exc))

    @staticmethod
    def _similarity(left: np.ndarray, right: np.ndarray) -> float:
        return float(np.dot(left, right))

    def _cluster_unassigned(self) -> None:
        self._db.execute(
            "DELETE FROM people WHERE NOT EXISTS "
            "(SELECT 1 FROM faces WHERE faces.person_id = people.id)")
        rows = self._db.execute(
            "SELECT id, photo_name, embedding FROM faces WHERE person_id IS NULL "
            "ORDER BY photo_name, id").fetchall()
        clusters: list[dict[str, Any]] = []
        for row in rows:
            embedding = np.frombuffer(row["embedding"], dtype=np.float32)
            best_index, best_score = None, -1.0
            for index, cluster in enumerate(clusters):
                if row["photo_name"] in cluster["photos"]:
                    continue
                score = self._similarity(embedding, cluster["centroid"])
                minimum = min(
                    self._similarity(embedding, member)
                    for member in cluster["vectors"])
                if score >= COSINE_THRESHOLD and minimum >= 0.42 \
                        and score > best_score:
                    best_index, best_score = index, score
            if best_index is not None:
                cluster = clusters[best_index]
                cluster["members"].append((row["id"], best_score))
                cluster["photos"].add(row["photo_name"])
                values = cluster["vectors"] + [embedding]
                centroid = np.mean(values, axis=0)
                centroid /= max(float(np.linalg.norm(centroid)), 1e-12)
                cluster.update(vectors=values, centroid=centroid)
            else:
                clusters.append({
                    "members": [(row["id"], 1.0)],
                    "photos": {row["photo_name"]},
                    "vectors": [embedding],
                    "centroid": embedding.copy(),
                })
        for cluster in clusters:
            person_id = "person-" + uuid.uuid4().hex[:12]
            timestamp = _now()
            self._db.execute(
                "INSERT INTO people VALUES (?, '', 0, ?, ?)",
                (person_id, timestamp, timestamp))
            self._db.executemany(
                "UPDATE faces SET person_id = ?, cluster_confidence = ? "
                "WHERE id = ?",
                [
                    (person_id, round(score, 6), face_id)
                    for face_id, score in cluster["members"]
                ])
        self._db.commit()

    @staticmethod
    def _coverage(photos: list[str], selected: set[str]) -> dict[str, Any]:
        unique = set(photos)
        return {
            "photos": len(unique),
            "selected_photos": len(unique & selected),
            "unselected_photos": len(unique - selected),
        }

    def public(self) -> dict[str, Any]:
        with self._lock:
            people = []
            photo_people: dict[str, list[str]] = {}
            selected = set(self.reviews.export()["selected_photos"])
            person_photos: dict[str, list[str]] = {}
            for relation in self._db.execute(
                "SELECT person_id, photo_name FROM faces "
                "WHERE person_id IS NOT NULL ORDER BY photo_name"
            ):
                photo_people.setdefault(relation["photo_name"], []).append(
                    relation["person_id"])
                person_photos.setdefault(relation["person_id"], []).append(
                    relation["photo_name"])
            rows = self._db.execute("""
                SELECT p.id, p.private_name, p.confirmed,
                       COUNT(f.id) AS face_count,
                       COUNT(DISTINCT f.photo_name) AS photo_count
                FROM people p LEFT JOIN faces f ON f.person_id = p.id
                GROUP BY p.id ORDER BY face_count DESC, p.id
            """).fetchall()
            for row in rows:
                face_rows = self._db.execute(
                    "SELECT id, photo_name, x, y, width, height, confidence, "
                    "cluster_confidence FROM faces WHERE person_id = ? "
                    "ORDER BY confidence DESC, photo_name LIMIT 60",
                    (row["id"],)).fetchall()
                faces = []
                for face in face_rows:
                    faces.append({
                        "id": face["id"],
                        "photo_name": face["photo_name"],
                        "box": [
                            face["x"], face["y"],
                            face["width"], face["height"],
                        ],
                        "detection_confidence": face["confidence"],
                        "cluster_confidence": face["cluster_confidence"],
                    })
                people.append({
                    "id": row["id"],
                    "private_name": row["private_name"],
                    "display_name": row["private_name"] or row["id"],
                    "confirmed": bool(row["confirmed"]),
                    "face_count": row["face_count"],
                    "photo_count": row["photo_count"],
                    "faces": faces,
                    "faces_returned": len(faces),
                    "faces_truncated": row["face_count"] > len(faces),
                    "coverage": self._coverage(
                        person_photos.get(row["id"], []), selected),
                })
            processed = self._db.execute(
                "SELECT COUNT(*) FROM photos").fetchone()[0]
            status = deepcopy(self._status)
            status.update(
                processed=processed,
                total=len(self.report.photo_names),
                faces=self._db.execute(
                    "SELECT COUNT(*) FROM faces").fetchone()[0],
                people=len(people),
            )
            return {
                "format": FACE_DB_FORMAT,
                "database": str(self.path),
                "local_only": True,
                "embeddings_exposed": False,
                "model_hashes": {
                    "yunet": YUNET_SHA256, "sface": SFACE_SHA256},
                "status": status,
                "people": people,
                "photo_people": photo_people,
            }

    def person_faces(
        self, person_id: str, offset: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        offset = max(0, int(offset))
        limit = max(1, min(200, int(limit)))
        with self._lock:
            exists = self._db.execute(
                "SELECT 1 FROM people WHERE id = ?", (person_id,)).fetchone()
            if not exists:
                raise FaceError("unknown person")
            total = self._db.execute(
                "SELECT COUNT(*) FROM faces WHERE person_id = ?",
                (person_id,)).fetchone()[0]
            rows = self._db.execute(
                "SELECT id, photo_name, x, y, width, height, confidence, "
                "cluster_confidence FROM faces WHERE person_id = ? "
                "ORDER BY confidence DESC, photo_name LIMIT ? OFFSET ?",
                (person_id, limit, offset)).fetchall()
            return {
                "person_id": person_id,
                "offset": offset,
                "limit": limit,
                "total": total,
                "faces": [{
                    "id": face["id"],
                    "photo_name": face["photo_name"],
                    "box": [
                        face["x"], face["y"], face["width"], face["height"]],
                    "detection_confidence": face["confidence"],
                    "cluster_confidence": face["cluster_confidence"],
                } for face in rows],
            }

    def face_crop(self, face_id: str, maximum: int = 420) -> Image.Image:
        with self._lock:
            row = self._db.execute(
                "SELECT photo_name, x, y, width, height FROM faces WHERE id = ?",
                (face_id,)).fetchone()
        if row is None:
            raise FaceError("unknown face")
        image = open_preview(self.photos.resolve(row["photo_name"])).convert("RGB")
        margin = 0.35
        x, y, width, height = (
            row["x"], row["y"], row["width"], row["height"])
        left = max(0, round(x - width * margin))
        top = max(0, round(y - height * margin))
        right = min(image.width, round(x + width * (1 + margin)))
        bottom = min(image.height, round(y + height * (1 + margin)))
        crop = image.crop((left, top, right, bottom))
        crop.thumbnail((maximum, maximum), Image.Resampling.LANCZOS)
        return crop

    def rename(
        self, person_id: str, name: str, confirmed: bool
    ) -> dict[str, Any]:
        name = str(name).strip()
        if len(name) > 200:
            raise FaceError("private name is too long")
        with self._lock:
            cursor = self._db.execute(
                "UPDATE people SET private_name = ?, confirmed = ?, "
                "updated_at = ? WHERE id = ?",
                (name, int(bool(confirmed)), _now(), person_id))
            if cursor.rowcount != 1:
                raise FaceError("unknown person")
            self._db.commit()
            return self.public()

    def merge(self, person_ids: Any) -> dict[str, Any]:
        if (
            not isinstance(person_ids, list) or len(set(person_ids)) < 2
            or any(not isinstance(value, str) for value in person_ids)
        ):
            raise FaceError("merge requires at least two distinct people")
        with self._lock:
            rows = self._db.execute(
                f"SELECT id, private_name, confirmed FROM people WHERE id IN "
                f"({','.join('?' for _ in person_ids)})", person_ids).fetchall()
            if len(rows) != len(set(person_ids)):
                raise FaceError("merge names an unknown person")
            target = rows[0]["id"]
            names = {row["private_name"] for row in rows if row["private_name"]}
            if len(names) > 1:
                raise FaceError("named people conflict; clear or reconcile names first")
            self._db.executemany(
                "UPDATE faces SET person_id = ? WHERE person_id = ?",
                [(target, row["id"]) for row in rows[1:]])
            self._db.executemany(
                "DELETE FROM people WHERE id = ?",
                [(row["id"],) for row in rows[1:]])
            self._db.execute(
                "UPDATE people SET private_name = ?, confirmed = ?, updated_at = ? "
                "WHERE id = ?",
                (
                    next(iter(names), ""),
                    int(any(row["confirmed"] for row in rows)),
                    _now(), target,
                ))
            self._db.commit()
            return self.public()

    def split(self, person_id: str, face_ids: Any) -> dict[str, Any]:
        if not isinstance(face_ids, list) or not face_ids:
            raise FaceError("split requires one or more faces")
        with self._lock:
            count = self._db.execute(
                f"SELECT COUNT(*) FROM faces WHERE person_id = ? AND id IN "
                f"({','.join('?' for _ in face_ids)})",
                [person_id, *face_ids]).fetchone()[0]
            total = self._db.execute(
                "SELECT COUNT(*) FROM faces WHERE person_id = ?",
                (person_id,)).fetchone()[0]
            if count != len(set(face_ids)) or count >= total:
                raise FaceError(
                    "split faces must be a non-empty proper subset of one person")
            new_id = "person-" + uuid.uuid4().hex[:12]
            timestamp = _now()
            self._db.execute(
                "INSERT INTO people VALUES (?, '', 0, ?, ?)",
                (new_id, timestamp, timestamp))
            self._db.execute(
                f"UPDATE faces SET person_id = ?, cluster_confidence = 1 "
                f"WHERE id IN ({','.join('?' for _ in face_ids)})",
                [new_id, *face_ids])
            self._db.commit()
            return self.public()

    def forget(self, person_id: str) -> dict[str, Any]:
        """Delete this person's detections and embeddings, not just their name."""
        with self._lock:
            exists = self._db.execute(
                "SELECT 1 FROM people WHERE id = ?", (person_id,)).fetchone()
            if not exists:
                raise FaceError("unknown person")
            self._db.execute("DELETE FROM faces WHERE person_id = ?", (person_id,))
            self._db.execute("DELETE FROM people WHERE id = ?", (person_id,))
            self._db.commit()
            self._db.execute("VACUUM")
            return self.public()

    def delete_all(self) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise FaceError("pause face indexing before deleting private data")
            self._db.execute("DELETE FROM faces")
            self._db.execute("DELETE FROM people")
            self._db.execute("DELETE FROM photos")
            self._db.commit()
            self._db.execute("VACUUM")
            self._status.update(
                state="idle", processed=0, faces=0, error="")
            return self.public()

    def shutdown(self) -> None:
        self._cancel.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=3)
        with self._lock:
            self._db.close()
