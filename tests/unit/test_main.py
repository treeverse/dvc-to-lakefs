import argparse

import pytest
from dvc.repo import Repo as DvcRepo
from dvc.scm import Git
from dvc.testing.tmp_dir import TmpDir

from dvc_to_lakefs.cli import _parse_lakefs_repo_arg, main


def test_noscm_exits_with_error(
    tmp_dir: TmpDir, dvc: DvcRepo, capsys: pytest.CaptureFixture[str]
) -> None:
    tmp_dir.dvc_gen({"data.txt": "content"})
    # Exits before touching lakeFS, so an invalid repo name is fine here.
    assert main([str(tmp_dir), "irrelevant-repo"]) == 1
    assert "without Git" in capsys.readouterr().err


@pytest.mark.parametrize(
    "arg, expected",
    [
        ("myrepo", "myrepo"),
        ("lakefs://myrepo", "myrepo"),
        ("lakefs://myrepo/", "myrepo"),
        ("my-repo-123", "my-repo-123"),
        ("lakefs://my-repo-123", "my-repo-123"),
    ],
)
def test_parse_lakefs_repo_arg_valid(arg: str, expected: str) -> None:
    assert _parse_lakefs_repo_arg(arg) == expected


@pytest.mark.parametrize(
    "arg,msg",
    [
        ("lakefs://", "missing repository name in URI"),
        ("lakefs://myrepo/branch", "contains a ref or path"),
        ("lakefs://myrepo/branch/path", "contains a ref or path"),
        ("lakfs://myrepo", "unsupported URI scheme 'lakfs'"),
        ("s3://myrepo", "unsupported URI scheme 's3'"),
        ("http://myrepo", "unsupported URI scheme 'http'"),
    ],
)
def test_parse_lakefs_repo_arg_invalid(arg: str, msg: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError, match=msg):
        _parse_lakefs_repo_arg(arg)


def test_workspace_branch_rejected(
    tmp_dir: TmpDir, scm: Git, dvc: DvcRepo, capsys: pytest.CaptureFixture[str]
) -> None:
    tmp_dir.dvc_gen({"data.txt": "content"})
    assert main([str(tmp_dir), "irrelevant-repo", "--branch", "workspace"]) == 1
    assert "reserved" in capsys.readouterr().err
