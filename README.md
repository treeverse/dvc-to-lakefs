# dvc-to-lakefs

Zero-copy import of DVC-tracked data into [lakeFS](https://lakefs.io). Objects are linked by reference from the DVC remote.

## Requirements:

- Python 3.10+
- a Git-backed DVC repo (`dvc init --no-scm` is not supported)
- a configured DVC remote with data already pushed (`dvc push`)
- a running lakeFS instance with a repository created on the same blockstore as the DVC remote (e.g. both on S3); a mismatch will return an error
- lakeFS credentials set up (reads from `~/.lakectl.yaml` by default, or `LAKECTL_CONFIG_FILE`)
- read access to the DVC remote storage configured for DVC and for lakeFS server

## Installation

```console
pip install dvc-to-lakefs
```

## Usage

```console
lakectl import-from-dvc dvc-repo lakefs://<repo-name>
```

> [!NOTE]
> You can also invoke the tool directly:
>
> ```console
> lakectl-import-from-dvc dvc-repo lakefs://<repo>  # or,
> python -m dvc_to_lakefs dvc-repo lakefs://<repo>
> ```

Reads the HEAD of the current Git branch and imports all tracked DVC outputs into a lakeFS branch of the same name.

- The lakeFS branch is created from the default branch if it doesn't exist.
- A new commit is created on the branch with the imported files. The commit message matches the Git commit message, and the commit includes a `git_sha` metadata field with the corresponding Git SHA.
- Existing files at the same path are overwritten; all other files on the branch are left untouched.
- Re-running the import never deletes files. Removing a `.dvc` file from DVC and re-importing will not remove it from lakeFS.

Use `--dry-run` to preview what would be imported:

```console
lakectl import-from-dvc ./myrepo lakefs://myrepo --dry-run
```

## Options

| Flag | Description |
|---|---|
| `-r`, `--remote name` | DVC remote to use (default: the repo's default remote) |
| `--branch branch` | Git branch to export; repeat to export multiple branches (default: current branch) |
| `--dry-run` | Preview the import plan without writing anything to lakeFS |
| `--skip-broken-stages` | Skip unreadable `dvc.yaml`/`dvc.lock`/`.dvc` files and export the rest of the repo |
| `--skip-broken-revs` | When exporting multiple branches, skip any branch that fails instead of aborting |
| `--show-files` | Expand directory outputs to list every file instead of a single summary line |

## Examples

```console
# export two branches
lakectl import-from-dvc ./myrepo lakefs://myrepo --branch main --branch dev

# use a specific remote
lakectl import-from-dvc ./myrepo lakefs://myrepo --remote staging

# skip branches that fail
lakectl import-from-dvc ./myrepo lakefs://myrepo --branch main --branch dev --skip-broken-revs
```

## Unsupported outputs

The following outputs are not supported and will be skipped (reported under "Skipped" in the output):

- no hash info (stage was never run)
- directory output with missing or corrupted cache (run `dvc push` to fix)
- `cache: false` or `push: false`
- `dvc import` and `dvc import-url` stages
- external outputs or paths outside the repository
- per-output `remote:` override in `dvc.yaml`
- cloud-versioned outputs (pushed to a `version_aware = true` remote)

## Supported remotes

S3, GCS, Azure Blob Storage, and local filesystem (Linux/macOS only).

Not supported: worktree remotes, version-aware remotes (`version_aware = true`), `dvc init --no-scm`.

## Limitations

- Only HEAD of the Git branch is exported. Git history is not replayed.
- Uncommitted and staged changes are ignored; only committed state is exported.

## Contributing

Contributions are welcome! This project uses [uv](https://docs.astral.sh/uv/) for environment and dependency management.

### Setup

First, [fork](https://github.com/treeverse/dvc-to-lakefs/fork) the repository on GitHub and clone your fork:

```console
git clone https://github.com/<your-username>/dvc-to-lakefs
cd dvc-to-lakefs

# create the virtualenv and install all dev dependencies
uv sync --group dev

# install the pre-commit hooks
uv run prek install
```

Code is formatted and linted with [ruff](https://docs.astral.sh/ruff/) and type checked with mypy in strict mode. Both run automatically via pre-commit.

### Tests

```console
uv run pytest                       # unit + e2e tests (runs in parallel via pytest-xdist)
uv run pytest tests/unit            # unit tests only
```

The e2e tests spin up local backends and a lakeFS instance (the lakeFS binary is downloaded automatically on first run), so they may take longer than the unit tests. The S3 backend runs in-process via moto, but the Azure and GCS backends need [Docker](https://docs.docker.com/get-docker/) (they start Azurite and fake-gcs-server containers).

### Opening a PR

- Sign the [lakeFS CLA](https://cla-assistant.io/treeverse/dvc-to-lakefs) (individual or corporate) when opening your first pull request.
- Work on a branch off `main`, not your fork's `main`.
- Keep each PR focused on a single change.
- Run the same checks as [CI](.github/workflows/) before requesting review:

  ```console
  uv run prek run --all-files   # ruff lint + format, mypy, and assorted checks
  uv run pytest                 # full test suite
  ```

## License

Licensed under the [Apache License 2.0](LICENSE).
