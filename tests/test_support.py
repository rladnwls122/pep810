"""Resolver, project discovery, import-time parsing and filter generation."""

from pathlib import Path

import pytest

from lazyimp.filters import FilterPlan, render_filter_module
from lazyimp.importtime import parse_importtime
from lazyimp.project import discover, load_config, module_name_for
from lazyimp.resolver import ModuleKind, ModuleResolver


# --- resolver --------------------------------------------------------------

def test_resolves_stdlib_without_importing():
    resolver = ModuleResolver()
    assert resolver.resolve("json").kind is ModuleKind.PACKAGE
    assert resolver.resolve("sys").kind is ModuleKind.BUILTIN
    assert resolver.resolve("xml.etree.ElementTree").kind is ModuleKind.SOURCE
    assert resolver.resolve("definitely.not.here").kind is ModuleKind.NOT_FOUND


def test_resolves_namespace_and_extension_packages(tmp_path):
    (tmp_path / "ns").mkdir()
    (tmp_path / "ext.so").write_bytes(b"\x00")
    resolver = ModuleResolver(project_roots=[tmp_path])
    assert resolver.resolve("ns").kind is ModuleKind.NAMESPACE
    assert resolver.resolve("ext").kind is ModuleKind.EXTENSION


@pytest.mark.parametrize(
    "level, module, expected",
    [(1, "", "pkg"), (1, "core", "pkg.core"), (2, "", "")],
)
def test_relative_import_resolution(tmp_path, level, module, expected):
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    resolver = ModuleResolver(project_roots=[tmp_path])
    assert (resolver.resolve_from(module, level, package / "mod.py") or "") == expected


# --- project discovery ------------------------------------------------------

def test_discovery_skips_virtualenvs(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "dep.py").write_text("")
    names = {path.name for path in discover([tmp_path]).files}
    assert names == {"app.py"}


def test_explicit_file_beats_the_exclude_list(tmp_path):
    excluded = tmp_path / ".venv" / "dep.py"
    excluded.parent.mkdir(parents=True)
    excluded.write_text("")
    assert discover([excluded]).files == [excluded.resolve()]


def test_module_names_follow_package_boundaries(tmp_path):
    deep = tmp_path / "src" / "pkg" / "sub"
    deep.mkdir(parents=True)
    for directory in (deep.parent, deep):
        (directory / "__init__.py").write_text("")
    (deep / "mod.py").write_text("")
    assert module_name_for(deep / "mod.py") == "pkg.sub.mod"
    assert module_name_for(deep / "__init__.py") == "pkg.sub"


def test_loose_script_keeps_its_stem(tmp_path):
    (tmp_path / "script.py").write_text("")
    assert module_name_for(tmp_path / "script.py") == "script"


def test_config_is_optional(tmp_path):
    assert load_config(tmp_path) == {}
    (tmp_path / "pyproject.toml").write_text('[tool.lazyimp]\nignore = ["W311"]\n')
    assert load_config(tmp_path) == {"ignore": ["W311"]}


def test_malformed_config_does_not_raise(tmp_path):
    (tmp_path / "pyproject.toml").write_text("this is not toml [[[")
    assert load_config(tmp_path) == {}


# --- importtime -------------------------------------------------------------

SAMPLE = """\
import time: self [us] | cumulative | imported package
import time:       100 |        100 |     encodings.aliases
import time:       200 |        300 |   encodings
import time:        50 |        350 | codecs
"""


def test_importtime_parses_depth_from_indentation():
    tree = parse_importtime(SAMPLE)
    depths = {record.module: record.depth for record in tree.records}
    assert depths == {"encodings.aliases": 2, "encodings": 1, "codecs": 0}


def test_importtime_totals_only_roots():
    assert parse_importtime(SAMPLE).total_us == 350


def test_importtime_cost_falls_back_to_an_ancestor():
    tree = parse_importtime(SAMPLE)
    assert tree.cost_of("encodings.aliases.missing") == 100
    assert tree.cost_of("nothing") == 0


def test_importtime_keeps_unrelated_stderr():
    tree = parse_importtime(SAMPLE + "Traceback (most recent call last):\n")
    assert "Traceback" in tree.stderr and len(tree.records) == 3


def test_importtime_handles_empty_output():
    assert not parse_importtime("")


# --- filters ----------------------------------------------------------------

def build_filter(**kwargs):
    plan = FilterPlan(**kwargs)
    plan.deny("readline", "installs the line editor")
    plan.deny("myapp.plugins", "registers at import time")
    namespace = {}
    exec(render_filter_module(plan), namespace)
    return namespace["lazy_import_filter"]


def test_generated_filter_denies_listed_modules():
    decide = build_filter()
    assert decide(None, "readline", None) is False
    assert decide(None, "json", None) is True


def test_denying_a_package_denies_its_submodules():
    # Importing a submodule runs the parent's __init__ either way.
    decide = build_filter()
    assert decide(None, "myapp.plugins.sub", None) is False


def test_lazy_only_prefixes_restrict_everything_else():
    decide = build_filter(lazy_only_prefixes=["myapp"])
    assert decide(None, "myapp.core", None) is True
    assert decide(None, "json", None) is False


def test_generated_module_is_valid_python():
    compile(render_filter_module(FilterPlan()), "lazy_filter.py", "exec")
