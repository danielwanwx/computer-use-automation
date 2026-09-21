import hashlib
from pathlib import Path
import sys
from typing import Any

from cua.models.qualification import RuntimeFingerprint


_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_PROJECT_ROOT = _PACKAGE_ROOT.parents[1]


def current_runtime_fingerprint(*, project_root: Path | None = None) -> RuntimeFingerprint:
    """Hash executable source, lock/config closure, and Python implementation."""
    project_root = Path(project_root).resolve() if project_root is not None else _PROJECT_ROOT
    source_files = sorted(_PACKAGE_ROOT.rglob("*.py"))
    source_hash = hashlib.sha256()
    for source_file in source_files:
        relative_name = source_file.relative_to(_PACKAGE_ROOT).as_posix().encode("utf-8")
        source_hash.update(len(relative_name).to_bytes(4, "big"))
        source_hash.update(relative_name)
        content = source_file.read_bytes()
        source_hash.update(len(content).to_bytes(8, "big"))
        source_hash.update(content)
    for project_file in (project_root / "pyproject.toml", project_root / "uv.lock"):
        _update_hash(source_hash, project_file.relative_to(project_root).as_posix(), project_file.read_bytes())
    implementation = (
        f"{sys.implementation.name}:{sys.version_info.major}.{sys.version_info.minor}"
    ).encode("ascii")
    source_hash.update(b"python-implementation")
    source_hash.update(len(implementation).to_bytes(4, "big"))
    source_hash.update(implementation)

    return RuntimeFingerprint(
        source_sha256=source_hash.hexdigest(),
        parser_sha256=_hash_file(_PACKAGE_ROOT / "conditions" / "parsers.py"),
        condition_sha256=_hash_files(
            (
                _PACKAGE_ROOT / "conditions" / "evaluator.py",
                _PACKAGE_ROOT / "models" / "conditions.py",
            )
        ),
        profile_sha256=_hash_file(_PACKAGE_ROOT / "profiles" / "parabank.py"),
    )


def _update_hash(digest: Any, relative_name: str, content: bytes) -> None:
    encoded_name = relative_name.encode("utf-8")
    digest.update(len(encoded_name).to_bytes(4, "big"))
    digest.update(encoded_name)
    digest.update(len(content).to_bytes(8, "big"))
    digest.update(content)


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hash_files(paths: tuple[Path, ...]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        name = path.relative_to(_PACKAGE_ROOT).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()
