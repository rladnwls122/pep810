"""Rewrite eligible imports to PEP 810 ``lazy`` imports.

The rewrite is deliberately the smallest one that can work: insert the five
characters ``lazy `` before the ``import`` or ``from`` keyword.  Nothing else in
the file moves.  Round-tripping through an AST would be easier to write and much
worse to use -- it discards comments, normalises quotes and string prefixes, and
reflows every parenthesised import list, turning a five-line diff into a
thousand-line one that no reviewer can read.

Every rewritten file is verified before it is written.  On Python 3.15 the
result is compiled directly; on older interpreters the ``lazy`` prefixes are
stripped first and the remainder compiled, which still catches a mangled
insertion.  A file that fails verification is left untouched and reported.
"""

from __future__ import annotations

import difflib
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ._syntax import insert_lazy, strip_lazy
from .analyzer import FileAnalysis
from .verdict import Decision, Verdict

__all__ = ["FileEdit", "rewrite", "render_lazy_modules", "apply_edits"]

#: PEP 810 landed in 3.15, so only there can we compile the real syntax.
_HAS_PEP810 = sys.version_info >= (3, 15)


@dataclass
class FileEdit:
    """A proposed change to one file."""

    path: Path
    original: str
    updated: str
    #: Line numbers of the import statements that were made lazy.
    changed_lines: list[int] = field(default_factory=list)
    error: str | None = None

    @property
    def is_change(self) -> bool:
        return self.error is None and self.updated != self.original

    def diff(self, context: int = 3, root: Path | None = None) -> str:
        """Unified diff of the change, empty when there is none.

        ``root`` shortens the header paths so the output can be piped straight
        into ``git apply`` from the project root.
        """
        if not self.is_change:
            return ""
        name = self.path
        if root is not None:
            try:
                name = self.path.relative_to(root)
            except ValueError:
                pass
        # git apply wants forward slashes even on Windows
        name = name.as_posix()
        return "".join(
            difflib.unified_diff(
                self.original.splitlines(keepends=True),
                self.updated.splitlines(keepends=True),
                fromfile=f"a/{name}",
                tofile=f"b/{name}",
                n=context,
            )
        )

    def write(self) -> bool:
        """Write the change to disk; returns whether anything was written."""
        if not self.is_change:
            return False
        self.path.write_text(self.updated, encoding="utf-8")
        return True


def rewrite(
    analysis: FileAnalysis,
    verdicts: list[Verdict],
    accept: frozenset[Decision] = frozenset({Decision.SAFE}),
) -> FileEdit:
    """Produce the edit that makes every accepted import lazy."""
    anchors = [
        (verdict.site.lineno, verdict.site.col)
        for verdict in verdicts
        if verdict.decision in accept and not verdict.site.already_lazy
    ]
    if not anchors:
        return FileEdit(analysis.path, analysis.source, analysis.source)

    updated = insert_lazy(analysis.source, anchors)
    error = _verify(updated, analysis.path)
    if error is not None:
        return FileEdit(
            analysis.path,
            analysis.source,
            analysis.source,
            error=f"rewrite would not compile ({error}); file left unchanged",
        )

    return FileEdit(
        path=analysis.path,
        original=analysis.source,
        updated=updated,
        changed_lines=sorted({lineno for lineno, _ in anchors}),
    )


def _verify(source: str, path: Path) -> str | None:
    """Compile the rewritten source, returning an error message or ``None``."""
    text = source if _HAS_PEP810 else strip_lazy(source).text
    try:
        compile(text, str(path), "exec")
    except SyntaxError as exc:
        return f"{exc.msg} at line {exc.lineno}"
    except ValueError as exc:  # null bytes and similar
        return str(exc)
    return None


def render_lazy_modules(
    analysis: FileAnalysis,
    verdicts: list[Verdict],
    accept: frozenset[Decision] = frozenset({Decision.SAFE}),
) -> str:
    """Render a ``__lazy_modules__`` declaration for the accepted imports.

    PEP 810 offers this as the way to opt into laziness while still running on
    interpreters older than 3.15: the list is inert before 3.15 and honoured
    after, so a library can ship one release that is fast on new Pythons and
    unchanged on old ones.  Returns an empty string when nothing qualifies.
    """
    modules: list[str] = []
    for verdict in verdicts:
        if verdict.decision not in accept:
            continue
        for module in verdict.site.modules:
            if module and not module.startswith(".") and module not in modules:
                modules.append(module)
    if not modules:
        return ""
    body = ",\n".join(f"    {module!r}" for module in sorted(modules))
    return f"__lazy_modules__ = [\n{body},\n]\n"


def apply_edits(edits: list[FileEdit], write: bool) -> tuple[int, list[FileEdit]]:
    """Write or count the edits; returns ``(files_changed, failures)``."""
    changed = 0
    failures = [edit for edit in edits if edit.error is not None]
    for edit in edits:
        if not edit.is_change:
            continue
        if write:
            edit.write()
        changed += 1
    return changed, failures
