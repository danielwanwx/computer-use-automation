#!/usr/bin/env python3
"""Export persisted, typed EvidenceSink records without widening their schema.

The native lifecycle keeps its evidence roots temporary.  This helper is used
before that root is removed to copy only the already-redacted database records,
events, and structural snapshots into a canonical release-safe JSON package.
It refuses missing roots, empty event logs, malformed records, digest
mismatches, and private-value matches supplied by the caller.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from scripts.native_lifecycle_common import contains_private_value
from cua.evidence.models import RunMetadata, RunMode, RunState, SafeEvent, SafeReasonCode, SafeSnapshot


_SOURCE_KINDS = {"native_target", "injected_test_harness"}


class EvidenceExportError(RuntimeError):
    """The source is not a complete, typed, value-safe evidence run."""


def _canonical(value: Mapping[str, Any] | list[Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_events(path: Path, run_alias: str) -> list[dict[str, Any]]:
    if not path.is_file() or path.is_symlink():
        raise EvidenceExportError(f"event log is missing for {run_alias}")
    events: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_bytes().splitlines(), 1):
        if not raw.strip():
            raise EvidenceExportError(f"event log has a blank line at {line_number}")
        try:
            event = SafeEvent.model_validate_json(raw)
        except ValueError as error:
            raise EvidenceExportError(f"event log is not a SafeEvent at line {line_number}") from error
        if event.run_alias != run_alias:
            raise EvidenceExportError("event run alias does not match its source run")
        events.append(event.model_dump(mode="json", exclude_none=True))
    if not events:
        raise EvidenceExportError(f"event log is empty for {run_alias}")
    return events


def _load_snapshots(run_dir: Path, run_alias: str) -> list[dict[str, Any]]:
    directory = run_dir / "safe_snapshots"
    if not directory.exists():
        return []
    result: list[dict[str, Any]] = []
    for path in sorted(directory.glob("snap_*.json")):
        if path.is_symlink():
            raise EvidenceExportError("snapshot symlinks are not exportable")
        try:
            snapshot = SafeSnapshot.model_validate_json(path.read_bytes())
        except ValueError as error:
            raise EvidenceExportError(f"invalid safe snapshot for {run_alias}") from error
        result.append(snapshot.model_dump(mode="json"))
    return result


def _run_records(source_root: Path) -> list[dict[str, Any]]:
    source_root = source_root.expanduser().resolve()
    db_path = source_root / "evidence.sqlite3"
    if not db_path.is_file() or db_path.is_symlink():
        raise EvidenceExportError("source evidence.sqlite3 is missing")
    try:
        connection = sqlite3.connect(db_path)
        rows = connection.execute(
            "SELECT run_alias, mode, capability_name, capability_version, bundle_digest, "
            "profile_id, target_revision, browser_version, model_id, state, outcome_code "
            "FROM runs ORDER BY rowid"
        ).fetchall()
        database_digest = _sha256(db_path.read_bytes())
    except (OSError, sqlite3.Error) as error:
        raise EvidenceExportError("source evidence database cannot be read") from error
    if not rows:
        raise EvidenceExportError("source evidence database contains no runs")

    records: list[dict[str, Any]] = []
    for row in rows:
        (run_alias, mode, name, version, digest, profile, revision, browser, model, state, outcome) = row
        try:
            metadata = RunMetadata(
                mode=RunMode(mode),
                capability_name=name,
                capability_version=version,
                bundle_digest=digest,
                profile_id=profile,
                target_revision=revision,
                browser_version=browser,
                model_id=model,
            )
            run_state = RunState(state)
            outcome_code = SafeReasonCode(outcome) if outcome is not None else None
        except (TypeError, ValueError) as error:
            raise EvidenceExportError(f"run metadata is invalid for {run_alias}") from error
        events = _load_events(source_root / run_alias / "events.jsonl", run_alias)
        snapshots = _load_snapshots(source_root / run_alias, run_alias)
        event_rows = connection.execute(
            "SELECT event_id, run_alias, event_type, step_id, state, reason_code, effect_state, duration_ms, control_ref "
            "FROM events WHERE run_alias = ? ORDER BY rowid", (run_alias,)
        ).fetchall()
        if {row[0] for row in event_rows} != {event["event_id"] for event in events}:
            raise EvidenceExportError("event JSONL and database records disagree")
        snapshot_rows = connection.execute(
            "SELECT snapshot_id FROM snapshots WHERE run_alias = ? ORDER BY rowid", (run_alias,)
        ).fetchall()
        if {row[0] for row in snapshot_rows} != {snapshot["snapshot_id"] for snapshot in snapshots}:
            raise EvidenceExportError("snapshot files and database records disagree")
        records.append({
            "run": {
                "run_alias": run_alias,
                **metadata.model_dump(mode="json"),
                "state": run_state.value,
                "outcome_code": outcome_code.value if outcome_code is not None else None,
            },
            "events": events,
            "snapshots": snapshots,
            "source_db_sha256": database_digest,
        })
    connection.close()
    return records


def export_records(
    source_roots: Mapping[str, Path],
    output_dir: Path,
    *,
    source_kind: str,
    source_fingerprint: str | None = None,
    artifact_digest: str | None = None,
    trace_binding: Mapping[str, Any] | None = None,
    forbidden_values: Iterable[bytes] = (),
) -> dict[str, Any]:
    """Export one or more typed EvidenceSink roots as immutable safe records."""
    if source_kind not in _SOURCE_KINDS:
        raise EvidenceExportError("source kind must identify the native target or injected harness")
    if source_fingerprint is not None and (len(source_fingerprint) != 64 or any(c not in "0123456789abcdef" for c in source_fingerprint)):
        raise EvidenceExportError("source fingerprint is not a SHA-256 value")
    if artifact_digest is not None:
        normalized_digest = artifact_digest.removeprefix("sha256:")
        if len(normalized_digest) != 64 or any(c not in "0123456789abcdef" for c in normalized_digest):
            raise EvidenceExportError("artifact digest is not a SHA-256 value")
        artifact_digest = f"sha256:{normalized_digest}"
    all_records: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    for group, root in source_roots.items():
        records = _run_records(root)
        for record in records:
            record["group"] = group
            record["source_kind"] = source_kind
            if source_fingerprint is not None:
                record["source_fingerprint"] = source_fingerprint
            if artifact_digest is not None:
                record["artifact_digest"] = artifact_digest
                raw = record["run"].get("bundle_digest")
                if raw is not None and f"sha256:{raw}" != artifact_digest:
                    raise EvidenceExportError("run bundle digest does not match the requested artifact")
            if trace_binding is not None and group == "discovery":
                record["trace"] = dict(trace_binding)
            encoded = _canonical(record)
            if contains_private_value(encoded, forbidden_values):
                raise EvidenceExportError("private value reached exported evidence")
            record["_encoded"] = encoded
            all_records.append(record)
        groups.append({"group": group, "record_count": len(records)})
    if not all_records:
        raise EvidenceExportError("no records were available for export")

    output_dir = output_dir.expanduser().resolve()
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    if output_dir.exists():
        raise EvidenceExportError(f"refusing to overwrite existing evidence directory: {output_dir}")
    staging = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}-", dir=output_dir.parent))
    index_records: list[dict[str, Any]] = []
    try:
        output_relative = output_dir.relative_to(ROOT).as_posix()
    except ValueError:
        output_relative = output_dir.name
    try:
        for number, record in enumerate(all_records, 1):
            encoded = record.pop("_encoded")
            filename = f"record_{number:03d}_{record['run']['run_alias']}.json"
            path = staging / filename
            path.write_bytes(encoded)
            index_records.append({"path": f"{output_relative}/{filename}", "sha256": _sha256(encoded), "group": record["group"], "run_alias": record["run"]["run_alias"]})
        index = {"schema_version": 1, "record_type": "safe_event_records", "source_kind": source_kind, "source_fingerprint": source_fingerprint, "artifact_digest": artifact_digest, "groups": groups, "records": index_records}
        index_bytes = _canonical(index)
        (staging / "index.json").write_bytes(index_bytes)
        os.replace(staging, output_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    try:
        display_path = output_dir.relative_to(ROOT).as_posix()
    except ValueError:
        display_path = str(output_dir)
    return {"path": display_path, "sha256": _sha256(index_bytes), "record_count": len(index_records), "index": index}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", action="append", required=True, metavar="GROUP=PATH")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--source-kind", choices=sorted(_SOURCE_KINDS), required=True)
    parser.add_argument("--artifact-digest")
    parser.add_argument("--source-fingerprint")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        roots = {}
        for value in args.source_root:
            group, separator, path = value.partition("=")
            if not separator or not group or not path:
                raise EvidenceExportError("--source-root must be GROUP=PATH")
            roots[group] = Path(path)
        result = export_records(roots, Path(args.output_dir), source_kind=args.source_kind, artifact_digest=args.artifact_digest, source_fingerprint=args.source_fingerprint)
    except EvidenceExportError as error:
        print(f"safe evidence export blocked: {error}", file=sys.stderr)
        return 1
    print(json.dumps({key: value for key, value in result.items() if key != "index"}, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
