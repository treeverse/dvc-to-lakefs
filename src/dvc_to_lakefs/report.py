from collections import defaultdict
from typing import TYPE_CHECKING

from dvc.ui import ui

from dvc_to_lakefs.core import Severity

if TYPE_CHECKING:
    from dvc_to_lakefs.core import ExportOutput, ExportRefusal, RefusalReason


def _pluralize(n: int, noun: str) -> str:
    """'1 object', '0 objects', '2 objects'."""
    return f"{n} {noun}" + ("" if n == 1 else "s")


def _line(level: int, text: str, *, highlight: bool = True) -> None:
    """Print ``text`` indented ``level`` steps (two spaces each)."""
    ui.rich_print("  " * level + text, highlight=highlight)


def _summarize(outputs: list["ExportOutput"], refusals: list["ExportRefusal"]) -> str:
    """One-line, scannable count of a branch's plan, e.g.

    '3 objects to import · 2 skipped · 1 broken stage'
    """
    n_skip = sum(1 for r in refusals if r.reason.severity is Severity.SKIP)
    n_broken = sum(1 for r in refusals if r.reason.severity is Severity.BROKEN)

    total = sum(len(o.files) for o in outputs)
    count = _pluralize(total, "object")
    parts = [f"{count} to import"]
    if n_skip:
        parts.append(f"{n_skip} skipped")
    if n_broken:
        parts.append(_pluralize(n_broken, "broken stage"))
    return " · ".join(parts)


def _print_importing(
    outputs: list["ExportOutput"], *, show_files: bool, dry_run: bool
) -> None:
    # In a dry run nothing is written, so use the conditional "Would import" rather
    # than the present-continuous "Importing" that reads as a live, finished import.
    _line(1, "[green]Would import:[/green]" if dry_run else "[green]Importing:[/green]")
    for o in outputs:
        if o.is_dir:
            # The view stays collapsed (just the dir and its count) unless
            # --show-files expanded it, so a real import shows the same tidy view.
            label = _pluralize(len(o.files), "file")
            expand = show_files and o.files
            _line(
                2, f"- [green]{o.repo_path}/[/green] ({label}){':' if expand else ''}"
            )
            for i in o.files if expand else ():  # nested under the dir header
                _line(
                    3,
                    f"- [green]{i.repo_path}[/green] <-- [dim]{i.physical_url}[/dim]",
                    highlight=False,
                )
        else:
            i = o.files[0]  # file output: exactly one object
            _line(
                2,
                f"- [green]{i.repo_path}[/green] <-- [dim]{i.physical_url}[/dim]",
                highlight=False,
            )


# Refusals are shown in distinct, ordered sections by severity: boring
# out-of-scope skips first, the alarming broken ones last (closest to the next
# branch / prompt) so they stand out. (severity, title, color)
_SECTIONS: list[tuple[Severity, str, str]] = [
    (Severity.REFUSE, "Refused", "red"),
    (Severity.SKIP, "Skipped", "yellow"),
    (Severity.BROKEN, "Broken", "red"),
]


def _print_refusals(refusals: list["ExportRefusal"]) -> None:
    """Print refusals grouped into severity sections, then by reason."""
    for severity, title, color in _SECTIONS:
        in_section = [r for r in refusals if r.reason.severity is severity]
        if not in_section:
            continue
        _line(1, f"[{color}]{title}:[/{color}]")
        by_reason: dict[RefusalReason, list[ExportRefusal]] = defaultdict(list)
        for r in in_section:
            by_reason[r.reason].append(r)
        for reason, group in by_reason.items():
            _line(2, f"[{color}]{reason.description} ({len(group)}):[/{color}]")
            for r in group:
                _line(3, f"- {r.output}" + (f" ({r.detail})" if r.detail else ""))


def report(
    outputs: list["ExportOutput"],
    refusals: list["ExportRefusal"],
    *,
    show_files: bool,
    dry_run: bool,
) -> None:
    _line(1, _summarize(outputs, refusals))
    # Imported first, then the skipped/broken sections so the alarming broken
    # ones land last, closest to the next branch.
    _print_importing(outputs, show_files=show_files, dry_run=dry_run)
    _print_refusals(refusals)
