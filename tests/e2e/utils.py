from typing import TypedDict, Union

import lakefs

CatStruct = dict[str, Union[bytes, "CatStruct"]]


def lakefs_cat(repo: lakefs.Repository, ref: str, path: str = "") -> CatStruct:
    branch = repo.branch(ref)
    prefix = path.rstrip("/") + "/" if path else ""
    result: CatStruct = {}

    for obj in branch.objects(prefix=prefix):
        parts = obj.path[len(prefix) :].split("/")
        node = result
        for part in parts[:-1]:
            sub = node.setdefault(part, {})
            assert isinstance(sub, dict)
            node = sub
        with branch.object(obj.path).reader(mode="rb") as fd:
            content = fd.read()
            assert isinstance(content, bytes)
            node[parts[-1]] = content
    return result


class CommitDict(TypedDict, total=False):
    id: str
    parents: list[str]
    committer: str
    message: str
    creation_date: int
    meta_range_id: str
    metadata: dict[str, str] | None


def commit_to_dict(commit: lakefs.Commit) -> CommitDict:
    return CommitDict(
        id=commit.id,
        parents=commit.parents,
        committer=commit.committer,
        message=commit.message,
        creation_date=commit.creation_date,
        meta_range_id=commit.meta_range_id,
        metadata=commit.metadata,
    )


def lakefs_head(repo: lakefs.Repository, ref: str) -> CommitDict:
    return commit_to_dict(repo.branch(ref).get_commit())  # type: ignore[no-untyped-call]


def lakefs_log(repo: lakefs.Repository, ref: str) -> list[CommitDict]:
    return [commit_to_dict(c) for c in repo.branch(ref).log()]
