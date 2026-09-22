import importlib.util
import json
from pathlib import Path
import subprocess
import sys

from scripts import review_native_bundle
from tests.test_native_codex_lifecycle import _successful_native_review_result


ROOT = Path(__file__).resolve().parents[1]


def _load_verifier():
    spec = importlib.util.spec_from_file_location(
        "release_verifier_for_tests", ROOT / "scripts/verify_release.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_report_has_exact_required_headings():
    headings = [
        line.removeprefix("## ")
        for line in (ROOT / "REPORT.md").read_text(encoding="utf-8").splitlines()
        if line.startswith("## ")
    ]
    assert headings == [
        "Architecture",
        "Artifact schema",
        "Determinism & error handling",
        "Heterogeneity & multi-tenant",
        "Escalation & handoff",
        "Safety",
        "Cuts",
    ]
    assert not any(line.startswith("# ") for line in (ROOT / "REPORT.md").read_text().splitlines())


def test_readme_contains_reproducible_setup_target_and_entrypoints():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for command in (
        "uv sync --locked",
        "testbed.parabank prepare",
        "testbed.parabank start",
        "testbed.parabank seed",
        "testbed.parabank reset",
        "cua serve",
        "cua capabilities validate",
        "cua capabilities approve",
        "cua replay",
        "scripts/verify_release.py",
    ):
        assert command in text
    assert "V2 has a current-source native loopback record" in text
    assert "V11 remains `NOT_RUN`" in text
    assert "localStorage" in text
    assert "CUA_PROVIDER_MODEL=codex-cli" not in text


def test_verification_entrypoint_keeps_unsupported_cases_unrun():
    result = subprocess.run(
        [sys.executable, "scripts/verify_release.py", "--skip-tests"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(result.stdout)
    assert len(report["cases"]) == 12
    assert {case["id"] for case in report["cases"]} == {f"V{i}" for i in range(1, 13)}
    statuses = {case["id"]: case["status"] for case in report["cases"]}
    assert statuses["V2"] == "PASS"
    # A partial clean-checkout diagnostic is deliberately not evidence for V11.
    assert statuses["V11"] == "NOT_RUN"
    assert {case_id for case_id, status in statuses.items() if status == "NOT_RUN"} == {
        "V1",
        "V3",
        "V4",
        "V5",
        "V6",
        "V7",
        "V8",
        "V9",
        "V10",
        "V11",
        "V12",
    }
    assert report["summary"] == {"fail": 0, "not_run": 11, "pass": 1}
    assert report["source_fingerprint"]
    assert report["commands"]["offline_tests"]
    assert report["fixture_paths"]
    assert report["evidence_paths"]


def test_release_indexes_do_not_fabricate_capabilities_or_live_artifacts():
    artifacts = json.loads((ROOT / "artifacts/index.json").read_text())
    evidence = json.loads((ROOT / "evidence/index.json").read_text())
    assert len(artifacts["entries"]) == 1
    draft = artifacts["entries"][0]
    assert draft["lifecycle"] == "DRAFT"
    assert draft["approval"]["sidecar_committed"] is False
    assert {entry["case_id"] for entry in evidence["entries"]} == {"V1", "V2", "V11"}
    by_case = {entry["case_id"]: entry for entry in evidence["entries"]}
    assert by_case["V1"]["status"] == "PARTIAL_DIAGNOSTIC"
    assert by_case["V1"]["acceptance_status"] == "NOT_RUN"
    assert by_case["V2"]["status"] == "PASS"
    assert by_case["V11"]["status"] == "PARTIAL_DIAGNOSTIC"
    assert by_case["V11"]["acceptance_status"] == "NOT_RUN"
    assert artifacts["status"] == "LIVE_CODEX_DRAFT_AVAILABLE"
    assert evidence["status"] == "PARTIAL_RELEASE_EVIDENCE"


def test_offline_failure_makes_cli_nonzero(monkeypatch, capsys):
    verifier = _load_verifier()
    monkeypatch.setattr(
        verifier,
        "offline_test",
        lambda root, *, run: {
            "status": "FAIL",
            "command": "pytest -q",
            "exit_code": 1,
        },
    )

    assert verifier.main(["--skip-tests"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["offline_check"]["status"] == "FAIL"
    assert report["summary"]["fail"] == 1


def test_submission_mode_blocks_missing_or_unrun_cases():
    verifier = _load_verifier()

    report = verifier.build_report(ROOT, run_tests=False, submission=True)

    assert report["mode"] == "submission"
    assert report["submission_status"] == "BLOCKED"
    assert report["submission_ready"] is False
    assert all(
        case["status"] in {"PASS", "BLOCKED"} for case in report["cases"]
    )
    assert report["summary"]["blocked"] >= 1
    assert verifier._exit_code(report) == 1


def test_stale_fingerprint_never_promotes_evidence(monkeypatch):
    verifier = _load_verifier()
    current = json.loads((ROOT / "evidence/index.json").read_text(encoding="utf-8"))
    stale = json.loads(json.dumps(current))
    stale["entries"] = [
        entry
        for entry in stale["entries"]
        if entry.get("case_id") == "V2"
    ]
    stale["entries"][0]["source_fingerprint"] = "stale-source"
    monkeypatch.setattr(verifier, "_release_evidence", lambda root: stale)

    report = verifier.build_report(ROOT, run_tests=False)

    assert next(case for case in report["cases"] if case["id"] == "V2")["status"] == "NOT_RUN"


def test_malformed_evidence_is_not_promoted(monkeypatch):
    verifier = _load_verifier()
    malformed = {
        "schema_version": 1,
        "entries": [
            {
                "case_id": "V2",
                "status": "PASS",
                "source_fingerprint": verifier.source_fingerprint(ROOT),
                "evidence_type": "native_loopback",
            }
        ],
    }
    monkeypatch.setattr(verifier, "_release_evidence", lambda root: malformed)

    diagnostic = verifier.build_report(ROOT, run_tests=False)
    strict = verifier.build_report(ROOT, run_tests=False, strict=True)

    assert next(case for case in diagnostic["cases"] if case["id"] == "V2")["status"] == "NOT_RUN"
    assert next(case for case in strict["cases"] if case["id"] == "V2")["status"] == "BLOCKED"


def test_complete_case_specific_evidence_is_promoted():
    verifier = _load_verifier()
    report = verifier.build_report(ROOT, run_tests=False)

    v2 = next(case for case in report["cases"] if case["id"] == "V2")
    assert v2["status"] == "PASS"
    assert v2["evidence"]["validation"] == "PASS"


def test_v1_requires_committed_approved_artifact_and_target_provenance(monkeypatch):
    verifier = _load_verifier()
    evidence = json.loads((ROOT / "evidence/index.json").read_text(encoding="utf-8"))
    v1 = next(entry for entry in evidence["entries"] if entry["case_id"] == "V1")
    v1 = json.loads(json.dumps(v1))
    v1.update(
        {
            "status": "PASS",
            "evidence_type": "live_discovery",
            "source_fingerprint": verifier.source_fingerprint(ROOT),
            "test": {"passed": True},
        }
    )
    v1["trace"]["derived_capability"] = True
    v1["artifact"]["lifecycle"] = "APPROVED"
    v1["artifact"]["sidecar_committed"] = True
    v1["validation"]["validation_run_ref"] = "run_a5ce93086e6c9aa0"
    evidence["entries"] = [
        entry for entry in evidence["entries"] if entry["case_id"] != "V1"
    ] + [v1]
    monkeypatch.setattr(verifier, "_release_evidence", lambda root: evidence)

    sidecar_path = ROOT / "artifacts/get_savings_balance-1.0.0.json.approval.json"
    sidecar = {
        "approved_at": "2026-09-21T00:00:00Z",
        "capability_version": "1.0.0",
        "digest": v1["artifact"]["digest"].removeprefix("sha256:"),
        "reviewer_ref": "reviewer_native_codex",
        "reviewer_type": "independent_reviewer",
        "validation_run_ref": "run_a5ce93086e6c9aa0",
    }
    sidecar_path.write_bytes(
        json.dumps(sidecar, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    )
    try:
        report = verifier.build_report(ROOT, run_tests=False)
    finally:
        sidecar_path.unlink(missing_ok=True)

    promoted = next(case for case in report["cases"] if case["id"] == "V1")
    assert promoted["status"] == "PASS"
    assert promoted["evidence"]["validation"] == "PASS"


def test_v1_missing_artifact_cannot_promote(monkeypatch):
    verifier = _load_verifier()
    evidence = json.loads((ROOT / "evidence/index.json").read_text(encoding="utf-8"))
    v1 = next(entry for entry in evidence["entries"] if entry["case_id"] == "V1")
    v1 = json.loads(json.dumps(v1))
    v1.update(
        {
            "status": "PASS",
            "evidence_type": "live_discovery",
            "source_fingerprint": verifier.source_fingerprint(ROOT),
            "test": {"passed": True},
        }
    )
    v1["trace"]["derived_capability"] = True
    v1["artifact"]["lifecycle"] = "APPROVED"
    v1["artifact"]["sidecar_committed"] = True
    v1["artifact"]["path"] = "artifacts/does-not-exist.json"
    evidence["entries"] = [
        entry for entry in evidence["entries"] if entry["case_id"] != "V1"
    ] + [v1]
    monkeypatch.setattr(verifier, "_release_evidence", lambda root: evidence)

    report = verifier.build_report(ROOT, run_tests=False)

    rejected = next(case for case in report["cases"] if case["id"] == "V1")
    assert rejected["status"] == "NOT_RUN"
    assert rejected["evidence"]["validation"] == "REJECTED"


def test_partial_v11_clean_checkout_diagnostic_is_rejected():
    verifier = _load_verifier()
    report = verifier.build_report(ROOT, run_tests=False)

    v11 = next(case for case in report["cases"] if case["id"] == "V11")
    assert v11["status"] == "NOT_RUN"
    assert v11["evidence"]["validation"] == "REJECTED"


def test_complete_v11_requires_native_no_model_replay_and_approved_artifact(monkeypatch):
    verifier = _load_verifier()
    evidence = json.loads((ROOT / "evidence/index.json").read_text(encoding="utf-8"))
    v11 = next(entry for entry in evidence["entries"] if entry["case_id"] == "V11")
    v11 = json.loads(json.dumps(v11))
    v11.update(
        {
            "status": "PASS",
            "evidence_type": "clean_checkout",
            "source_fingerprint": verifier.source_fingerprint(ROOT),
            "native_target": {
                "prepared": True,
                "loopback": True,
                "pinned_revision": "ee82474be5f58bea3ddc8be0fd831072b00201cb",
            },
            "approved_artifact": {
                "approved": True,
                "reference": "capability/savings-balance@1.0.0",
                "digest": "sha256:approved-digest",
            },
            "no_model_replay": {
                "command": "clean-checkout native replay",
                "artifact_reference": "capability/savings-balance@1.0.0",
                "artifact_digest": "sha256:approved-digest",
                "result": {"passed": True, "exit_code": 0},
                "provider_calls": 0,
                "credentials": "removed",
            },
            "independent_oracle_match": True,
        }
    )
    evidence["entries"] = [
        entry for entry in evidence["entries"] if entry["case_id"] != "V11"
    ] + [v11]
    monkeypatch.setattr(verifier, "_release_evidence", lambda root: evidence)

    monkeypatch.setattr(
        review_native_bundle,
        "_git_metadata",
        lambda: ("5ab604b4c12a8d151d28691a747ae76057088e26", True),
    )
    monkeypatch.setattr(
        review_native_bundle,
        "_source_fingerprint",
        lambda: verifier.source_fingerprint(ROOT),
    )
    review = review_native_bundle._build_evidence(
        ROOT / "artifacts/get_savings_balance-1.0.0.json",
        _successful_native_review_result(),
    )
    review_path = ROOT / "evidence/v11_native_review.json"
    original_safe_path = verifier._safe_release_path
    original_read = verifier._read_canonical_json
    review_bytes = json.dumps(
        review,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    monkeypatch.setattr(
        verifier,
        "_safe_release_path",
        lambda root, relative: review_path
        if relative == "evidence/v11_native_review.json"
        else original_safe_path(root, relative),
    )
    monkeypatch.setattr(
        verifier,
        "_read_canonical_json",
        lambda path: (review_bytes, review)
        if path == review_path
        else original_read(path),
    )
    v11["review_evidence_path"] = "evidence/v11_native_review.json"

    report = verifier.build_report(ROOT, run_tests=False)

    promoted = next(case for case in report["cases"] if case["id"] == "V11")
    assert promoted["status"] == "PASS"
    assert promoted["evidence"]["validation"] == "PASS"
