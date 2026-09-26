from __future__ import annotations

import json

import pytest

from cua.evidence import EvidenceSink, RunMetadata, RunMode, RunState, SafeEvent, SafeReasonCode
from scripts.export_safe_evidence import EvidenceExportError, export_records


def _metadata(*, mode: RunMode, digest: str | None = None) -> RunMetadata:
    return RunMetadata(
        mode=mode,
        capability_name="get_savings_balance",
        capability_version="1.0.0",
        bundle_digest=digest,
        profile_id="parabank-native-v1",
        target_revision="ee82474be5f58bea3ddc8be0fd831072b00201cb",
        browser_version="1.60.0",
        model_id="codex-cli" if mode is RunMode.DISCOVERY else None,
    )


def _source(tmp_path, *, mode: RunMode, digest: str | None = None, with_event: bool = True):
    sink = EvidenceSink(tmp_path / "source")
    run_alias = sink.register_run(_metadata(mode=mode, digest=digest))
    sink.transition_run(run_alias, RunState.RUNNING)
    if with_event:
        sink.emit(
            SafeEvent(
                event_id="e_export_1",
                run_alias=run_alias,
                event_type="ACTION_EFFECT_VERIFIED",
                step_id="click_account",
                reason_code=SafeReasonCode.AUTHORIZED,
                effect_state="VERIFIED",
            )
        )
    sink.transition_run(
        run_alias,
        RunState.SUCCESS,
        outcome_code=(
            SafeReasonCode.DISCOVERY_COMPLETE
            if mode is RunMode.DISCOVERY
            else SafeReasonCode.REPLAY_COMPLETE
        ),
    )
    return tmp_path / "source"


def test_export_records_preserves_typed_events_and_bindings(tmp_path):
    source = _source(tmp_path, mode=RunMode.DISCOVERY)
    output = tmp_path / "exported"

    result = export_records(
        {"discovery": source},
        output,
        source_kind="native_target",
        source_fingerprint="a" * 64,
        trace_binding={"trace_id": "trace_live", "event_ids": ["event_1"], "step_ids": ["click_account"]},
    )

    assert result["record_count"] == 1
    index_path = output / "index.json"
    assert index_path.read_bytes() == json.dumps(
        result["index"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    record_path = output / result["index"]["records"][0]["path"].rsplit("/", 1)[-1]
    record = json.loads(record_path.read_text())
    assert record["source_kind"] == "native_target"
    assert record["source_fingerprint"] == "a" * 64
    assert record["trace"]["trace_id"] == "trace_live"
    assert [event["event_type"] for event in record["events"]] == [
        "RUN_STATE_CHANGED",
        "ACTION_EFFECT_VERIFIED",
        "RUN_STATE_CHANGED",
    ]
    assert record["events"][-1]["state"] == "SUCCESS"


def test_export_records_rejects_missing_events_and_private_values(tmp_path):
    # A run that was registered but never started has no event log to export.
    empty = tmp_path / "empty" / "source"
    EvidenceSink(empty).register_run(_metadata(mode=RunMode.DISCOVERY))
    with pytest.raises(EvidenceExportError, match="event log is missing"):
        export_records({"discovery": empty}, tmp_path / "empty-out", source_kind="native_target")

    source = _source(tmp_path / "private", mode=RunMode.DISCOVERY)
    with pytest.raises(EvidenceExportError, match="private value"):
        export_records(
            {"discovery": source},
            tmp_path / "private-out",
            source_kind="native_target",
            forbidden_values=(b"ACTION_EFFECT_VERIFIED",),
        )


def test_export_records_binds_replay_artifact_digest(tmp_path):
    source = _source(tmp_path, mode=RunMode.REPLAY, digest="a" * 64)
    with pytest.raises(EvidenceExportError, match="bundle digest"):
        export_records(
            {"replay": source},
            tmp_path / "out",
            source_kind="native_target",
            artifact_digest="sha256:" + "b" * 64,
        )
