"""Generating a ``sys.set_lazy_imports_filter`` module."""

from pep810.filters import FilterPlan, render_filter_module


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
