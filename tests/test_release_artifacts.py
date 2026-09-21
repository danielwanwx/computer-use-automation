import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


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
    assert "All V1–V12 statuses remain `NOT_RUN`" in text
    assert "localStorage" in text


def test_verification_entrypoint_keeps_unrun_acceptance_cases_unrun():
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
    assert {case["status"] for case in report["cases"]} == {"NOT_RUN"}
    assert report["summary"] == {"fail": 0, "not_run": 12, "pass": 0}
    assert report["source_fingerprint"]
    assert report["commands"]["offline_tests"]
    assert report["fixture_paths"]
    assert report["evidence_paths"]


def test_release_indexes_do_not_fabricate_capabilities_or_live_evidence():
    artifacts = json.loads((ROOT / "artifacts/index.json").read_text())
    evidence = json.loads((ROOT / "evidence/index.json").read_text())
    assert artifacts["entries"] == []
    assert evidence["entries"] == []
    assert artifacts["status"] == "EMPTY_UNTIL_LIVE_DISCOVERY"
    assert evidence["status"] == "EMPTY_UNTIL_VERIFIED_RUN"
