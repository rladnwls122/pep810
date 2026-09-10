"""The decision rules, which are where correctness actually matters."""

from pathlib import Path

import pytest

from pep810.analyzer import analyze_source
from pep810.effects import EffectAnalyzer
from pep810.knowledge import Confidence
from pep810.resolver import ModuleResolver
from pep810.verdict import Decision, Policy, judge


def decide(source, policy=None, root=None, path="demo.py"):
    analysis = analyze_source(source, Path(path))
    resolver = ModuleResolver(project_roots=[root] if root else [])
    analyzer = EffectAnalyzer(resolver=resolver)
    return [judge(site, analysis, analyzer, policy or Policy()) for site in analysis.sites]


def only(source, **kwargs):
    verdicts = decide(source, **kwargs)
    assert len(verdicts) == 1, [v.site.names for v in verdicts]
    return verdicts[0]


def test_deferred_use_of_a_clean_module_is_safe():
    verdict = only("import json\ndef f():\n    return json.loads('{}')\n")
    assert verdict.decision is Decision.SAFE


def test_module_level_use_has_no_benefit():
    verdict = only("import json\nX = json.dumps({})\n")
    assert verdict.decision is Decision.LOW_BENEFIT
    assert "I401" in verdict.codes


def test_unused_import_is_unsafe():
    # This is the rule that matters most: deferring an import nothing touches
    # does not delay its side effect, it cancels it.
    verdict = only("import readline\n")
    assert verdict.decision is Decision.UNSAFE
    assert "E201" in verdict.codes


def test_unused_import_in_a_package_init_is_a_reexport(tmp_path):
    verdict = only("import json\n", path=str(tmp_path / "__init__.py"))
    assert verdict.decision is not Decision.UNSAFE


def test_dunder_all_makes_init_unused_names_meaningful(tmp_path):
    verdicts = decide(
        "import json\nimport readline\n__all__ = ['json']\n",
        path=str(tmp_path / "__init__.py"),
    )
    assert verdicts[1].decision is Decision.UNSAFE


@pytest.mark.parametrize(
    "source, code",
    [
        ("from __future__ import annotations\n", "E103"),
        ("from os import *\n", "E102"),
        ("def f():\n    import json\n    return json\n", "E101"),
        ("try:\n    import json\nexcept ImportError:\n    pass\n", "E101"),
    ],
)
def test_illegal_contexts_are_ineligible(source, code):
    verdicts = decide(source)
    assert verdicts[0].decision is Decision.INELIGIBLE
    assert code in verdicts[0].codes


def test_type_checking_block_is_skipped():
    verdicts = decide("from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import a\n")
    assert verdicts[1].decision is Decision.SKIPPED


def test_already_lazy_is_left_alone():
    assert only("lazy import json\ndef f(): return json\n").decision is Decision.ALREADY_LAZY


def test_shadowed_name_is_unsafe():
    verdict = decide("import ujson\nujson = None\ndef f(): return ujson\n")[0]
    assert verdict.decision is Decision.UNSAFE
    assert "E203" in verdict.codes


def test_side_effect_target_is_risky(tmp_path):
    (tmp_path / "plugins.py").write_text("import atexit\natexit.register(f)\n")
    source = "import plugins\ndef f():\n    return plugins.REGISTRY\n"
    verdict = only(source, root=tmp_path)
    assert verdict.decision is Decision.RISKY
    assert any(code.startswith("W") for code in verdict.codes)


def test_relative_imports_are_resolved(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "plugins.py").write_text("import atexit\natexit.register(f)\n")
    source = "from . import plugins\ndef f():\n    return plugins.REGISTRY\n"
    verdict = only(source, root=tmp_path, path=str(package / "core.py"))
    assert verdict.decision is Decision.RISKY


def test_startup_modules_have_no_benefit():
    verdict = only("import os\ndef f():\n    return os.sep\n")
    assert verdict.decision is Decision.LOW_BENEFIT
    assert "I406" in verdict.codes


def test_confidence_threshold_moves_the_line(tmp_path):
    (tmp_path / "target.py").write_text("import other\nother.attr = 1\n")
    source = "import target\ndef f():\n    return target\n"
    strict = only(source, root=tmp_path, policy=Policy(block_at=Confidence.MEDIUM))
    lenient = only(source, root=tmp_path, policy=Policy(block_at=Confidence.HIGH + 1))
    assert strict.decision is Decision.RISKY
    assert lenient.decision is Decision.SAFE


def test_ignored_codes_are_suppressed(tmp_path):
    (tmp_path / "plugins.py").write_text("import atexit\natexit.register(f)\n")
    source = "import plugins\ndef f():\n    return plugins\n"
    verdict = only(source, root=tmp_path, policy=Policy(ignore=frozenset({"W301"})))
    assert verdict.decision is Decision.SAFE


def test_conditional_imports_can_be_excluded():
    source = "if x:\n    import json\ndef f(): return json\n"
    assert only(source).decision is Decision.SAFE
    assert only(source, policy=Policy(allow_conditional=False)).decision is Decision.INELIGIBLE
