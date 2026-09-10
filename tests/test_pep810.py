"""The source shim must survive real-world formatting without moving anything."""

import ast

import pytest

from lazyimp._pep810 import insert_lazy, splitlines_keepends, strip_lazy


def test_strips_only_real_soft_keywords():
    source = (
        "lazy import json\n"
        "lazy = 1\n"
        "x = lazy_import('nope')\n"
        "s = '''lazy import fake'''\n"
        "# lazy import commented\n"
    )
    stripped = strip_lazy(source)
    assert sorted(stripped.prefixes) == [1]
    assert stripped.text.startswith("import json\n")
    assert "lazy = 1" in stripped.text


def test_line_numbers_are_preserved():
    source = "lazy import a\nlazy from b import c\nx = 1\n"
    stripped = strip_lazy(source)
    assert len(splitlines_keepends(stripped.text)) == len(splitlines_keepends(source))


def test_columns_map_back_to_the_original():
    source = "if True:\n    lazy import csv\n"
    stripped = strip_lazy(source)
    tree = ast.parse(stripped.text)
    node = tree.body[0].body[0]
    column = stripped.original_col(node.lineno, node.col_offset)
    assert source.splitlines()[1][column:].startswith("import csv")


def test_stripped_source_parses_on_any_interpreter():
    # Blanking the prefix with spaces would make this an IndentationError.
    ast.parse(strip_lazy("lazy import json\n").text)


def test_insert_is_idempotent():
    source = "import json\n"
    once = insert_lazy(source, [(1, 0)])
    assert once == "lazy import json\n"
    assert insert_lazy(once, [(1, 0)]) == once


def test_insert_preserves_comments_and_continuations():
    source = "from x import (  # keep\n    a,\n)\n"
    updated = insert_lazy(source, [(1, 0)])
    assert updated == "lazy from x import (  # keep\n    a,\n)\n"


def test_insert_applies_bottom_up():
    source = "import a\nimport b\nimport c\n"
    updated = insert_lazy(source, [(1, 0), (2, 0), (3, 0)])
    assert updated == "lazy import a\nlazy import b\nlazy import c\n"


def test_unparseable_source_yields_no_prefixes():
    assert strip_lazy("def f(:\n").prefixes == {}


@pytest.mark.parametrize(
    "source",
    ["", "\n", "# only a comment\n", "x = 1", "\x0c\nimport json\n"],
)
def test_degenerate_inputs(source):
    assert strip_lazy(source).text == source
