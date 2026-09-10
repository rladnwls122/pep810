"""Import contexts and usage classification."""

from pathlib import Path

import pytest

from lazyimp.analyzer import ImportContext, analyze_source


def analyze(source: str, name: str = "demo.py"):
    return analyze_source(source, Path(name))


@pytest.mark.parametrize(
    "source, expected",
    [
        ("import a\n", ImportContext.MODULE),
        ("if x:\n    import a\n", ImportContext.CONDITIONAL),
        ("def f():\n    import a\n", ImportContext.FUNCTION),
        ("class C:\n    import a\n", ImportContext.CLASS),
        ("try:\n    import a\nexcept ImportError:\n    pass\n", ImportContext.TRY),
        ("with open('f'):\n    import a\n", ImportContext.WITH),
        ("for i in y:\n    import a\n", ImportContext.LOOP),
        ("while y:\n    import a\n", ImportContext.LOOP),
        ("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import a\n",
         ImportContext.TYPE_CHECKING),
        ("if __name__ == '__main__':\n    import a\n", ImportContext.MAIN_GUARD),
    ],
)
def test_context_detection(source, expected):
    sites = [site for site in analyze(source).sites if "a" in site.names]
    assert sites and sites[0].context is expected


def test_nested_context_keeps_the_stronger_label():
    # An `if` inside a function is still function scope, which is what forbids
    # the keyword -- the innermost label must not win.
    source = "def f():\n    if x:\n        import a\n"
    site = analyze(source).sites[0]
    assert site.context is ImportContext.FUNCTION
    assert not site.context.is_syntactically_eligible


def test_star_and_future_are_flagged():
    analysis = analyze("from __future__ import annotations\nfrom os import *\n")
    assert analysis.sites[0].is_future
    assert analysis.sites[1].is_star


def test_module_level_use_is_eager():
    analysis = analyze("import os\nP = os.sep\n")
    assert analysis.usage_for("os").used_eagerly


def test_function_body_use_is_deferred():
    analysis = analyze("import os\ndef f():\n    return os.sep\n")
    usage = analysis.usage_for("os")
    assert usage.used_lazily and not usage.used_eagerly


@pytest.mark.parametrize(
    "source",
    [
        "import os\n@os.wrap\ndef f(): pass\n",          # decorator
        "import os\ndef f(x=os.sep): pass\n",            # default argument
        "import os\nclass C(os.PathLike): pass\n",       # base class
        "import os\nif os.sep: pass\n",                  # module-level condition
        "import os\nX = [os.sep for _ in range(1)]\n",   # comprehension
    ],
)
def test_contexts_that_run_immediately(source):
    assert analyze(source).usage_for("os").used_eagerly


def test_annotations_are_deferred():
    source = "import np\ndef f(x: np.Array) -> np.Array: ...\n"
    usage = analyze(source).usage_for("np")
    assert usage.type_only and not usage.eager


def test_string_annotations_still_count_as_use():
    source = "import pandas\ndef f() -> 'pandas.DataFrame': ...\n"
    assert not analyze(source).usage_for("pandas").unused


def test_unused_name_is_reported_as_unused():
    assert analyze("import readline\n").usage_for("readline").unused


def test_dunder_all_marks_a_reexport():
    analysis = analyze("import json\n__all__ = ['json']\n")
    assert analysis.usage_for("json").reexported
    assert analysis.has_dunder_all


def test_explicit_as_reexport_convention():
    analysis = analyze("from a import b as b\n")
    assert analysis.usage_for("b").reexported


def test_shadowing_and_deletion_are_recorded():
    analysis = analyze("import ujson\nujson = None\ndel ujson\n")
    usage = analysis.usage_for("ujson")
    assert usage.shadowed and usage.deleted


def test_dynamic_namespace_is_detected():
    assert analyze("import a\nprint(globals())\n").dynamic_namespace


def test_submodule_import_binds_the_root():
    site = analyze("import xml.etree.ElementTree\n").sites[0]
    assert site.names == ["xml"]
    assert site.modules == ["xml.etree.ElementTree"]
    assert site.bindings[0].is_submodule_chain


def test_syntax_error_is_reported_not_raised():
    assert analyze("def f(:\n").syntax_error is not None


def test_already_lazy_is_detected():
    assert analyze("lazy import json\n").sites[0].already_lazy
