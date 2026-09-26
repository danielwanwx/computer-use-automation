"""Consistency checks for the committed write-up, artifact, and evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from cua.models.bundles import CapabilityBundle
from cua.registry.bundle_registry import canonical_bundle_json
from scripts import render_evidence_log

ROOT = Path(__file__).resolve().parents[1]
REPORT_HEADINGS = [
    "Architecture",
    "Artifact schema",
    "Determinism & error handling",
    "Heterogeneity & multi-tenant",
    "Escalation & handoff",
    "Safety",
    "Cuts",
]


def _json(relative: str):
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def test_report_uses_the_seven_required_headings_in_order():
    headings = [
        line[3:].strip()
        for line in (ROOT / "REPORT.md").read_text(encoding="utf-8").splitlines()
        if line.startswith("## ")
    ]
    assert headings == REPORT_HEADINGS


def test_readme_demo_commands_point_at_existing_scripts():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for script in (
        "scripts/discover_and_replay.py",
        "scripts/review_native_bundle.py",
        "scripts/demo_handoff.py",
        "scripts/demo_service.sh",
    ):
        assert script in readme
        assert (ROOT / script).is_file()


def test_committed_artifact_is_canonical_and_matches_every_index():
    raw = (ROOT / "artifacts/get_savings_balance-1.0.0.json").read_bytes()
    bundle = CapabilityBundle.model_validate_json(raw)
    assert canonical_bundle_json(bundle) == raw
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    entry = _json("artifacts/index.json")["entries"][0]
    assert entry["digest"] == digest
    assert entry["provenance"]["trace_id"] == bundle.provenance.trace_id
    lifecycle = _json("evidence/live_lifecycle.json")
    assert lifecycle["artifact"]["digest"] == digest
    assert lifecycle["trace"]["trace_id"] == bundle.provenance.trace_id
    assert {step.source.type for step in bundle.steps} == {"observed", "declared", "reviewer_added"}


def test_evidence_packages_match_their_recorded_digests():
    lifecycle = _json("evidence/live_lifecycle.json")
    handoff = _json("evidence/handoff_run/handoff_summary.json")
    for index_path, expected in (
        (lifecycle["event_records"]["index_path"], lifecycle["event_records"]["index_sha256"]),
        (handoff["event_records"]["index_path"], handoff["event_records"]["index_sha256"]),
    ):
        index_bytes = (ROOT / index_path).read_bytes()
        assert hashlib.sha256(index_bytes).hexdigest() == expected
        for record in json.loads(index_bytes)["records"]:
            assert hashlib.sha256((ROOT / record["path"]).read_bytes()).hexdigest() == record["sha256"]
    decisions = lifecycle["discovery_decisions"]
    assert hashlib.sha256((ROOT / decisions["path"]).read_bytes()).hexdigest() == decisions["sha256"]


def test_evidence_shows_real_discovery_replay_outcomes_and_handoff():
    lifecycle = _json("evidence/live_lifecycle.json")
    assert lifecycle["provider"]["discovery_decisions"] >= 1
    assert lifecycle["provider"]["replay_provider_calls"] == 0
    assert lifecycle["replay"]["successful_replays"] == 10
    statuses = {case["case"]: case["status"] for case in lifecycle["outcome_replays"]}
    assert statuses["account_does_not_exist"] == "BUSINESS_OUTCOME"
    assert statuses["malformed_account_id"] == "FAILURE"
    handoff = _json("evidence/handoff_run/handoff_summary.json")
    states = [e["state"] for e in handoff["timeline"] if e["kind"] == "intervention_state"]
    assert states == ["WAITING_FOR_HUMAN", "HUMAN_CLAIMED", "RESUMING", "RUNNING"]
    assert handoff["operator_action"]["same_page_as_automation"] is True
    assert handoff["result"]["status"] == "SUCCESS"


def test_run_log_is_rendered_from_the_committed_json():
    assert (ROOT / "evidence/RUN_LOG.md").read_text(encoding="utf-8") == render_evidence_log.render()
