from os import path
from typing import TYPE_CHECKING, Union

import lakefs
import pytest
from dvc.repo import Repo as DvcRepo
from dvc.scm import Git
from dvc.testing.tmp_dir import TmpDir, make_subrepo
from lakefs import ObjectInfo

from dvc_to_lakefs.cli import main

from .utils import lakefs_cat, lakefs_head, lakefs_log

if TYPE_CHECKING:
    from dvc.testing.cloud import Cloud

    from .conftest import Backend, RemoteFactory

Remote = Union[TmpDir, "Cloud"]


@pytest.mark.parametrize("clear_cache", [True, False])
def test_export(
    tmp_dir: TmpDir,
    dvc: DvcRepo,
    remote: Remote,
    scm: Git,
    lakefs_repo: lakefs.Repository,
    clear_cache: bool,
) -> None:
    contents = {
        "data": {"file": b"content", "file2": b"more content"},
        "dir": {"file": b"even more content", "subdir": {"file": b"subdir content"}},
        "lorem": b"ipsum",
    }
    stages = tmp_dir.dvc_gen(contents, commit="add data")
    assert len(stages) == 3
    dvc.push()
    if clear_cache:
        dvc.cache.local.clear()

    assert main([str(tmp_dir), lakefs_repo.id]) == 0
    head = lakefs_head(lakefs_repo, "main")
    assert head["message"] == "add data"
    assert head["metadata"]
    assert head["metadata"]["git_sha"] == scm.get_rev()
    assert lakefs_cat(lakefs_repo, head["id"]) == contents


def test_export_dir_from_non_default_remote_with_cleared_cache(
    tmp_dir: TmpDir,
    dvc: DvcRepo,
    make_remote: "RemoteFactory",
    scm: Git,
    lakefs_repo: lakefs.Repository,
    backend_name: str,
) -> None:
    """Export a directory via a non-default --remote with no local cache.

    The default remote is left empty; the data lives only on 'staging'. With the
    local cache cleared, the directory's .dir manifest must be fetched from the
    chosen --remote (via get_dir_cache(odb=...)), not DVC's default remote --
    otherwise the directory is wrongly refused as having no hash info and nothing
    is imported.
    """
    make_remote(name="default_remote", typ=backend_name, default=True)
    make_remote(name="staging", typ=backend_name, default=False)

    contents = {"dir": {"a.txt": b"a", "subdir": {"b.txt": b"b"}}}
    tmp_dir.dvc_gen(contents, commit="add dir")
    dvc.push(remote="staging")
    dvc.cache.local.clear()

    assert main([str(tmp_dir), lakefs_repo.id, "--remote", "staging"]) == 0
    assert lakefs_cat(lakefs_repo, "main") == contents


def test_dry_run(
    tmp_dir: TmpDir,
    dvc: DvcRepo,
    remote: Remote,
    scm: Git,
    lakefs_repo: lakefs.Repository,
) -> None:
    tmp_dir.dvc_gen({"data.csv": b"content"}, commit="add data")
    dvc.push()

    initial_commit_count = len(lakefs_log(lakefs_repo, "main"))

    assert main([str(tmp_dir), lakefs_repo.id, "--dry-run"]) == 0

    assert len(lakefs_log(lakefs_repo, "main")) == initial_commit_count


def test_export_multiple_branches(
    tmp_dir: TmpDir,
    dvc: DvcRepo,
    remote: Remote,
    scm: Git,
    lakefs_repo: lakefs.Repository,
) -> None:
    tmp_dir.dvc_gen({"file1.txt": b"content1"}, commit="add file1")
    dvc.push()
    main_sha = scm.get_rev()

    scm.checkout("feature", create_new=True)
    tmp_dir.dvc_gen({"file2.txt": b"content2"}, commit="add file2")
    dvc.push()
    feature_sha = scm.get_rev()
    scm.checkout("main")

    assert (
        main([str(tmp_dir), lakefs_repo.id, "--branch", "main", "--branch", "feature"])
        == 0
    )

    main_head = lakefs_head(lakefs_repo, "main")
    assert main_head["metadata"]
    assert main_head["metadata"]["git_sha"] == main_sha
    assert lakefs_cat(lakefs_repo, "main") == {"file1.txt": b"content1"}

    feature_head = lakefs_head(lakefs_repo, "feature")
    assert feature_head["metadata"]
    assert feature_head["metadata"]["git_sha"] == feature_sha
    assert lakefs_cat(lakefs_repo, "feature") == {
        "file1.txt": b"content1",
        "file2.txt": b"content2",
    }


def test_skip_broken_stages(
    tmp_dir: TmpDir,
    dvc: DvcRepo,
    remote: Remote,
    scm: Git,
    lakefs_repo: lakefs.Repository,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tmp_dir.dvc_gen({"good.txt": b"good content"}, commit="add good output")
    dvc.push()

    tmp_dir.scm_gen(
        {"broken_pipeline/dvc.yaml": "stages:\n  bad: [invalid yaml"},
        commit="add corrupt stage",
    )

    assert main([str(tmp_dir), lakefs_repo.id]) == 1
    assert path.join("broken_pipeline", "dvc.yaml") in capsys.readouterr().err

    assert main([str(tmp_dir), lakefs_repo.id, "--skip-broken-stages"]) == 0
    assert path.join("broken_pipeline", "dvc.yaml") in capsys.readouterr().out
    assert lakefs_cat(lakefs_repo, "main") == {"good.txt": b"good content"}


def test_skip_broken_revs(
    tmp_dir: TmpDir,
    dvc: DvcRepo,
    remote: Remote,
    scm: Git,
    lakefs_repo: lakefs.Repository,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tmp_dir.dvc_gen({"data.txt": b"content"}, commit="add data")
    dvc.push()

    assert (
        main([str(tmp_dir), lakefs_repo.id, "--branch", "nonexistent-branch-xyz"]) == 1
    )
    assert "nonexistent-branch-xyz" in capsys.readouterr().err

    assert (
        main(
            [
                str(tmp_dir),
                lakefs_repo.id,
                "--branch",
                "nonexistent-branch-xyz",
                "--skip-broken-revs",
            ]
        )
        == 0
    )
    assert "Skipped branch" in capsys.readouterr().out


def test_export_with_skipped_output(
    tmp_dir: TmpDir,
    dvc: DvcRepo,
    remote: Remote,
    scm: Git,
    lakefs_repo: lakefs.Repository,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tmp_dir.dvc_gen({"data.txt": b"content"}, commit="add data")
    dvc.push()

    # A stage in dvc.yaml that has never been run produces no dvc.lock, so its
    # outputs have no hash info and are refused as NO_HASH_INFO; other outputs
    # in the same repo still export successfully.
    tmp_dir.scm_gen(
        {
            "pipeline/dvc.yaml": (
                "stages:\n  generate:\n    cmd: echo hello\n"
                "    outs:\n    - output.txt\n"
            )
        },
        commit="add unrun stage",
    )

    assert main([str(tmp_dir), lakefs_repo.id]) == 0

    out, _ = capsys.readouterr()
    assert "output.txt" in out
    assert "Skipped" in out

    head = lakefs_head(lakefs_repo, "main")
    assert head["message"] == "add unrun stage"
    assert lakefs_cat(lakefs_repo, head["id"]) == {"data.txt": b"content"}


@pytest.mark.xfail(
    reason="lakeFS import is additive; re-exporting does not remove files "
    "deleted from DVC",
    strict=True,
)
def test_export_removes_deleted_file(
    tmp_dir: TmpDir,
    dvc: DvcRepo,
    remote: Remote,
    scm: Git,
    lakefs_repo: lakefs.Repository,
) -> None:
    tmp_dir.dvc_gen({"keep.txt": b"keep", "stale.txt": b"stale"}, commit="initial")
    dvc.push()
    main([str(tmp_dir), lakefs_repo.id])

    (tmp_dir / "stale.txt.dvc").unlink()
    scm.add_commit(["stale.txt.dvc"], "remove stale.txt")

    main([str(tmp_dir), lakefs_repo.id])

    assert lakefs_cat(lakefs_repo, "main") == {"keep.txt": b"keep"}


def test_no_remote_configured(
    tmp_dir: TmpDir, dvc: DvcRepo, scm: Git, lakefs_repo: lakefs.Repository
) -> None:
    tmp_dir.dvc_gen({"data.txt": b"content"}, commit="add data")

    assert main([str(tmp_dir), lakefs_repo.id]) == 1


def test_invalid_dvc_repo(tmp_path: TmpDir) -> None:
    assert main([str(tmp_path / "no-such-dir"), "dummy-repo"]) == 1


def test_invalid_lakefs_repo(tmp_dir: TmpDir, dvc: DvcRepo) -> None:
    assert main([str(tmp_dir), "nonexistent-lakefs-repo-xyz"]) == 1


def test_physical_addresses(
    tmp_dir: TmpDir,
    dvc: DvcRepo,
    remote: Remote,
    scm: Git,
    lakefs_repo: lakefs.Repository,
    backend: "Backend",
) -> None:
    from dvc.api import get_url

    contents = {
        "data": {"file": b"content", "file2": b"more content"},
        "dir": {"file": b"even more content", "subdir": {"file": b"subdir content"}},
        "lorem": b"ipsum",
    }
    tmp_dir.dvc_gen(contents, commit="add data")
    dvc.push()
    assert main([str(tmp_dir), lakefs_repo.id]) == 0

    objects = list(lakefs_repo.branch("main").objects())
    assert objects
    for obj in objects:
        assert isinstance(obj, ObjectInfo)
        assert obj.physical_address == backend.to_physical_address(
            get_url(obj.path, repo=str(tmp_dir))
        )


def test_export_multiple_commits(
    tmp_dir: TmpDir,
    dvc: DvcRepo,
    remote: Remote,
    scm: Git,
    lakefs_repo: lakefs.Repository,
) -> None:
    tmp_dir.dvc_gen({"data.txt": b"v1"}, commit="v1")
    dvc.push()
    assert main([str(tmp_dir), lakefs_repo.id]) == 0

    tmp_dir.dvc_gen({"data.txt": b"v2"}, commit="v2")
    dvc.push()
    assert main([str(tmp_dir), lakefs_repo.id]) == 0

    log = lakefs_log(lakefs_repo, "main")
    messages = [c["message"] for c in log]
    assert messages[0] == "v2"
    assert "v1" in messages
    assert lakefs_cat(lakefs_repo, log[0]["id"]) == {"data.txt": b"v2"}


def test_subrepo_outputs_excluded_from_parent_export(
    tmp_dir: TmpDir,
    dvc: DvcRepo,
    remote: Remote,
    scm: Git,
    lakefs_repo: lakefs.Repository,
) -> None:
    """Outputs that live in a DVC subrepo must not appear in the parent repo export.

    DVC does not recurse into subrepos when building the parent's index, so
    walk() on the parent naturally excludes them.  This test pins that contract.
    """
    tmp_dir.dvc_gen({"parent.txt": b"parent content"}, commit="add parent data")
    dvc.push()

    sub = tmp_dir / "sub"
    make_subrepo(sub, scm, config=remote.config)
    with sub.chdir():
        sub.dvc_gen({"sub.txt": b"sub content"}, commit="sub: add data")
    sub.dvc.push()

    assert main([str(tmp_dir), lakefs_repo.id]) == 0

    exported = lakefs_cat(lakefs_repo, "main")
    assert exported == {"parent.txt": b"parent content"}


def test_export_subrepo_directly(
    tmp_dir: TmpDir,
    dvc: DvcRepo,
    remote: Remote,
    scm: Git,
    lakefs_repo: lakefs.Repository,
) -> None:
    """The tool works when pointed directly at a DVC subrepo (dvc init --subdir)."""
    sub = tmp_dir / "sub"
    make_subrepo(sub, scm, config=remote.config)
    with sub.chdir():
        sub.dvc_gen({"data.txt": b"subrepo content"}, commit="sub: add data")
    sub.dvc.push()

    assert main([str(sub), lakefs_repo.id]) == 0

    exported = lakefs_cat(lakefs_repo, "main")
    assert exported == {"data.txt": b"subrepo content"}
