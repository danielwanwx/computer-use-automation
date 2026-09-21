#!/usr/bin/env python3
"""Emit the honest V1–V12 release matrix for this checkout.

The script runs only the offline automated suite by default.  Native target,
provider, clean-checkout, and real-person cases stay NOT_RUN until their
required evidence exists; the script never promotes those cases from a unit
test result to PASS.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]

_OFFLINE_CASES = {"V3", "V4", "V5", "V6", "V7", "V8", "V10"}
_CASES: tuple[dict[str, Any], ...] = (
    {
        "id": "V1",
        "title": "real discovery and trace source",
        "required": "live provider-backed discovery with a trace-derived capability",
        "status": "NOT_RUN",
        "reason": "Provider use was deferred; no live discovery artifact exists.",
        "command": ".venv/bin/python -B -m pytest -p no:cacheprovider tests/test_discovery_native.py",
    },
    {
        "id": "V2",
        "title": "new inputs and changed balance",
        "required": "two native customers, five replay runs each, and an observed balance change",
        "status": "NOT_RUN",
        "reason": "The required final native rerun has not been performed after source changes.",
        "command": ".venv/bin/python -B -m pytest -p no:cacheprovider tests/test_discovery_native.py",
    },
    {
        "id": "V3",
        "title": "zero-model replay",
        "required": "native replay and recovery with model credentials removed",
        "status": "NOT_RUN",
        "reason": "The live/native criterion remains pending; offline model-trap checks are reported separately.",
        "command": ".venv/bin/python -B -m pytest -p no:cacheprovider tests/test_replay.py",
    },
    {
        "id": "V4",
        "title": "business and input boundaries",
        "required": "native account-not-found, access-denied, and missing-input outcomes",
        "status": "NOT_RUN",
        "reason": "Native target boundary cases have not been rerun for the release source.",
        "command": ".venv/bin/python -B -m pytest -p no:cacheprovider tests/test_replay.py tests/test_completion_verifier.py",
    },
    {
        "id": "V5",
        "title": "wrong-success protection",
        "required": "native wrong-member, wrong-account, wrong-type, duplicate-target, and evaluator cases",
        "status": "NOT_RUN",
        "reason": "Native/evaluator release evidence is pending; offline verifier checks are reported separately.",
        "command": ".venv/bin/python -B -m pytest -p no:cacheprovider tests/test_completion_verifier.py tests/test_replay.py",
    },
    {
        "id": "V6",
        "title": "recovery and stopping",
        "required": "native transient recovery, exhaustion, and lost-receipt behavior",
        "status": "NOT_RUN",
        "reason": "No final native fault-injection run is recorded.",
        "command": ".venv/bin/python -B -m pytest -p no:cacheprovider tests/test_replay.py tests/test_execution_gateway.py",
    },
    {
        "id": "V7",
        "title": "control and capability validity",
        "required": "native stale/frame/condition checks and post-approval dependency invalidation",
        "status": "NOT_RUN",
        "reason": "The release matrix keeps native qualification separate from offline registry checks.",
        "command": ".venv/bin/python -B -m pytest -p no:cacheprovider tests/test_bundle_registry.py tests/test_compiler.py",
    },
    {
        "id": "V8",
        "title": "takeover protocol",
        "required": "actor handoff races, stale epochs, and wrong-principal resume",
        "status": "NOT_RUN",
        "reason": "Protocol tests exist, but the application handoff is not fully wired to a native session.",
        "command": ".venv/bin/python -B -m pytest -p no:cacheprovider tests/test_handoff_service.py tests/test_sessions_actor.py",
    },
    {
        "id": "V9",
        "title": "real human takeover",
        "required": "a person operates the same browser session and resumes successfully",
        "status": "NOT_RUN",
        "reason": "Manual intervention was not performed.",
        "command": "manual same-session operator test",
    },
    {
        "id": "V10",
        "title": "policy and data protection",
        "required": "zero forbidden side effects, zero canary leakage, and usable safe evidence",
        "status": "NOT_RUN",
        "reason": "Native canary and forbidden-target release evidence is pending.",
        "command": ".venv/bin/python -B -m pytest -p no:cacheprovider tests/test_policy.py tests/test_evidence_sink.py",
    },
    {
        "id": "V11",
        "title": "clean environment reproduction",
        "required": "fresh checkout setup and no-model replay without development state",
        "status": "NOT_RUN",
        "reason": "A fresh checkout reproduction has not been executed.",
        "command": "uv sync --locked && .venv/bin/python -B -m pytest -p no:cacheprovider -q",
    },
    {
        "id": "V12",
        "title": "operator page",
        "required": "browser UI from discovery through approval, replay, result, and takeover",
        "status": "NOT_RUN",
        "reason": "HTTP contract checks pass, but browser end-to-end and takeover UI evidence are not recorded.",
        "command": ".venv/bin/python -B -m pytest -p no:cacheprovider tests/test_web_app.py tests/test_cli.py",
    },
)


def _files_for_fingerprint(root: Path) -> list[Path]:
    paths = [root / "pyproject.toml", root / "uv.lock"]
    for directory in (root / "src", root / "testbed"):
        if not directory.exists():
            continue
        paths.extend(
            path
            for path in directory.rglob("*.py")
            if "__pycache__" not in path.parts
        )
    return sorted(path for path in paths if path.is_file())


def source_fingerprint(root: Path = ROOT) -> str:
    digest = hashlib.sha256()
    for path in _files_for_fingerprint(root):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def runtime_fingerprint(root: Path = ROOT) -> dict[str, Any]:
    source_path = str(root / "src")
    if source_path not in sys.path:
        sys.path.insert(0, source_path)
    try:
        from cua.registry.runtime_fingerprint import current_runtime_fingerprint

        return {
            "status": "AVAILABLE",
            **current_runtime_fingerprint().model_dump(mode="json"),
        }
    except Exception:
        return {"status": "UNAVAILABLE", "reason": "runtime fingerprint import failed"}


def offline_test(root: Path = ROOT, *, run: bool = True) -> dict[str, Any]:
    python = root / ".venv" / "bin" / "python"
    executable = str(python if python.is_file() else Path(sys.executable))
    command = [
        executable,
        "-B",
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-q",
    ]
    if not run:
        return {"status": "NOT_RUN", "command": " ".join(command), "exit_code": None}
    environment = dict(__import__("os").environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=300,
            check=False,
        )
        return {
            "status": "PASS" if completed.returncode == 0 else "FAIL",
            "command": " ".join(command),
            "exit_code": completed.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"status": "FAIL", "command": " ".join(command), "exit_code": "TIMEOUT"}
    except OSError:
        return {"status": "FAIL", "command": " ".join(command), "exit_code": "UNAVAILABLE"}


def _path_status(root: Path, relative: str) -> dict[str, Any]:
    return {"path": relative, "exists": (root / relative).exists()}


def build_report(root: Path = ROOT, *, run_tests: bool = True) -> dict[str, Any]:
    checks = offline_test(root, run=run_tests)
    cases: list[dict[str, Any]] = []
    for original in _CASES:
        case = dict(original)
        case["offline_check"] = (
            checks["status"] if case["id"] in _OFFLINE_CASES else "NOT_APPLICABLE"
        )
        # An aggregate offline suite cannot prove a case-specific native or
        # manual acceptance criterion. Keep the acceptance status NOT_RUN
        # until a fingerprinted evidence manifest names that exact case.
        case["status"] = "NOT_RUN"
        case["status_basis"] = "No case-specific release evidence manifest is present."
        cases.append(case)
    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_fingerprint": source_fingerprint(root),
        "runtime_fingerprint": runtime_fingerprint(root),
        "offline_check": checks,
        "commands": {
            "setup": "uv sync --locked",
            "offline_tests": checks["command"],
            "native_prepare": ".venv/bin/python -m testbed.parabank prepare",
            "native_start": ".venv/bin/python -m testbed.parabank start",
            "native_seed": ".venv/bin/python -m testbed.parabank seed",
            "server": ".venv/bin/cua serve",
        },
        "fixture_paths": [
            _path_status(root, "testbed/fixtures/session_manifest.json"),
            _path_status(root, "testbed/.cache/deployment_manifest.json"),
            _path_status(root, "testbed/.cache/seed_manifest.json"),
        ],
        "evidence_paths": [
            _path_status(root, "artifacts/index.json"),
            _path_status(root, "evidence/index.json"),
            {"path": "testbed/.cache/", "exists": (root / "testbed/.cache").is_dir()},
        ],
        "cases": cases,
        "summary": {
            "pass": sum(case["status"] == "PASS" for case in cases),
            "fail": sum(case["status"] == "FAIL" for case in cases),
            "not_run": sum(case["status"] == "NOT_RUN" for case in cases),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="report the matrix without running the offline pytest command",
    )
    args = parser.parse_args(argv)
    report = build_report(ROOT, run_tests=not args.skip_tests)
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if report["summary"]["fail"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
