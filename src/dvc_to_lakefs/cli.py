import argparse
from typing import TYPE_CHECKING

from dvc.ui import ui

from dvc_to_lakefs.core import (
    BrokenStageError,
    ExportRefusal,
    LakeFSImportError,
    import_outputs,
    walk,
)
from dvc_to_lakefs.report import _line, _pluralize, report

if TYPE_CHECKING:
    import lakefs
    from dvc.repo import Repo as DvcRepo

    from dvc_to_lakefs.core import ExportOutput


class CLIError(Exception):
    """A user-facing failure: its message is printed in red and the process exits 1."""


def _parse_lakefs_repo_arg(arg: str) -> str:
    # A bare repository name is accepted, as is lakefs://<repo>. Anything carrying
    # a different scheme (lakfs://, s3://, http://, ...) is a typo, not a repo name;
    # reject it early with a clear message instead of letting it reach the lakeFS
    # SDK as a literal repository name and fail obscurely there.
    if "://" in arg and not arg.startswith("lakefs://"):
        scheme = arg.split("://", 1)[0]
        raise argparse.ArgumentTypeError(
            f"unsupported URI scheme {scheme!r} in {arg!r}; "
            "expected lakefs://<repo> or a plain repository name"
        )
    repo = arg
    if arg.startswith("lakefs://"):
        rest = arg.removeprefix("lakefs://")
        repo, _, path = rest.partition("/")
        if not repo:
            raise argparse.ArgumentTypeError(f"missing repository name in URI {arg!r}")
        if path.strip("/"):
            raise argparse.ArgumentTypeError(
                f"URI {arg!r} contains a ref or path; "
                "only lakefs://<repo> is accepted here"
            )
    return repo


def _get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Zero-copy import of DVC-tracked data into lakeFS.",
    )
    parser.add_argument(
        "repo", metavar="dvc-repo", help="path to the DVC git repository"
    )
    parser.add_argument(
        "lakefs_repo",
        metavar="lakefs://<repo>",
        type=_parse_lakefs_repo_arg,
        help="target lakeFS repository URI; a plain repository name is also accepted",
    )
    parser.add_argument(
        "--remote",
        "-r",
        metavar="name",
        help="DVC remote to use (default: the repo's default remote)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="preview the import plan without writing anything to lakeFS",
    )
    parser.add_argument(
        "--branch",
        metavar="branch",
        action="append",
        help="Git branch to export; repeat to export multiple branches "
        "(default: current branch)",
    )
    parser.add_argument(
        "--skip-broken-revs",
        action="store_true",
        help="when exporting multiple branches, skip any branch that fails "
        "instead of aborting",
    )
    parser.add_argument(
        "--skip-broken-stages",
        action="store_true",
        help="skip unreadable dvc.yaml/dvc.lock/.dvc files and export the rest "
        "of the repo",
    )
    parser.add_argument(
        "--show-files",
        action="store_true",
        help="expand directory outputs to list every file instead of a single "
        "summary line",
    )
    return parser


def _collect(
    repo: "DvcRepo",
    *,
    remote: str | None,
    storage_namespace: str,
    skip_broken_stages: bool,
) -> tuple[list["ExportOutput"], list[ExportRefusal]]:
    outputs: list[ExportOutput] = []
    refusals: list[ExportRefusal] = []
    for node in walk(
        repo,
        remote_name=remote,
        lakefs_storage_namespace=storage_namespace,
        skip_broken_stages=skip_broken_stages,
    ):
        if isinstance(node, ExportRefusal):
            refusals.append(node)
        else:
            outputs.append(node)
    return outputs, refusals


def _export_rev(  # noqa: PLR0913
    repo: "DvcRepo",
    lakefs_repository: "lakefs.Repository",
    branch: str,
    /,
    remote: str | None,
    storage_namespace: str,
    dry_run: bool,
    show_files: bool,
    skip_broken_stages: bool,
) -> None:
    with repo.switch(branch) as rev:
        outputs, refusals = _collect(
            repo,
            remote=remote,
            storage_namespace=storage_namespace,
            skip_broken_stages=skip_broken_stages,
        )

        action = "Previewing import into" if dry_run else "Importing into"
        ui.rich_print(
            f"[yellow]{action} [cyan]{branch}[/cyan] from git commit "
            f"[magenta]{rev[:7]}[/magenta]"
        )
        report(outputs, refusals, show_files=show_files, dry_run=dry_run)
        if dry_run:
            return

        commit = repo.scm.resolve_commit(rev)
        commit_message = commit.message
        metadata = {"git_sha": commit.hexsha}
        result = import_outputs(
            lakefs_repository,
            branch,
            outputs,
            commit_message=commit_message,
            metadata=metadata,
        )
        ui.rich_print(
            f"[green]Imported {_pluralize(result.ingested, 'object')}[/green] "
            f"to [cyan]lakefs://{lakefs_repository.id}/{branch}[/cyan] "
            f"(commit [magenta]{result.commit_id[:12]}[/magenta])"
        )


def _export_branches(  # noqa: PLR0913
    repo: "DvcRepo",
    lakefs_repository: "lakefs.Repository",
    branches: list[str],
    *,
    remote: str | None,
    storage_namespace: str,
    dry_run: bool,
    show_files: bool,
    skip_broken_stages: bool,
    skip_broken_revs: bool,
) -> None:
    for n, branch in enumerate(branches):
        if n:
            ui.rich_print("")  # blank line to separate branch sections

        try:
            _export_rev(
                repo,
                lakefs_repository,
                branch,
                remote=remote,
                storage_namespace=storage_namespace,
                dry_run=dry_run,
                show_files=show_files,
                skip_broken_stages=skip_broken_stages,
            )
        except BrokenStageError as exc:
            raise CLIError(
                f"branch {branch!r}: {exc}, use --skip-broken-stages to skip"
            ) from exc
        except LakeFSImportError as exc:
            # if it's a server error, abort instead of supporting --skip-broken-revs
            raise CLIError(f"branch {branch!r}: {exc}") from exc
        except Exception as exc:
            # A whole revision can be unprocessable: the commit isn't a DVC repo,
            # the rev won't resolve, or its remote is missing/incompatible. Abort
            # unless the user opted to skip such revisions. (Import failures raise
            # LakeFSImportError, handled above, and always abort regardless.)
            if not skip_broken_revs:
                raise CLIError(f"branch {branch!r}: {exc}") from exc
            # Surface the skipped branch as its own block so the preview is
            # complete (no resolved rev — the failure may be the switch itself).
            _line(1, f"[red]Skipped branch [cyan]{branch}[/cyan]:[/red] {exc}")


def _run(argv: list[str] | None) -> int:
    parser = _get_parser()
    args = parser.parse_args(argv)
    ui.enable()

    from dvc.repo import Repo
    from dvc.scm import NoSCM

    try:
        repo = Repo(args.repo)
    except Exception as exc:
        raise CLIError(f"failed to open DVC repo at {args.repo!r}: {exc}") from exc
    if isinstance(repo.scm, NoSCM):
        raise CLIError(
            f"the DVC project at {args.repo!r} was initialized without Git "
            "(dvc init --no-scm), so it has no branches or commits to export from. "
            "Only Git-backed DVC repositories are supported."
        )

    if args.branch:
        # --branch is append; collapse exact repeats (order-preserving) so
        # `--branch main --branch main` doesn't import and commit main twice.
        branches = list(dict.fromkeys(args.branch))
    else:
        try:
            branches = [repo.scm.active_branch()]
        except Exception as exc:
            # detached HEAD, unborn branch, etc. — no current branch to default to
            raise CLIError(
                f"could not determine the current branch: {exc}; "
                "pass --branch explicitly"
            ) from exc
    if "workspace" in branches:
        raise CLIError(
            "'workspace' is a reserved DVC keyword and cannot be used as a branch name."
        )

    import lakefs

    try:
        lakefs_repository = lakefs.repository(args.lakefs_repo)
        # The lakeFS blockstore is fixed per repository, independent of the DVC branch.
        storage_namespace = lakefs_repository.properties.storage_namespace
    except Exception as exc:
        raise CLIError(
            f"failed to read lakeFS repository {args.lakefs_repo!r}: {exc}"
        ) from exc

    _export_branches(
        repo,
        lakefs_repository,
        branches,
        remote=args.remote,
        storage_namespace=storage_namespace,
        dry_run=args.dry_run,
        show_files=args.show_files,
        skip_broken_stages=args.skip_broken_stages,
        skip_broken_revs=args.skip_broken_revs,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point: run the export, turning a CLIError into a red message + exit 1."""
    try:
        return _run(argv)
    except CLIError as exc:
        ui.rich_print(f"[red]{exc}[/red]", stderr=True)
        return 1
