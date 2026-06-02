import os
import sys
from collections.abc import Iterator

import pytest
from dvc.testing.fixtures import (
    dvc,
    make_cloud,
    make_local,
    make_remote,
    make_tmp_dir,
    scm,
    tmp_dir,
)

__all__ = [
    "dvc",
    "make_cloud",
    "make_local",
    "make_remote",
    "make_tmp_dir",
    "scm",
    "tmp_dir",
]


@pytest.fixture(scope="session")
def monkeypatch_session() -> Iterator[pytest.MonkeyPatch]:
    m = pytest.MonkeyPatch()
    yield m
    m.undo()


@pytest.fixture(scope="session", autouse=True)
def isolate(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch_session: pytest.MonkeyPatch
) -> None:
    """Redirect all DVC and git config to a temporary home directory so tests
    never touch the developer's real config files.

    Lives in the root conftest (not tests/e2e/) so it is autouse for the entire
    suite — the unit tests in tests/unit also construct real dvc.repo.Repo objects
    and must not read the developer's real ~/.config/dvc or ~/.gitconfig."""
    from dvc import env as dvc_env

    path = tmp_path_factory.mktemp("isolate")
    home_dir = path / "home"
    home_dir.mkdir()

    if sys.platform == "win32":
        home_drive, home_path = os.path.splitdrive(home_dir)
        monkeypatch_session.setenv("USERPROFILE", str(home_dir))
        monkeypatch_session.setenv("HOMEDRIVE", home_drive)
        monkeypatch_session.setenv("HOMEPATH", home_path)
        for env_var, sub_path in (("APPDATA", "Roaming"), ("LOCALAPPDATA", "Local")):
            p = home_dir / "AppData" / sub_path
            p.mkdir(parents=True)
            monkeypatch_session.setenv(env_var, os.fspath(p))
    else:
        monkeypatch_session.setenv("HOME", str(home_dir))
        monkeypatch_session.setenv("XDG_CONFIG_HOME", str(home_dir / ".config"))

    monkeypatch_session.setenv("GIT_CONFIG_NOSYSTEM", "1")
    (home_dir / ".gitconfig").write_bytes(
        b"[user]\nname=DVC Tester\nemail=dvctester@example.com\n"
        b"[init]\ndefaultBranch=main\n",
    )
    import pygit2
    from pygit2.enums import ConfigLevel

    # pygit2's typed `SearchPathList.__setitem__` declares `ConfigLevel` for
    # the key, but `GIT_CONFIG_LEVEL_GLOBAL` is exported as a plain `int`.
    # Stuffing the value past the type checker is the path of least resistance.
    _search_path = pygit2.settings.search_path
    _search_path[ConfigLevel.GLOBAL] = str(home_dir)

    monkeypatch_session.setenv(
        dvc_env.DVC_SYSTEM_CONFIG_DIR, os.fspath(path / "system")
    )
    monkeypatch_session.setenv(
        dvc_env.DVC_GLOBAL_CONFIG_DIR, os.fspath(path / "global")
    )
    monkeypatch_session.setenv(
        dvc_env.DVC_SITE_CACHE_DIR, os.fspath(path / "site_cache_dir")
    )


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--azurite-host", action="store", help="Host running azurite.")


# Canonical list of lakeFS blockstore backends the e2e suite is parametrized over.
# Lives in the root conftest (not tests/e2e/conftest.py) because the worker-count
# hook below is resolved at startup, before subdir conftests are imported. The
# backend_name fixture in tests/e2e/conftest.py imports this as its single source.
BACKENDS = ("local", "s3", "gs", "azure")


def pytest_xdist_auto_num_workers(config: pytest.Config) -> int:
    """Make ``-n auto``/``-n logical`` use one worker per backend.

    The suite is I/O-bound — each worker mostly waits on its own lakeFS server and
    emulator — so matching workers to backends maximizes parallelism regardless of
    CPU count (xdist's default keys off physical cores, which over- or
    under-provisions for this workload). Only consulted for auto/logical; an
    explicit ``-n <N>`` still wins."""
    return len(BACKENDS)
