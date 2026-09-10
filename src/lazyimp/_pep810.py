"""Read and write PEP 810 source on interpreters that predate PEP 810.

``lazy import json`` is a :exc:`SyntaxError` for every CPython released before
3.15, which would make a migration tool unable to run on the very interpreter
most projects are migrating *from*.  This module bridges the gap.

``lazy`` is a soft keyword: it is only special immediately before ``import`` or
``from`` at the start of a logical line.  :mod:`tokenize` knows nothing about
grammar, so it happily tokenizes 3.15 source on 3.9+, and it *does* know about
strings, comments and line continuations.  That makes it a reliable way to find
the real ``lazy`` prefixes without the false positives a regex would produce on
``lazy = 1``, ``lazy_import(...)`` or the word "lazy" inside a docstring.

The two public helpers cooperate:

* :func:`strip_lazy` deletes the prefixes so :func:`ast.parse` can run, and
  records how many characters it removed from each line.  Line numbers are never
  disturbed, and :meth:`StrippedSource.original_col` maps a column in the
  stripped text back to the untouched source.
* :func:`insert_lazy` writes prefixes at chosen anchors *in the original
  source*, skipping statements that already have one.

A codemod therefore parses the stripped text, decides using the AST, and edits
the original text at the mapped anchors -- touching nothing but the bytes it
means to change.
"""

from __future__ import annotations

import io
import tokenize
from dataclasses import dataclass

__all__ = [
    "LazyPrefix",
    "StrippedSource",
    "strip_lazy",
    "insert_lazy",
    "splitlines_keepends",
]


@dataclass(frozen=True)
class LazyPrefix:
    """A ``lazy`` soft keyword found before an import statement.

    ``lazy_col`` is where the keyword itself starts and ``keyword_col`` is where
    the following ``import``/``from`` starts, both 0-based on ``lineno``
    (1-based).  Keeping both lets a rewrite delete exactly the keyword and the
    whitespace that separates it from the import.
    """

    lineno: int
    lazy_col: int
    keyword_col: int
    keyword: str  # "import" or "from"


@dataclass
class StrippedSource:
    """Source with every ``lazy`` prefix removed, plus a map of what was removed.

    ``prefixes`` is keyed by line number.  Stripping never adds or removes lines,
    so that key is valid for both the stripped and the original text.
    """

    text: str
    prefixes: dict[int, LazyPrefix]

    def is_lazy(self, lineno: int) -> bool:
        """Whether the statement starting on ``lineno`` was already lazy."""
        return lineno in self.prefixes

    def original_col(self, lineno: int, col: int) -> int:
        """Map a column in the stripped text back to the original source.

        Removing ``lazy`` shifts everything after it on that line left by the
        width of the keyword plus its trailing whitespace; this adds that width
        back for any column at or after the removal point.
        """
        prefix = self.prefixes.get(lineno)
        if prefix is None or col < prefix.lazy_col:
            return col
        return col + (prefix.keyword_col - prefix.lazy_col)


def splitlines_keepends(text: str) -> list[str]:
    """Split like :meth:`str.splitlines` with ``keepends=True``.

    :meth:`str.splitlines` treats several exotic code points (``\\x0b``,
    ``\\x0c``, ``\\u2028`` ...) as line breaks; the Python tokenizer does not.
    Using the built-in would misalign every line number in a file that contains
    a form feed, which real code does use as a section separator.
    """
    lines = text.split("\n")
    out = [line + "\n" for line in lines[:-1]]
    if lines[-1]:
        out.append(lines[-1])
    return out


def _find_prefixes(text: str) -> list[LazyPrefix]:
    """Locate every ``lazy`` soft keyword that prefixes an import statement."""
    found: list[LazyPrefix] = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # A file we cannot tokenize cannot contain a prefix we can trust.
        return found

    # A logical line starts after NEWLINE/NL/INDENT/DEDENT and at the very
    # beginning of the file.  COMMENT tokens never end one.
    at_stmt_start = True
    for index, tok in enumerate(tokens):
        if tok.type in (tokenize.NEWLINE, tokenize.NL, tokenize.INDENT, tokenize.DEDENT):
            at_stmt_start = True
            continue
        if tok.type in (tokenize.COMMENT, tokenize.ENCODING):
            continue
        if tok.type == tokenize.ENDMARKER:
            break

        if at_stmt_start and tok.type == tokenize.NAME and tok.string == "lazy":
            nxt = _next_significant(tokens, index)
            if nxt is not None and nxt.type == tokenize.NAME and nxt.string in ("import", "from"):
                found.append(
                    LazyPrefix(
                        lineno=tok.start[0],
                        lazy_col=tok.start[1],
                        keyword_col=nxt.start[1],
                        keyword=nxt.string,
                    )
                )
        # `;` also starts a new simple statement on the same physical line.
        at_stmt_start = tok.type == tokenize.OP and tok.string == ";"

    return found


def _next_significant(tokens: list[tokenize.TokenInfo], start: int) -> tokenize.TokenInfo | None:
    for tok in tokens[start + 1 :]:
        if tok.type in (tokenize.COMMENT, tokenize.NL):
            continue
        return tok
    return None


def strip_lazy(text: str) -> StrippedSource:
    """Remove ``lazy`` prefixes so the result parses on any Python >= 3.9.

    The keyword and the whitespace up to the following ``import``/``from`` are
    deleted outright.  Blanking them with spaces would keep columns aligned but
    turn ``lazy import json`` into an indented statement, which is a syntax error
    at module level -- so instead the shift is recorded and replayed by
    :meth:`StrippedSource.original_col`.
    """
    prefixes = _find_prefixes(text)
    if not prefixes:
        return StrippedSource(text=text, prefixes={})

    lines = splitlines_keepends(text)
    by_line: dict[int, LazyPrefix] = {}
    for prefix in prefixes:
        line = lines[prefix.lineno - 1]
        lines[prefix.lineno - 1] = line[: prefix.lazy_col] + line[prefix.keyword_col :]
        by_line[prefix.lineno] = prefix

    return StrippedSource(text="".join(lines), prefixes=by_line)


def insert_lazy(text: str, anchors: list[tuple[int, int]]) -> str:
    """Insert ``lazy `` at each ``(lineno, col)`` anchor.

    ``anchors`` point at the ``import``/``from`` keyword of statements that
    should become lazy.  Anchors are applied bottom-up so earlier insertions do
    not shift the columns of later ones, and an anchor whose statement is
    already lazy is ignored, making the operation idempotent.
    """
    if not anchors:
        return text

    already = {p.lineno for p in _find_prefixes(text)}
    lines = splitlines_keepends(text)
    ordered = sorted(set(anchors), key=lambda a: (a[0], a[1]), reverse=True)

    for lineno, col in ordered:
        if lineno in already or not 1 <= lineno <= len(lines):
            continue
        line = lines[lineno - 1]
        lines[lineno - 1] = line[:col] + "lazy " + line[col:]

    return "".join(lines)
