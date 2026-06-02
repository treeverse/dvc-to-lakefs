import logging
import posixpath
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from typing import TYPE_CHECKING

from typing_extensions import override

if TYPE_CHECKING:
    import lakefs
    from dvc.data_cloud import Remote
    from dvc.output import Output
    from dvc.repo import Repo
    from dvc.stage import Stage
    from dvc_data.hashfile.db import HashFileDB
    from dvc_objects.fs.base import FileSystem

logger = logging.getLogger(__name__)

# Polling cadence for lakeFS import completion. ``None`` keeps the SDK default
# (a flat 2s, sleeping before the first check) — fine in production. Tests patch
# this to a small value since their imports finish in well under a second.
IMPORT_POLL_INTERVAL: timedelta | None = None


class BrokenStageError(Exception):
    pass


class LakeFSImportError(Exception):
    pass


class Severity(Enum):
    """How a refusal is reported.

    REFUSE — the export aborts; the output ought to have been exportable
    but isn't (scheme mismatch, missing remote, not pushed, ...).

    SKIP   — the output is legitimately out of scope (repo-imports,
    directory outputs in Phase 1) and surfaced as a warning only.

    BROKEN — the output/stage could not be read at all (corrupt or partial
    dvc.yaml/dvc.lock) and was tolerated via --skip-broken-*; surfaced
    distinctly because, unlike SKIP, it usually points at something to fix.
    """

    REFUSE = "refuse"
    SKIP = "skip"
    BROKEN = "broken"


class RefusalReason(Enum):
    NO_HASH_INFO = ("no_hash_info", Severity.SKIP, "output has no hash info")
    NO_CACHE_OUTPUT = (
        "no_cache_output",
        Severity.SKIP,
        "output is marked to not be cached",
    )
    REPO_IMPORT_NOT_SUPPORTED = (
        "repo_import_not_supported",
        Severity.SKIP,
        "repository imports are not supported",
    )
    URL_IMPORT_NOT_SUPPORTED = (
        "url_import_not_supported",
        Severity.SKIP,
        "URL imports are not supported",
    )
    CUSTOM_REMOTE_NOT_SUPPORTED = (
        "custom_remote_output",
        Severity.SKIP,
        "output uses a custom per-output remote",
    )
    NO_PUSH_OUTPUT = (
        "no_push_output",
        Severity.SKIP,
        "output is marked as not pushable",
    )
    EXTERNAL_OUTPUT_NOT_SUPPORTED = (
        "external_output_not_supported",
        Severity.SKIP,
        "external outputs are not supported",
    )
    VERSIONED_OUTPUT_NOT_SUPPORTED = (
        "versioned_output_not_supported",
        Severity.SKIP,
        "output was pushed to a version-aware remote (cloud-versioned)",
    )
    PATH_OUTSIDE_REPO = (
        "path_outside_repo",
        Severity.SKIP,
        "output path is outside the repository",
    )
    STAGE_LOAD_FAILED = (
        "stage_load_failed",
        Severity.BROKEN,
        "stage file could not be loaded",
    )
    MANIFEST_READ_FAILED = (
        "manifest_read_failed",
        Severity.SKIP,
        "could not read the directory manifest from the remote; "
        "check the remote is reachable and credentials are valid",
    )
    MISSING_MANIFEST = (
        "missing_manifest",
        Severity.SKIP,
        "directory manifest is missing from both the cache and the remote",
    )

    def __init__(self, code: str, severity: Severity, description: str) -> None:
        self.code = code
        self.severity = severity
        self.description = description

    @override
    def __str__(self) -> str:
        return self.code


@dataclass(frozen=True, slots=True)
class ExportItem:
    """One object to import into lakeFS."""

    repo_path: str  # destination path under the lakeFS branch
    physical_url: str  # source URL on the blockstore (scheme://bucket/key)


@dataclass(frozen=True, slots=True)
class ExportOutput:
    """One DVC output in the export plan.

    A file output is one object; a directory output is one object per file it
    contains. ``files`` holds the per-object items once expanded."""

    repo_path: str
    is_dir: bool
    files: tuple[ExportItem, ...] = ()


@dataclass(frozen=True, slots=True)
class ExportRefusal:
    output: str
    reason: RefusalReason
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ImportResult:
    """Outcome of a completed lakeFS import."""

    ingested: int
    commit_id: str


WalkResult = ExportOutput | ExportRefusal

SUPPORTED_SCHEMES = {
    "s3",
    "gs",
    "azure",
    "local",
}


def _azure_blob_host(fs: "FileSystem") -> str:
    """Return the canonical Azure blob host URL for a DVC azure filesystem.

    adlfs sets account_url when account_name is known at construction time
    (account_name + key/SAS/credential, including sovereign-cloud account_host).
    For connection_string-only auth it is absent, so we derive the canonical
    blob URL from AccountName + EndpointSuffix in the connection string.
    BlobEndpoint (present in Azurite strings) is a traffic redirect to a
    local emulator; lakeFS needs the canonical blob URL, not that address.
    UseDevelopmentStorage=true is the Azurite shorthand (no AccountName key);
    the devstore account name is fixed at "devstoreaccount1".
    """
    import adlfs

    abfs = fs.fs  # adlfs.AzureBlobFileSystem
    assert isinstance(abfs, adlfs.AzureBlobFileSystem)

    account_url: str | None = getattr(abfs, "account_url", None)
    if account_url:
        return account_url.rstrip("/")

    conn_str = abfs.connection_string or ""
    parts = {
        k: v
        for seg in conn_str.split(";")
        if "=" in seg
        for k, _, v in [seg.partition("=")]
    }
    if parts.get("UseDevelopmentStorage") == "true":
        return "https://devstoreaccount1.blob.core.windows.net"
    account = parts.get("AccountName")
    if not account:
        raise Exception(
            f"cannot determine the Azure storage account for remote {fs!r}; "
            "set account_name or include AccountName in the connection string"
        )
    suffix = parts.get("EndpointSuffix", "core.windows.net")
    return f"https://{account}.blob.{suffix}"


def _uri_builder(fs: "FileSystem") -> Callable[[str], str]:
    """Return a per-path closure mapping a DVC remote path to a lakeFS import URI.

    lakeFS imports objects by reference from its own blockstore, so the URI must
    be in the form lakeFS expects per backend:
      s3    -> s3://bucket/key                                   (passthrough)
      gs    -> gs://bucket/key                                   (passthrough)
      azure -> https://<account>.blob.core.windows.net/<container>/<key>
      local -> local:///absolute/path/to/file

    The per-filesystem bits (notably the azure host, which parses credentials)
    are resolved once, here; the returned closure does only per-path work. A
    directory output calls this once and reuses the closure for every file,
    rather than re-deriving the constant parts per object.
    """
    protocol = fs.protocol
    if protocol in ("s3", "gs"):

        def build(fs_path: str) -> str:
            url = fs.unstrip_protocol(fs_path)
            assert isinstance(url, str)
            return url

        return build
    if protocol == "local":
        # unstrip_protocol returns a bare absolute path for local filesystems.
        return lambda fs_path: "local://" + fs_path
    if protocol == "azure":
        # DVC yields azure://<container>/<key>; the account lives in the remote
        # config/credentials, not the path, so derive the host once up front.
        host = _azure_blob_host(fs)

        def build_azure(fs_path: str) -> str:
            url = fs.unstrip_protocol(fs_path)
            assert isinstance(url, str)
            return host + "/" + url.removeprefix("azure://").lstrip("/")

        return build_azure
    raise Exception(f"unsupported remote scheme {protocol!r}")


def _blockstore_type(storage_namespace: str) -> str:
    """Canonical blockstore type backing a lakeFS storage namespace."""
    scheme = storage_namespace.split("://", 1)[0]
    if scheme in ("s3", "gs", "local"):
        return scheme
    # Azure blob endpoints are https and contain blob.core.<cloud> (windows.net,
    # usgovcloudapi.net, chinacloudapi.cn, ...).
    if scheme in ("http", "https") and "blob.core." in storage_namespace:
        return "azure"
    raise Exception(f"unsupported lakeFS storage namespace {storage_namespace!r}")


def _verify_remote(repo: "Repo", remote_name: str | None) -> "Remote":
    # Check that we have remote configured, otherwise raises NoRemoteError
    remote = repo.cloud.get_remote(remote_name)
    # do not support worktree remotes
    if remote.worktree:
        raise Exception(
            f"remote {remote_name!r} is a worktree remote; "
            "worktree remotes are not supported"
        )
    # Do not support version-aware remotes
    if remote.config.get("version_aware", False):
        raise Exception(
            f"remote {remote_name!r} is version-aware; "
            "version-aware remotes are not supported"
        )
    # Only support select remotes for now.
    if remote.fs.protocol not in SUPPORTED_SCHEMES:
        raise Exception(
            f"remote {remote.name!r} has unsupported scheme {remote.fs.protocol!r}; "
            f"only {SUPPORTED_SCHEMES!r} remotes are supported"
        )
    return remote


def _verify_blockstore_match(remote: "Remote", storage_namespace: str) -> None:
    """Refuse when the DVC remote and lakeFS repo live on different blockstores.

    lakeFS imports objects by reference, so a source object must already sit on
    the repository's own blockstore; it cannot import across clouds.
    """
    lakefs_type = _blockstore_type(storage_namespace)
    if remote.fs.protocol != lakefs_type:
        raise Exception(
            f"DVC remote {remote.name!r} uses {remote.fs.protocol!r} but the lakeFS "
            f"repository is backed by {lakefs_type!r} ({storage_namespace!r})"
        )


def _refuse(output: str, reason: RefusalReason, detail: str = "") -> ExportRefusal:
    return ExportRefusal(output=output, reason=reason, detail=detail)


def _qualify(out: "Output") -> "ExportRefusal | None":  # noqa: PLR0911
    name = str(out)
    stage: Stage = out.stage
    # Skip repo imports
    if stage.is_repo_import:
        return _refuse(
            name,
            RefusalReason.REPO_IMPORT_NOT_SUPPORTED,
            f"imports from {stage.deps[0]}",
        )
    # Skip URL imports
    if stage.is_import and not stage.is_db_import:
        return _refuse(
            name,
            RefusalReason.URL_IMPORT_NOT_SUPPORTED,
            f"imports from {stage.deps[0]}",
        )
    # DVC outputs without hashes cannot be exported.
    if not out.hash_info or not out.hash_info.value:
        return _refuse(name, RefusalReason.NO_HASH_INFO)
    # DVC outputs could have `cache: false`.
    if not out.use_cache:
        return _refuse(name, RefusalReason.NO_CACHE_OUTPUT)
    # Cloud-versioned outputs (pushed to a version-aware remote) record a per-file
    # version_id instead of living at a content-addressed cache path. DVC sets
    # version_aware when ``meta.version_id or files`` is present (dvc.output.Output);
    # mirror that test. _build_output would otherwise map them via odb.oid_to_path()
    # to objects that do not exist on the blockstore, creating dangling lakeFS
    # references. (A version-aware *remote* is already rejected in _verify_remote,
    # but a versioned output can be reached through a different selected remote.)
    if (
        out.meta is not None and out.meta.version_id is not None
    ) or out.files is not None:
        return _refuse(name, RefusalReason.VERSIONED_OUTPUT_NOT_SUPPORTED)
    # Skip external outputs
    if out.fs.protocol != "local":
        return _refuse(
            name,
            RefusalReason.EXTERNAL_OUTPUT_NOT_SUPPORTED,
            f"output has non-local fs protocol {out.fs.protocol!r}",
        )
    # Skip out-of-repo outputs
    if not out.is_in_repo:
        return _refuse(name, RefusalReason.PATH_OUTSIDE_REPO)
    # DVC can be configured per-output to push to a different remote (and never
    # to any other place). skip those for now.
    if out.remote is not None:
        return _refuse(
            name,
            RefusalReason.CUSTOM_REMOTE_NOT_SUPPORTED,
            f"output uses custom remote {out.remote}",
        )
    # DVC outputs can be marked as "not pushable"; skip those.
    if not out.can_push:
        return _refuse(name, RefusalReason.NO_PUSH_OUTPUT)
    return None


def _build_output(
    out: "Output",
    repo: "Repo",
    odb: "HashFileDB",
    to_uri: Callable[[str], str],
) -> "ExportOutput | ExportRefusal":
    # ``to_uri`` is resolved once per odb by the caller and shared across every
    # output (and every file within a directory), so the constant per-backend
    # work (e.g. deriving the azure host) never repeats per object.
    repo_path = posixpath.sep.join(repo.fs.relparts(out.fs_path, repo.root_dir))
    assert out.hash_info is not None
    assert out.hash_info.value
    if not out.is_dir_checksum:
        item = ExportItem(repo_path, to_uri(odb.oid_to_path(out.hash_info.value)))
        return ExportOutput(repo_path, is_dir=False, files=(item,))

    obj = out.get_obj()
    if obj is None:
        try:
            result = repo.cloud.pull([out.hash_info], odb=odb)
        except Exception as exc:  # noqa: BLE001
            return _refuse(
                str(out),
                RefusalReason.MANIFEST_READ_FAILED,
                f"{type(exc).__name__}: {exc}",
            )
        if out.hash_info in result.failed:
            return _refuse(
                str(out), RefusalReason.MANIFEST_READ_FAILED, "manifest download failed"
            )
        obj = out.get_obj()
        if obj is None:
            return _refuse(str(out), RefusalReason.MISSING_MANIFEST)

    from dvc_data.hashfile.tree import Tree

    assert isinstance(obj, Tree)
    files = []
    for key, _, hi in obj:
        assert hi is not None
        assert hi.value
        files.append(
            ExportItem(
                posixpath.sep.join((repo_path, *key)),
                to_uri(odb.oid_to_path(hi.value)),
            )
        )
    return ExportOutput(repo_path, is_dir=True, files=tuple(files))


def walk(
    repo: "Repo",
    *,
    remote_name: str | None = None,
    lakefs_storage_namespace: str,
    skip_broken_stages: bool = False,
) -> Iterator["WalkResult"]:
    """Yield one ExportOutput or ExportRefusal per DVC output.

    repo.index lazily loads every dvc.yaml/dvc.lock; by default a single corrupt
    or partial file aborts the whole collection. With ``skip_broken_stages`` we
    install a stage-collection error handler (see dvc.repo.index.collect_files)
    so the unreadable file is skipped whole, surfaced as a refusal, and the rest
    of the repo still exports.
    """
    from dvc.cachemgr import LEGACY_HASH_NAMES

    remote = _verify_remote(repo, remote_name)
    _verify_blockstore_match(remote, lakefs_storage_namespace)

    load_errors: list[ExportRefusal] = []
    if skip_broken_stages:

        def handler(path: str, exc: Exception) -> None:
            load_errors.append(_refuse(path, RefusalReason.STAGE_LOAD_FAILED, str(exc)))
    else:

        def handler(path: str, exc: Exception) -> None:
            raise BrokenStageError(f"{path!r}: {exc}") from exc

    repo.stage_collection_error_handler = handler
    outs = list(repo.index.outs)  # force collection so load_errors is filled
    yield from load_errors

    # At most two odbs are ever selected (current + legacy), so resolving the URI
    # builder once per odb keeps the constant per-backend work off the per-output
    # path — it matters for repos with very many single-file outputs. Keyed by
    # id(): both odbs stay reachable via ``remote`` for the whole loop, so there
    # is no GC/id-reuse window.
    uri_builders: dict[int, Callable[[str], str]] = {}
    for out in outs:
        if refusal := _qualify(out):
            yield refusal
            continue
        odb = remote.legacy_odb if out.hash_name in LEGACY_HASH_NAMES else remote.odb
        assert out.remote is None
        to_uri = uri_builders.get(id(odb))
        if to_uri is None:
            to_uri = uri_builders[id(odb)] = _uri_builder(odb.fs)
        yield _build_output(out, repo, odb, to_uri)


def import_outputs(  # noqa: PLR0913
    lakefs_repository: "lakefs.Repository",
    lakefs_branch: str,
    outputs: list[ExportOutput],
    *,
    commit_message: str,
    metadata: dict[str, str] | None = None,
    source_branch: str | None = None,
) -> ImportResult:
    """Import the objects of already-collected outputs into lakeFS."""
    try:
        default_branch = source_branch or lakefs_repository.properties.default_branch
        branch = lakefs_repository.branch(lakefs_branch).create(
            default_branch, exist_ok=True
        )
        mgr = branch.import_data(commit_message=commit_message, metadata=metadata)
        count = 0
        for o in outputs:
            for i in o.files:
                mgr.object(object_store_uri=i.physical_url, destination=i.repo_path)
                count += 1

        status = mgr.run(poll_interval=IMPORT_POLL_INTERVAL)
    except Exception as exc:
        raise LakeFSImportError(str(exc)) from exc

    if status.error is not None:
        raise LakeFSImportError(f"import failed: {status.error.message}")

    assert status.completed
    assert status.ingested_objects is not None
    assert status.commit is not None
    if count != status.ingested_objects:
        # The import has already committed by the time we get here, so surface the
        # commit id: the run "failed" but lakeFS data was written and is inspectable.
        raise LakeFSImportError(
            f"expected to import {count} objects but only {status.ingested_objects}"
            f" were ingested; a commit ({status.commit.id}) was still created — check"
            " that the source objects still exist"
        )
    return ImportResult(ingested=status.ingested_objects, commit_id=status.commit.id)
