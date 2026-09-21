from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Iterable

from pydantic import ValidationError

from cua.profiles.parabank import PAGE_STATES, PROFILE_ID, ROUTES
from cua.evidence.models import (
    EvidenceRef,
    RunAlias,
    RunMetadata,
    RunState,
    SafeControlSummary,
    SafeEvent,
    SafePageState,
    SafeProfile,
    SafeReasonCode,
    SafeRoute,
    SafeSnapshot,
)


_TERMINAL = {
    RunState.SUCCESS,
    RunState.BUSINESS_OUTCOME,
    RunState.FAILURE,
    RunState.ABORTED,
    RunState.SESSION_LOST,
}
_TRANSITIONS = {
    RunState.CREATED: {RunState.RUNNING, RunState.ABORTED},
    RunState.RUNNING: {
        RunState.WAITING_FOR_HUMAN,
        RunState.SUCCESS,
        RunState.BUSINESS_OUTCOME,
        RunState.FAILURE,
        RunState.ABORTED,
        RunState.SESSION_LOST,
    },
    RunState.WAITING_FOR_HUMAN: {
        RunState.RUNNING,
        RunState.FAILURE,
        RunState.ABORTED,
        RunState.SESSION_LOST,
    },
}
_SAFE_ROLES = {
    "link",
    "button",
    "textbox",
    "combobox",
    "checkbox",
    "radio",
    "tab",
    "heading",
    "generic",
}
_SAFE_OPERATIONS = {"CLICK", "TYPE_TEXT", "SELECT", "SCROLL"}
_KNOWN_ROUTES = set(ROUTES) | set(ROUTES.values())
_SAFE_PAGE_STATES = set(PAGE_STATES)
_RUN_ALIAS_PATTERN = re.compile(r"^run_[a-f0-9]{16}$", re.ASCII)


class EvidenceError(RuntimeError):
    """Redacted, expected evidence-store failure."""


class EvidenceSink:
    """Persist only typed safe metadata and structural, value-free snapshots."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._root, 0o700)
        self._db_path = self._root / "evidence.sqlite3"
        self._lock = RLock()
        self._initialize()

    def register_run(self, metadata: RunMetadata) -> RunAlias:
        try:
            metadata = RunMetadata.model_validate(metadata.model_dump(mode="python"))
        except (AttributeError, ValidationError) as error:
            raise EvidenceError("safe run metadata is invalid") from error

        run_alias = f"run_{secrets.token_hex(8)}"
        run_dir = self._run_dir(run_alias)
        run_dir.mkdir(mode=0o700)
        os.chmod(run_dir, 0o700)
        with self._lock, self._connection() as connection:
            connection.execute(
                """INSERT INTO runs (
                    run_alias, mode, capability_name, capability_version, bundle_digest,
                    profile_id, target_revision, browser_version, model_id, state,
                    outcome_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
                (
                    run_alias,
                    metadata.mode,
                    metadata.capability_name,
                    metadata.capability_version,
                    metadata.bundle_digest,
                    metadata.profile_id,
                    metadata.target_revision,
                    metadata.browser_version,
                    metadata.model_id,
                    RunState.CREATED,
                ),
            )
        return run_alias

    def get_run(self, run_alias: str) -> RunRecord:
        run_alias = _validate_run_alias(run_alias)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT state, outcome_code FROM runs WHERE run_alias = ?",
                (run_alias,),
            ).fetchone()
        if row is None:
            raise EvidenceError("run record not found")
        return RunRecord(run_alias=run_alias, state=RunState(row[0]), outcome_code=row[1])

    def transition_run(
        self,
        run_alias: str,
        state: RunState,
        *,
        outcome_code: SafeReasonCode | None = None,
    ) -> RunRecord:
        run_alias = _validate_run_alias(run_alias)
        try:
            state = RunState(state)
            if outcome_code is not None:
                outcome_code = SafeReasonCode(outcome_code)
        except ValueError as error:
            raise EvidenceError("run state or outcome code is invalid") from error

        with self._lock, self._connection() as connection:
            row = connection.execute(
                "SELECT state FROM runs WHERE run_alias = ?", (run_alias,)
            ).fetchone()
            if row is None:
                raise EvidenceError("run record not found")
            current = RunState(row[0])
            if state not in _TRANSITIONS.get(current, set()):
                raise ValueError("run state transition is not allowed")
            if state in _TERMINAL and outcome_code is None:
                raise ValueError("terminal run state requires a safe outcome code")
            if state not in _TERMINAL and outcome_code is not None:
                raise ValueError("outcome code is only valid for a terminal state")
            connection.execute(
                "UPDATE runs SET state = ?, outcome_code = ? WHERE run_alias = ?",
                (state, outcome_code, run_alias),
            )
        return self.get_run(run_alias)

    def emit(self, event: SafeEvent) -> None:
        try:
            event = SafeEvent.model_validate(event.model_dump(mode="python"))
        except (AttributeError, ValidationError) as error:
            raise EvidenceError("safe event is invalid") from error

        payload = event.model_dump_json(exclude_none=True, by_alias=False).encode("utf-8")
        event_path = self._run_dir(event.run_alias) / "events.jsonl"
        with self._lock, self._connection() as connection:
            if connection.execute(
                "SELECT 1 FROM runs WHERE run_alias = ?", (event.run_alias,)
            ).fetchone() is None:
                raise EvidenceError("run record not found")
            try:
                connection.execute(
                    """INSERT INTO events (
                        event_id, run_alias, event_type, step_id, state, reason_code,
                        effect_state, duration_ms, control_ref
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event.event_id,
                        event.run_alias,
                        event.event_type,
                        event.step_id,
                        event.state,
                        event.reason_code,
                        event.effect_state,
                        event.duration_ms,
                        event.control_ref,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise EvidenceError("duplicate event reference") from error
            descriptor = os.open(
                event_path,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                0o600,
            )
            try:
                os.write(descriptor, payload + b"\n")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def capture_safe(
        self,
        run_alias: str,
        view: object,
        *,
        observation: object | None = None,
    ) -> EvidenceRef:
        """Store page state and structure only; never access field/text values."""
        run_alias = _validate_run_alias(run_alias)
        with self._connection() as connection:
            if connection.execute(
                "SELECT 1 FROM runs WHERE run_alias = ?", (run_alias,)
            ).fetchone() is None:
                raise EvidenceError("run record not found")

        source = observation if observation is not None else view
        snapshot_id = f"snap_{secrets.token_hex(6)}"
        snapshot = _safe_snapshot(snapshot_id, view, source)
        relative_path = f"safe_snapshots/{snapshot_id}.json"
        snapshot_dir = self._run_dir(run_alias) / "safe_snapshots"
        snapshot_dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(snapshot_dir, 0o700)
        snapshot_path = self._run_dir(run_alias) / relative_path
        encoded = snapshot.model_dump_json().encode("utf-8")
        with self._lock:
            descriptor = os.open(
                snapshot_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            try:
                os.write(descriptor, encoded)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            with self._connection() as connection:
                connection.execute(
                    """INSERT INTO snapshots (
                        snapshot_id, run_alias, safe_route, page_state, control_count
                    ) VALUES (?, ?, ?, ?, ?)""",
                    (
                        snapshot_id,
                        run_alias,
                        snapshot.safe_route,
                        snapshot.page_state,
                        snapshot.control_count,
                    ),
                )
        return EvidenceRef(snapshot_id=snapshot_id, relative_path=relative_path)

    def finish_manifest(self, run_alias: str) -> Path:
        run_alias = _validate_run_alias(run_alias)
        with self._connection() as connection:
            row = connection.execute(
                """SELECT mode, capability_name, capability_version, bundle_digest,
                    profile_id, target_revision, browser_version, model_id, state,
                    outcome_code FROM runs WHERE run_alias = ?""",
                (run_alias,),
            ).fetchone()
            if row is None:
                raise EvidenceError("run record not found")
            event_count = connection.execute(
                "SELECT count(*) FROM events WHERE run_alias = ?", (run_alias,)
            ).fetchone()[0]
            snapshot_count = connection.execute(
                "SELECT count(*) FROM snapshots WHERE run_alias = ?", (run_alias,)
            ).fetchone()[0]
        manifest = {
            "run_alias": run_alias,
            "mode": row[0],
            "capability_name": row[1],
            "capability_version": row[2],
            "bundle_digest": row[3],
            "profile_id": row[4],
            "target_revision": row[5],
            "browser_version": row[6],
            "model_id": row[7],
            "state": row[8],
            "outcome_code": row[9],
            "event_count": event_count,
            "snapshot_count": snapshot_count,
        }
        path = self._run_dir(run_alias) / "manifest.json"
        encoded = json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        temporary = path.with_suffix(".tmp")
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_TRUNC | os.O_WRONLY,
            0o600,
        )
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        return path

    def _run_dir(self, run_alias: str) -> Path:
        return self._root / _validate_run_alias(run_alias)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=5.0)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_alias TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    capability_name TEXT,
                    capability_version TEXT,
                    bundle_digest TEXT,
                    profile_id TEXT NOT NULL,
                    target_revision TEXT NOT NULL,
                    browser_version TEXT NOT NULL,
                    model_id TEXT,
                    state TEXT NOT NULL,
                    outcome_code TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    run_alias TEXT NOT NULL REFERENCES runs(run_alias),
                    event_type TEXT NOT NULL,
                    step_id TEXT,
                    state TEXT,
                    reason_code TEXT,
                    effect_state TEXT,
                    duration_ms INTEGER,
                    control_ref TEXT
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    run_alias TEXT NOT NULL REFERENCES runs(run_alias),
                    safe_route TEXT NOT NULL,
                    page_state TEXT NOT NULL,
                    control_count INTEGER NOT NULL
                );
                """
            )
        os.chmod(self._db_path, 0o600)


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_alias: RunAlias
    state: RunState
    outcome_code: str | None


def _validate_run_alias(run_alias: str) -> RunAlias:
    if not isinstance(run_alias, str) or not _RUN_ALIAS_PATTERN.fullmatch(run_alias):
        raise EvidenceError("run alias is invalid")
    return run_alias


def _safe_snapshot(snapshot_id: str, view: object, source: object) -> SafeSnapshot:
    state = getattr(view, "page_state", None)
    if not isinstance(state, str):
        tags = getattr(source, "state_tags", ())
        if not isinstance(tags, (tuple, list)):
            tags = ()
        safe_tags = [tag for tag in tags if isinstance(tag, str) and tag in _SAFE_PAGE_STATES]
        state = safe_tags[-1] if len(safe_tags) == 1 else "UNKNOWN"
    if not isinstance(state, str) or state not in _SAFE_PAGE_STATES:
        state = "UNKNOWN"

    profile_id = getattr(view, "profile_id", None)
    if profile_id != PROFILE_ID:
        profile_id = "UNKNOWN"

    route = getattr(view, "safe_route", None)
    if not isinstance(route, str) or route not in _KNOWN_ROUTES:
        route = "UNKNOWN"
    elif route.startswith("/"):
        route = next((name for name, path in ROUTES.items() if path == route), "UNKNOWN")

    raw_controls = getattr(source, "controls", ())
    if not isinstance(raw_controls, (tuple, list)):
        raw_controls = ()
    visible_count = sum(
        getattr(control, "visible", False) is True for control in raw_controls
    )
    summaries: list[SafeControlSummary] = []
    for control in raw_controls[:64]:
        try:
            visible = getattr(control, "visible", False) is True
            enabled = getattr(control, "enabled", False) is True
            raw_role = getattr(control, "role", "other")
            role = (
                raw_role
                if isinstance(raw_role, str) and raw_role in _SAFE_ROLES
                else "other"
            )
            operations = tuple(
                operation
                for operation in getattr(control, "allowed_operations", ())
                if isinstance(operation, str) and operation in _SAFE_OPERATIONS
            )
            summaries.append(
                SafeControlSummary(
                    control_ref=getattr(control, "ref"),
                    role=role,
                    visible=visible,
                    enabled=enabled,
                    allowed_operations=operations,
                )
            )
        except (AttributeError, TypeError, ValidationError):
            continue

    return SafeSnapshot(
        snapshot_id=snapshot_id,
        profile_id=profile_id,
        safe_route=route,
        page_state=state,
        control_count=min(len(raw_controls), 10000),
        visible_control_count=min(visible_count, 10000),
        controls_truncated=len(raw_controls) > 64,
        controls=tuple(summaries),
    )
