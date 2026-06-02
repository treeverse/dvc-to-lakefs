import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from secrets import token_hex, token_urlsafe
from typing import TYPE_CHECKING

import lakefs
import pytest
from dvc_azure.tests.fixtures import azure_container, azurite, make_azure
from dvc_gs.tests.fixtures import fake_gcs_server, gs_bucket, gs_client, make_gs
from dvc_s3.tests.fixtures import (
    make_s3,
    reset_s3_fixture,
    s3_bucket,
    s3_client,
    s3_config,
    s3_server,
)

from tests.conftest import BACKENDS

if TYPE_CHECKING:
    from dvc.testing.cloud import Cloud
    from dvc.testing.tmp_dir import TmpDir

    class RemoteFactory:
        def __call__(
            self, name: str, typ: str = "local", *, default: bool = True
        ) -> "TmpDir | Cloud": ...


__all__ = [
    "azure_container",
    "azurite",
    "fake_gcs_server",
    "gs_bucket",
    "gs_client",
    "make_azure",
    "make_gs",
    "make_s3",
    "reset_s3_fixture",
    "s3_bucket",
    "s3_client",
    "s3_config",
    "s3_server",
]


def _parse_connection_string(conn_str: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for seg in conn_str.split(";"):
        if "=" in seg:
            k, _, v = seg.partition("=")
            result[k] = v
    return result


def _azure_namespace_base(conn_str: str) -> str:
    """Canonical host-style blob URL (https://<account>.blob.core.windows.net)
    for the Azurite account.

    lakeFS only understands host-style Azure URLs, where the storage account is
    a subdomain of the host. Azurite's real endpoint is IP-style
    (http://127.0.0.1:10000/<account>), which lakeFS rejects in namespace
    validation and would mis-parse anyway ("127" as the account, "<account>" as
    the container). So every URL handed to lakeFS (storage namespaces, import
    sources) must use this canonical form even though nothing is ever served
    there. That is safe because lakeFS never connects to the host in these
    URLs - it only parses account/container/key out of them; actual traffic is
    routed to Azurite by ``test_endpoint_url`` in the blockstore config.
    """
    parts = _parse_connection_string(conn_str)
    return f"https://{parts['AccountName']}.blob.core.windows.net"


def _azurite_test_endpoint(conn_str: str) -> str:
    """Azurite blob endpoint for lakeFS test_endpoint_url override."""
    parts = _parse_connection_string(conn_str)
    return parts.get(
        "BlobEndpoint",
        f"http://127.0.0.1:10000/{parts['AccountName']}",
    ).rstrip("/")


@pytest.fixture(scope="session")
def host(request: pytest.FixtureRequest) -> str | None:
    """Consumed by dvc_azure's ``azurite`` fixture: when ``--azurite-host`` is
    given, tests use that running Azurite instead of starting a docker container."""
    host = request.config.getoption("--azurite-host")
    assert host is None or isinstance(host, str)
    return host


@pytest.fixture(scope="session", autouse=True)
def fast_import_polling(monkeypatch_session: pytest.MonkeyPatch) -> None:
    """Poll lakeFS imports tightly: test imports finish in well under a second, so
    the SDK's default 2s cadence (sleeping before the first check) would otherwise
    add ~2s per import to the suite."""
    from datetime import timedelta

    monkeypatch_session.setattr(
        "dvc_to_lakefs.core.IMPORT_POLL_INTERVAL", timedelta(seconds=0.1)
    )


@pytest.fixture(scope="session")
def server_addr() -> str:
    with socket.socket() as sock:
        sock.bind(("localhost", 0))
        port = sock.getsockname()[1]
    return f"localhost:{port}"


@pytest.fixture(scope="session")
def lakefs_local_data(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Base directory of the lakeFS local blockstore adapter."""
    return tmp_path_factory.mktemp("lakefs-local-data")


# Each backend is pinned to its own xdist_group so that, under --dist loadgroup,
# all of a backend's tests run on one worker — letting the backends run in parallel
# without colliding on the fixed-port emulators (gs's fake-gcs-server on :4443,
# azurite), each of which is then owned by exactly one worker. The backend list and
# the matching ``-n auto`` worker count live in the root conftest (see BACKENDS).
#
# lakeFS's local blockstore cannot import from Windows paths (forward-slash URI vs
# backslash filepath.Clean), so the local backend is skipped there.
_LOCAL_WIN32_SKIP = pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "lakeFS local blockstore cannot import from Windows paths"
        " (forward-slash URI vs backslash filepath.Clean incompatibility)"
    ),
)


@pytest.fixture(
    scope="session",
    params=[
        pytest.param(
            backend,
            marks=[
                pytest.mark.xdist_group(backend),
                *([_LOCAL_WIN32_SKIP] if backend == "local" else []),
            ],
        )
        for backend in BACKENDS
    ],
)
def backend_name(request: pytest.FixtureRequest) -> str:
    param = request.param
    assert isinstance(param, str)
    return param


@pytest.fixture(scope="session")
def blockstore(backend_name: str, request: pytest.FixtureRequest) -> dict[str, object]:
    if backend_name == "local":
        local_data = request.getfixturevalue("lakefs_local_data")
        tmp_path_factory = request.getfixturevalue("tmp_path_factory")
        return {
            "type": "local",
            "local": {
                "path": str(local_data),
                "import_enabled": True,
                # Allow any path under the pytest session temp dir so the DVC
                # cache (which lives in per-test tmp_path subdirs) is reachable.
                "allowed_external_prefixes": [str(tmp_path_factory.getbasetemp())],
            },
        }
    if backend_name == "s3":
        s3_config = request.getfixturevalue("s3_config")
        return {
            "type": "s3",
            "s3": {
                "endpoint": s3_config["endpoint_url"],
                "credentials": {
                    "access_key_id": s3_config["aws_access_key_id"],
                    "secret_access_key": s3_config["aws_secret_access_key"],
                },
            },
        }
    if backend_name == "gs":
        monkeypatch = request.getfixturevalue("monkeypatch_session")
        monkeypatch.setenv("STORAGE_EMULATOR_HOST", "http://localhost:4443")
        # See https://github.com/fsouza/fake-gcs-server/issues/2237
        monkeypatch.setenv("GCSFS_EXPERIMENTAL_ZB_HNS_SUPPORT", "false")
        return {
            "type": "gs",
            # disable_pre_signed: fake-gcs-server has no credentials for URL signing
            "gs": {"disable_pre_signed": True},
        }
    if backend_name == "azure":
        connection_string = request.getfixturevalue("azurite")
        monkeypatch = request.getfixturevalue("monkeypatch_session")
        monkeypatch.setenv("AZURE_STORAGE_CONNECTION_STRING", connection_string)
        parts = _parse_connection_string(connection_string)
        return {
            "type": "azure",
            "azure": {
                "storage_account": parts["AccountName"],
                "storage_access_key": parts["AccountKey"],
                "test_endpoint_url": _azurite_test_endpoint(connection_string),
                # TODO: remove once lakeFS uses ProtocolHTTPSandHTTP when
                # TestEndpointURL is http:// (presigned SAS tokens hardcode
                # spr=https, which Azurite rejects over HTTP)
                "disable_pre_signed": True,
            },
        }
    raise ValueError(f"unsupported backend: {backend_name}")


@pytest.fixture(scope="session")
def lakefs_config(
    tmp_path_factory: pytest.TempPathFactory,
    blockstore: dict[str, object],
    server_addr: str,
) -> Path:
    from dvc.utils.serialize import dump_yaml

    path = tmp_path_factory.mktemp("lakefs") / "config.yaml"
    config = {
        "listen_address": server_addr,
        "auth": {
            "encrypt": {
                "secret_key": "random_key",
            }
        },
        "database": {
            "type": "local",
            "local": {"path": str(tmp_path_factory.mktemp("lakefs-db"))},
        },
        "blockstore": blockstore,
        "installation": {
            "user_name": "admin",
            "access_key_id": "lakefsadmin",
            "secret_access_key": "lakefsadmin",
        },
    }
    dump_yaml(path, config)
    return path


def wait_for_lakefs(
    url: str,
    timeout: int,
    proc: subprocess.Popen[bytes] | None = None,
    log_path: Path | None = None,
) -> None:
    def _read_log() -> str:
        if log_path is not None:
            return log_path.read_text(errors="replace")
        return ""

    while timeout > 0:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(
                f"lakeFS process exited unexpectedly (code {proc.returncode}).\n"
                f"{_read_log()}"
            )
        try:
            urllib.request.urlopen(f"{url}/_health", timeout=3)
            return
        except Exception as e:
            timeout -= 1
            if timeout <= 0:
                raise TimeoutError(
                    f"Failed to start lakeFS server.\n{_read_log()}"
                ) from e
            time.sleep(1)


@pytest.fixture(scope="session")
def lakefs_binary(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Path to the lakeFS binary, downloading it once if it is not already present.

    ``find_or_download_binary`` downloads into the shared venv bin dir
    (``sys.exec_prefix/bin``), which is *not* under ``$HOME`` — so the per-worker HOME
    mock from ``isolate`` gives no protection. Under xdist, parallel workers would
    otherwise extract into that same path at once and macOS SIGKILLs the half-written
    (torn, unsigned) binary. Serialize with a lock in the cross-worker shared base temp
    dir (``getbasetemp().parent``, shared by all workers of a run, unlike the mocked
    HOME): the first worker downloads while the rest wait, then they find it present."""
    from filelock import FileLock
    from lakefs.__main__ import find_or_download_binary

    shared_root = tmp_path_factory.getbasetemp().parent
    with FileLock(str(shared_root / "lakefs-binary.lock")):
        return find_or_download_binary("lakefs")


@pytest.fixture(scope="session")
def server(
    lakefs_binary: str,
    lakefs_config: Path,
    server_addr: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[str]:
    log_path = tmp_path_factory.mktemp("lakefs-log") / "lakefs.log"
    log_file = log_path.open("w")
    proc = subprocess.Popen(
        [lakefs_binary, "run", "--config", lakefs_config],
        stdout=log_file,
        stderr=log_file,
    )
    log_file.close()
    url = f"http://{server_addr}"
    try:
        wait_for_lakefs(url, timeout=60, proc=proc, log_path=log_path)
        yield url
    finally:
        proc.terminate()
        proc.wait()


@dataclass(frozen=True)
class Backend:
    namespace_prefix: str
    to_physical_address: Callable[[str], str] = lambda url: url


# Shared across all repos created in this session to avoid collisions between
# parallel runs.
SESSION_ID = token_urlsafe(16)


@pytest.fixture
def backend(backend_name: str, request: pytest.FixtureRequest) -> Backend:
    if backend_name == "local":
        # Local blockstore uses a path-based namespace relative to the
        # adapter's base directory; no bucket concept. get_url returns a
        # bare absolute path for local remotes, and the lakeFS local
        # adapter re-anchors imported external paths under its own base
        # directory, so the stored address is base dir + source path.
        base = request.getfixturevalue("lakefs_local_data")
        return Backend(
            namespace_prefix=f"local://repos/{SESSION_ID}",
            to_physical_address=f"local://{base}{{0}}".format,
        )
    if backend_name in ("s3", "gs"):
        bucket = request.getfixturevalue(f"{backend_name}_bucket")
        return Backend(namespace_prefix=f"{backend_name}://{bucket}/{SESSION_ID}")
    if backend_name == "azure":
        # The namespace must be host-style to pass lakeFS validation and
        # parse correctly; Azurite's IP-style URL would be rejected. The
        # namespace host is never dialed (see _azure_namespace_base).
        # Imported objects likewise carry the host-style address the
        # exporter handed to lakeFS, not Azurite's IP-style endpoint
        # where the data actually lives.
        namespace_base = _azure_namespace_base(request.getfixturevalue("azurite"))
        container = request.getfixturevalue("azure_container")
        return Backend(
            namespace_prefix=f"{namespace_base}/{container}/{SESSION_ID}",
            to_physical_address=lambda url: (
                f"{namespace_base}/{url.removeprefix('azure://').lstrip('/')}"
            ),
        )
    raise ValueError(f"unsupported backend: {backend_name}")


@pytest.fixture(scope="session", autouse=True)
def lakectl_config(
    server: str,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch_session: pytest.MonkeyPatch,
) -> Path:
    from dvc.utils.serialize import dump_yaml

    path = tmp_path_factory.mktemp("lakectl") / "config.yaml"
    config = {
        "credentials": {
            "access_key_id": "lakefsadmin",
            "secret_access_key": "lakefsadmin",
        },
        "server": {"endpoint_url": server},
    }
    dump_yaml(path, config)
    monkeypatch_session.setenv("LAKECTL_CONFIG_FILE", str(path))
    return path


@pytest.fixture(scope="session")
def lakefs_client(lakectl_config: Path) -> lakefs.Client:
    # Create a fresh Client per backend. ClientConfig reads LAKECTL_CONFIG_FILE at
    # construction time, so this picks up the correct server endpoint and avoids the
    # SDK's class-level client singleton carrying stale storage config across backends.
    return lakefs.Client()  # type: ignore[no-untyped-call]


@pytest.fixture
def remote(make_remote: "RemoteFactory", backend_name: str) -> "TmpDir | Cloud":
    return make_remote(name="upstream", typ=backend_name)


@pytest.fixture
def lakefs_repo(lakefs_client: lakefs.Client, backend: Backend) -> lakefs.Repository:
    repo = "test-repo-" + token_hex(8)
    storage_namespace = f"{backend.namespace_prefix}/{repo}/"
    return lakefs.repository(repo, client=lakefs_client).create(storage_namespace)
