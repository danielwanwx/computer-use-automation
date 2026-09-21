import sqlite3
from types import SimpleNamespace
from pathlib import Path
import stat

import pytest
from pydantic import ValidationError

from cua.evidence import (
    EvidenceSink,
    RunMode,
    RunMetadata,
    RunState,
    SafeEvent,
    SafeReasonCode,
)
from cua.models.observations import Observation, ObservedControl


def _metadata():
    return RunMetadata(
        mode=RunMode.REPLAY,
        capability_name="get_savings_balance",
        capability_version="1.0.0",
        bundle_digest="a" * 64,
        profile_id="parabank-native-v1",
        target_revision="ee82474be5f58bea3ddc8be0fd831072b00201cb",
        browser_version="1.60.0",
    )


def _private_observation(*, route="/activity.htm?accountId=ACCOUNT_PRIVATE_741"):
    return Observation(
        id="observation_private_741",
        session_id="session_private_741",
        page_id="page_private_741",
        document_generation=5,
        frame_generations={"f_main": 1},
        captured_monotonic_ms=400,
        safe_route=route,
        state_tags=("DETAIL_READY",),
        controls=(
            ObservedControl(
                ref="c_1",
                frame_ref="f_main",
                role="link",
                safe_name="ACCOUNT_PRIVATE_741",
                enabled=True,
                visible=True,
                allowed_operations=("CLICK",),
                binding_ref="inputs.account_id",
            ),
        ),
        safe_text=("CUSTOMER_PRIVATE_MARIGOLD $9812.34",),
        fingerprint="fingerprint_private_741",
    )


def test_sink_persists_only_whitelisted_safe_metadata_events_and_snapshot(tmp_path):
    sink = EvidenceSink(tmp_path / "evidence")
    run_alias = sink.register_run(_metadata())
    sink.emit(
        SafeEvent(
            event_id="e_00000001",
            run_alias=run_alias,
            event_type="OBSERVATION_CAPTURED",
            state=RunState.RUNNING,
            reason_code=SafeReasonCode.AUTHORIZED,
            duration_ms=8,
        )
    )
    reference = sink.capture_safe(run_alias, _private_observation())
    sink.transition_run(run_alias, RunState.RUNNING)
    manifest = sink.finish_manifest(run_alias)

    persisted = b"".join(
        path.read_bytes()
        for path in (tmp_path / "evidence").rglob("*")
        if path.is_file()
    )
    for private_marker in (
        b"ACCOUNT_PRIVATE_741",
        b"CUSTOMER_PRIVATE_MARIGOLD",
        b"9812.34",
        b"session_private_741",
        b"page_private_741",
        b"observation_private_741",
        b"fingerprint_private_741",
        b"accountId=",
        b"https://",
    ):
        assert private_marker not in persisted

    safe_snapshot = Path(tmp_path / "evidence" / run_alias / reference.relative_path)
    assert safe_snapshot.is_file()
    assert manifest.is_file()
    assert sink.get_run(run_alias).state is RunState.RUNNING
    with sqlite3.connect(tmp_path / "evidence" / "evidence.sqlite3") as connection:
        assert connection.execute("select count(*) from events").fetchone() == (1,)
        assert connection.execute("select count(*) from runs").fetchone() == (1,)
    assert stat.S_IMODE((tmp_path / "evidence" / "evidence.sqlite3").stat().st_mode) == 0o600


def test_unknown_view_uses_safe_structural_snapshot_without_raw_route(tmp_path):
    sink = EvidenceSink(tmp_path / "evidence")
    run_alias = sink.register_run(_metadata())

    reference = sink.capture_safe(
        run_alias,
        _private_observation(route="https://private.example/a?account=ACCOUNT_PRIVATE_741"),
    )

    content = (tmp_path / "evidence" / run_alias / reference.relative_path).read_text()
    assert '"safe_route":"UNKNOWN"' in content
    assert '"page_state":"DETAIL_READY"' in content
    assert '"control_count":1' in content
    assert "private.example" not in content
    assert "ACCOUNT_PRIVATE_741" not in content


def test_native_profile_route_keys_are_canonicalized_without_guessing(tmp_path):
    sink = EvidenceSink(tmp_path / "evidence")
    run_alias = sink.register_run(_metadata())
    overview_view = SimpleNamespace(
        profile_id="parabank-native-v1",
        safe_route="accounts_overview",
        page_state="OVERVIEW_READY",
        controls=(),
    )

    reference = sink.capture_safe(run_alias, overview_view)

    content = (tmp_path / "evidence" / run_alias / reference.relative_path).read_text()
    assert '"safe_route":"accounts_overview"' in content
    assert '"profile_id":"parabank-native-v1"' in content


def test_safe_schemas_reject_extra_values_and_unknown_event_reasons(tmp_path):
    sink = EvidenceSink(tmp_path / "evidence")
    run_alias = sink.register_run(_metadata())

    with pytest.raises(ValidationError):
        RunMetadata.model_validate(
            _metadata().model_dump(mode="python") | {"goal": "private goal"}
        )
    with pytest.raises(ValidationError):
        SafeEvent(
            event_id="e_00000001",
            run_alias=run_alias,
            event_type="ACTION_REJECTED",
            reason_code="ACCOUNT_PRIVATE_741",
        )
    with pytest.raises(ValidationError):
        SafeEvent.model_validate(
            {
                "event_id": "e_00000001",
                "run_alias": run_alias,
                "event_type": "ACTION_REJECTED",
                "reason_code": "POLICY_DENIED",
                "raw_value": "ACCOUNT_PRIVATE_741",
            }
        )


def test_run_lifecycle_rejects_terminal_reopening(tmp_path):
    sink = EvidenceSink(tmp_path / "evidence")
    run_alias = sink.register_run(_metadata())
    sink.transition_run(run_alias, RunState.RUNNING)
    sink.transition_run(
        run_alias,
        RunState.SUCCESS,
        outcome_code=SafeReasonCode.REPLAY_COMPLETE,
    )

    with pytest.raises(ValueError):
        sink.transition_run(run_alias, RunState.RUNNING)
