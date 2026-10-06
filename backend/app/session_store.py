from __future__ import annotations

import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyreadstat

from .filter_parser import evaluate_filter
from .models import DerivedKind, DerivedVariableSpec


@dataclass
class DatasetSession:
    id: str
    filename: str
    path: Path
    frame: pd.DataFrame
    metadata: Any
    size_bytes: int
    derived: dict[str, DerivedVariableSpec] = field(default_factory=dict)
    filter_expression: str = ""
    applied_mask: pd.Series | None = None
    revision: int = 0
    created_at: float = field(default_factory=time.monotonic)
    last_accessed: float = field(default_factory=time.monotonic)
    estimated_memory_bytes: int = 0

    def touch(self, now: float | None = None) -> None:
        self.last_accessed = time.monotonic() if now is None else now

    def refresh_size(self) -> None:
        frame_bytes = int(self.frame.memory_usage(index=True, deep=True).sum())
        mask_bytes = (
            int(self.applied_mask.memory_usage(index=True, deep=True))
            if self.applied_mask is not None
            else 0
        )
        self.estimated_memory_bytes = frame_bytes + mask_bytes

    @property
    def filtered_frame(self) -> pd.DataFrame:
        if self.applied_mask is None:
            return self.frame
        return self.frame.loc[self.applied_mask]

    def variable_payload(self) -> list[dict[str, Any]]:
        labels = getattr(self.metadata, "column_names_to_labels", {}) or {}
        result: list[dict[str, Any]] = []
        for name in self.frame.columns:
            series = self.frame[name]
            examples = [json_safe(value) for value in series.dropna().head(3).tolist()]
            result.append(
                {
                    "name": name,
                    "label": labels.get(name) or "",
                    "dtype": str(series.dtype),
                    "numeric": bool(pd.api.types.is_numeric_dtype(series)),
                    "non_missing": int(series.notna().sum()),
                    "missing": int(series.isna().sum()),
                    "examples": examples,
                    "derived": name in self.derived,
                }
            )
        return result

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "rows": int(len(self.frame)),
            "filtered_rows": int(len(self.filtered_frame)),
            "columns": int(len(self.frame.columns)),
            "encoding": getattr(self.metadata, "file_encoding", None) or "",
            "file_format": getattr(self.metadata, "file_format", None) or "Stata .dta",
            "variables": self.variable_payload(),
            "derived": [spec.model_dump() for spec in self.derived.values()],
            "filter_expression": self.filter_expression,
            "filter_applied": self.applied_mask is not None,
            "revision": self.revision,
        }


def json_safe(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (pd.Timestamp, pd.Timedelta)):
        return str(value)
    return value


class SessionStore:
    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="control-combination-"))
        self._sessions: dict[str, DatasetSession] = {}
        self._lock = threading.RLock()

    def create(self, filename: str, content: bytes) -> DatasetSession:
        self.root.mkdir(parents=True, exist_ok=True)
        dataset_id = uuid.uuid4().hex
        safe_name = Path(filename).name
        path = self.root / f"{dataset_id}-{safe_name}"
        path.write_bytes(content)
        frame, metadata = pyreadstat.read_dta(
            str(path), apply_value_formats=False, user_missing=False
        )
        session = DatasetSession(dataset_id, safe_name, path, frame, metadata, len(content))
        session.refresh_size()
        with self._lock:
            self._sessions[dataset_id] = session
        return session

    def get(self, dataset_id: str) -> DatasetSession:
        with self._lock:
            if dataset_id not in self._sessions:
                raise KeyError(dataset_id)
            session = self._sessions[dataset_id]
            session.touch()
            return session

    def peek(self, dataset_id: str) -> DatasetSession:
        with self._lock:
            if dataset_id not in self._sessions:
                raise KeyError(dataset_id)
            return self._sessions[dataset_id]

    def remove(self, dataset_id: str) -> DatasetSession:
        with self._lock:
            if dataset_id not in self._sessions:
                raise KeyError(dataset_id)
            session = self._sessions.pop(dataset_id)
        try:
            session.path.unlink(missing_ok=True)
        except OSError:
            pass
        return session

    def items(self) -> list[tuple[str, DatasetSession]]:
        with self._lock:
            return list(self._sessions.items())

    def add_derived(self, dataset_id: str, spec: DerivedVariableSpec) -> dict[str, Any]:
        session = self.get(dataset_id)
        if spec.name in session.frame.columns:
            raise ValueError(f"Variable already exists: {spec.name}")
        if spec.source not in session.frame.columns:
            raise ValueError(f"Unknown source variable: {spec.source}")
        source = session.frame[spec.source]
        if not pd.api.types.is_numeric_dtype(source):
            raise ValueError("Derived variables require numeric sources")
        if spec.kind == DerivedKind.log:
            valid = source > 0
            values = pd.Series(np.nan, index=source.index, dtype=float)
            values.loc[valid] = np.log(source.loc[valid].astype(float))
        else:
            assert spec.denominator is not None
            if spec.denominator not in session.frame.columns:
                raise ValueError(f"Unknown denominator: {spec.denominator}")
            denominator = session.frame[spec.denominator]
            if not pd.api.types.is_numeric_dtype(denominator):
                raise ValueError("Derived variables require numeric sources")
            valid = source.notna() & denominator.notna() & (denominator != 0)
            values = pd.Series(np.nan, index=source.index, dtype=float)
            values.loc[valid] = source.loc[valid].astype(float) / denominator.loc[valid].astype(
                float
            )
        session.frame[spec.name] = values
        session.derived[spec.name] = spec
        session.revision += 1
        if session.applied_mask is not None:
            session.applied_mask = session.applied_mask.reindex(
                session.frame.index, fill_value=False
            )
        session.refresh_size()
        return {
            "name": spec.name,
            "valid": int(values.notna().sum()),
            "missing": int(values.isna().sum()),
            "invalid": int((source.notna() & values.isna()).sum()),
        }

    def remove_derived(self, dataset_id: str, name: str) -> None:
        session = self.get(dataset_id)
        if name not in session.derived:
            raise ValueError("Only derived variables can be removed")
        session.frame.drop(columns=[name], inplace=True)
        del session.derived[name]
        session.filter_expression = ""
        session.applied_mask = None
        session.revision += 1
        session.refresh_size()

    def validate_filter(self, dataset_id: str, expression: str) -> dict[str, Any]:
        session = self.get(dataset_id)
        mask = evaluate_filter(expression, session.frame)
        before = len(session.frame)
        after = int(mask.sum())
        return {
            "valid": True,
            "before": before,
            "after": after,
            "removed": before - after,
            "removed_ratio": (before - after) / before if before else 0,
        }

    def apply_filter(self, dataset_id: str, expression: str) -> dict[str, Any]:
        session = self.get(dataset_id)
        mask = evaluate_filter(expression, session.frame)
        session.filter_expression = expression.strip()
        session.applied_mask = mask
        session.revision += 1
        session.refresh_size()
        return self.validate_filter(dataset_id, expression)

    def cleanup(self) -> None:
        with self._lock:
            self._sessions.clear()
        shutil.rmtree(self.root, ignore_errors=True)
