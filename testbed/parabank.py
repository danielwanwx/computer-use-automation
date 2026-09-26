"""Prepare and run the pinned native ParaBank target for local tests.

The script downloads only pinned upstream/toolchain artifacts, verifies their
SHA-512 digests, binds service listeners to loopback, and keeps generated files
under ``testbed/.cache``. It does not change ParaBank application pages or bank
logic.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import urlopen


UPSTREAM_URL = "https://github.com/parasoft/parabank.git"
UPSTREAM_COMMIT = "ee82474be5f58bea3ddc8be0fd831072b00201cb"
MAVEN_VERSION = "3.9.9"
MAVEN_SHA512 = "a555254d6b53d267965a3404ecb14e53c3827c09c3b94b5678835887ab404556bfaf78dcfe03ba76fa2508649dca8531c74bca4d5846513522404d48e8c4ac8b"
TOMCAT_VERSION = "10.1.60"
TOMCAT_SHA512 = "aa06508300ca137a023b74b8600f2c1b3248412eb85d4fc5e2f337c6c4d3776f4491e272f79856ac541cfab0fc35537111ae4f3cfcd0bbe702c0a3610a61bd04"
DEFAULT_ORIGIN = "http://127.0.0.1:8080/parabank"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CACHE = Path(os.environ.get("PARABANK_TESTBED_CACHE", PROJECT_ROOT / "testbed" / ".cache"))
TOOLCHAIN = CACHE / "toolchain"


class TestbedError(RuntimeError):
    __test__ = False  # not a pytest test class despite the name


def _run(command: Sequence[str], *, cwd: Optional[Path] = None, env=None) -> str:
    try:
        result = subprocess.run(
            list(command), cwd=str(cwd) if cwd else None, env=env,
            check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise TestbedError("command failed: {} (exit {})".format(command[0], exc.returncode)) from None
    return result.stdout


def _verify_sha512(path: Path, expected: str) -> None:
    digest = hashlib.sha512()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        path.unlink(missing_ok=True)
        raise TestbedError("SHA-512 verification failed for {}".format(path.name))


def _download(url: str, destination: Path, expected_sha512: str) -> None:
    if destination.exists():
        try:
            _verify_sha512(destination, expected_sha512)
            return
        except TestbedError:
            pass
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=destination.name + ".", dir=str(destination.parent))
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        try:
            with urlopen(url, timeout=120) as response, temporary.open("wb") as output:
                shutil.copyfileobj(response, output)
        except (HTTPError, URLError, TimeoutError, OSError):
            raise TestbedError("download failed for pinned artifact {}".format(url)) from None
        _verify_sha512(temporary, expected_sha512)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _extract_verified(archive: Path, parent: Path, directory_name: str) -> Path:
    destination = parent / directory_name
    if destination.is_dir():
        return destination
    parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as bundle:
        # Archives are accepted only after a hard-coded official SHA-512 check.
        bundle.extractall(parent)
    if not destination.is_dir():
        raise TestbedError("verified archive did not contain {}".format(directory_name))
    return destination


def _ensure_maven() -> Path:
    configured = os.environ.get("PARABANK_MAVEN_HOME")
    home = Path(configured) if configured else TOOLCHAIN / "apache-maven-{}".format(MAVEN_VERSION)
    if (home / "bin" / "mvn").is_file():
        return home
    if configured:
        raise TestbedError("PARABANK_MAVEN_HOME does not contain bin/mvn")
    archive = TOOLCHAIN / "apache-maven-{}-bin.tar.gz".format(MAVEN_VERSION)
    url = "https://archive.apache.org/dist/maven/maven-3/{}/binaries/{}".format(
        MAVEN_VERSION, archive.name
    )
    _download(url, archive, MAVEN_SHA512)
    return _extract_verified(archive, TOOLCHAIN, "apache-maven-{}".format(MAVEN_VERSION))


def _ensure_tomcat() -> Path:
    configured = os.environ.get("PARABANK_TOMCAT_HOME")
    home = Path(configured) if configured else TOOLCHAIN / "apache-tomcat-{}".format(TOMCAT_VERSION)
    if (home / "bin" / "catalina.sh").is_file():
        return home
    if configured:
        raise TestbedError("PARABANK_TOMCAT_HOME does not contain bin/catalina.sh")
    archive = TOOLCHAIN / "apache-tomcat-{}.tar.gz".format(TOMCAT_VERSION)
    url = "https://downloads.apache.org/tomcat/tomcat-10/v{}/bin/{}".format(
        TOMCAT_VERSION, archive.name
    )
    _download(url, archive, TOMCAT_SHA512)
    return _extract_verified(archive, TOOLCHAIN, "apache-tomcat-{}".format(TOMCAT_VERSION))


def _ensure_source() -> Path:
    configured = os.environ.get("PARABANK_SOURCE_DIR")
    if configured:
        source = Path(configured).resolve()
        actual = _run(["git", "rev-parse", "HEAD"], cwd=source).strip()
        if actual != UPSTREAM_COMMIT:
            raise TestbedError("PARABANK_SOURCE_DIR is not pinned to the required commit")
        return source

    source = CACHE / "parabank-source"
    CACHE.mkdir(parents=True, exist_ok=True)
    if not (source / ".git").is_dir():
        if source.exists():
            shutil.rmtree(source)
        _run(["git", "clone", "--filter=blob:none", "--no-checkout", UPSTREAM_URL, str(source)])
    _run(["git", "fetch", "--depth", "1", "origin", UPSTREAM_COMMIT], cwd=source)
    _run(["git", "checkout", "--detach", "--force", UPSTREAM_COMMIT], cwd=source)
    actual = _run(["git", "rev-parse", "HEAD"], cwd=source).strip()
    if actual != UPSTREAM_COMMIT:
        raise TestbedError("upstream checkout does not match the immutable commit pin")
    return source


def _replace_exactly_once(path: Path, original: str, replacement: str) -> None:
    text = path.read_text(encoding="utf-8")
    old_count = text.count(original)
    new_count = text.count(replacement)
    if old_count == 1 and new_count == 0:
        text = text.replace(original, replacement, 1)
        path.write_text(text, encoding="utf-8")
    elif old_count == 0 and new_count == 1:
        return
    else:
        raise TestbedError("unexpected pinned config shape in {}".format(path.name))


def _bind_services_to_loopback(source: Path, tomcat: Path) -> None:
    _replace_exactly_once(
        source / "src/main/resources/applicationContext-jms.xml",
        "tcp://0.0.0.0:61616?transport.daemon=true",
        "tcp://127.0.0.1:61616?transport.daemon=true",
    )
    hsqldb = source / "src/main/resources/applicationContext-hsqldb.xml"
    text = hsqldb.read_text(encoding="utf-8")
    loopback = '<prop key="server.address">127.0.0.1</prop>'
    server_silent = '<prop key="server.silent">true</prop>'
    if loopback in text and text.count(loopback) == 1:
        pass
    elif text.count(server_silent) == 1 and loopback not in text:
        hsqldb.write_text(text.replace(server_silent, loopback + "\n\t\t\t\t" + server_silent, 1), encoding="utf-8")
    else:
        raise TestbedError("unexpected pinned HSQLDB config shape")

    tomcat_xml = tomcat / "conf/server.xml"
    _replace_exactly_once(
        tomcat_xml,
        '<Connector port="8080" protocol="HTTP/1.1"',
        '<Connector port="8080" address="127.0.0.1" protocol="HTTP/1.1"',
    )


def _origin() -> str:
    origin = os.environ.get("PARABANK_ORIGIN", DEFAULT_ORIGIN).rstrip("/")
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except ValueError:
        raise TestbedError("PARABANK_ORIGIN is not a valid URL") from None
    if (
        parsed.scheme != "http"
        or parsed.hostname not in ("127.0.0.1", "localhost")
        or parsed.username is not None
        or parsed.password is not None
        or port != 8080
        or parsed.path != "/parabank"
        or parsed.query
        or parsed.fragment
        or "?" in origin
        or "#" in origin
    ):
        raise TestbedError("PARABANK_ORIGIN must be http://127.0.0.1:8080/parabank or localhost equivalent")
    return "http://{}:8080/parabank".format(parsed.hostname)


def check_health(timeout: float = 2.5) -> bool:
    url = _origin() + "/"
    try:
        with urlopen(url, timeout=timeout) as response:
            return response.status == 200
    except (HTTPError, URLError, TimeoutError, OSError):
        return False


def prepare() -> Path:
    java = shutil.which("java")
    if java is None:
        raise TestbedError("Java is required to run native ParaBank")
    _run([java, "-version"])
    source = _ensure_source()
    maven = _ensure_maven()
    tomcat = _ensure_tomcat()
    _bind_services_to_loopback(source, tomcat)
    _run([str(maven / "bin/mvn"), "-B", "-ntp", "-Dmaven.test.skip=true", "package"], cwd=source)
    war = source / "target/parabank-5.0.0-SNAPSHOT.war"
    if not war.is_file():
        raise TestbedError("pinned upstream build did not produce the expected WAR")
    webapps = tomcat / "webapps"
    webapps.mkdir(exist_ok=True)
    shutil.copy2(war, webapps / "parabank.war")
    digest = hashlib.sha256(war.read_bytes()).hexdigest()
    manifest = {
        "schema_version": 1,
        "target": "official ParaBank",
        "source": {"repository": UPSTREAM_URL, "immutable_commit": UPSTREAM_COMMIT},
        "build": {
            "command": "mvn -B -ntp -Dmaven.test.skip=true package",
            "maven_version": MAVEN_VERSION,
            "war_sha256": digest,
        },
        "runtime": {
            "tomcat_version": TOMCAT_VERSION,
            "http_connector": "127.0.0.1:8080",
            "activemq_connector": "127.0.0.1:61616",
            "hsqldb_bind_address": "127.0.0.1",
        },
        "built_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    CACHE.mkdir(parents=True, exist_ok=True)
    manifest_path = CACHE / "deployment_manifest.json"
    temporary = manifest_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(manifest_path)
    return tomcat


def start() -> None:
    if check_health():
        print("ParaBank healthy on loopback (HTTP 200)")
        return
    tomcat = prepare()
    env = os.environ.copy()
    env["CATALINA_HOME"] = str(tomcat)
    env["CATALINA_BASE"] = str(tomcat)
    env["CATALINA_PID"] = str(CACHE / "tomcat.pid")
    try:
        _run([str(tomcat / "bin/catalina.sh"), "start"], env=env)
    except TestbedError:
        raise TestbedError("Tomcat failed to start; inspect testbed/.cache/toolchain/apache-tomcat-10.1.60/logs") from None
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if check_health():
            print("ParaBank healthy on loopback (HTTP 200)")
            return
        time.sleep(1)
    raise TestbedError("ParaBank did not become healthy within 90 seconds; inspect the Tomcat logs")


def stop() -> None:
    tomcat = Path(os.environ.get("PARABANK_TOMCAT_HOME", TOOLCHAIN / "apache-tomcat-{}".format(TOMCAT_VERSION)))
    script = tomcat / "bin/catalina.sh"
    if not script.is_file():
        print("Tomcat is not installed")
        return
    env = os.environ.copy()
    env["CATALINA_HOME"] = str(tomcat)
    env["CATALINA_BASE"] = str(tomcat)
    env["CATALINA_PID"] = str(CACHE / "tomcat.pid")
    try:
        _run([str(script), "stop", "-force"], env=env)
    except TestbedError:
        if check_health():
            raise
    print("Tomcat stop requested")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "start", "health", "seed", "reset", "stop"))
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            prepare()
            print("Pinned ParaBank WAR prepared; target code remains upstream")
        elif args.command == "start":
            start()
        elif args.command == "health":
            if not check_health():
                print("ParaBank is not healthy on loopback", file=sys.stderr)
                return 1
            print("ParaBank healthy on loopback (HTTP 200)")
        elif args.command in ("seed", "reset"):
            from testbed.seed import seed
            manifest = seed()
            print("Synthetic ParaBank fixture ready; manifest: {}".format(manifest))
        else:
            stop()
    except TestbedError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
