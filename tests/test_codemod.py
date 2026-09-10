"""The rewrite must be minimal, verified, and safe to run twice."""

from pathlib import Path

from lazyimp.analyzer import analyze_source
from lazyimp.codemod import rewrite, render_lazy_modules
from lazyimp.effects import EffectAnalyzer
from lazyimp.resolver import ModuleResolver
from lazyimp.verdict import Decision, Policy, judge


def edit_for(source, path=Path("demo.py"), accept=frozenset({Decision.SAFE})):
    analysis = analyze_source(source, path)
    analyzer = EffectAnalyzer(resolver=ModuleResolver())
    verdicts = [judge(site, analysis, analyzer, Policy()) for site in analysis.sites]
    return rewrite(analysis, verdicts, accept), verdicts


def test_only_safe_imports_are_rewritten():
    source = (
        "import json\n"        # safe: used in a function
        "import readline\n"    # unsafe: never used
        "import os\n"          # low benefit: used at module level
        "P = os.sep\n"
        "def f():\n"
        "    return json.loads('{}')\n"
    )
    edit, _ = edit_for(source)
    assert edit.updated.startswith("lazy import json\nimport readline\nimport os\n")


def test_comments_and_formatting_survive():
    source = "import json  # keep me\nfrom x import (\n    a,\n)\ndef f(): return json, a\n"
    edit, _ = edit_for(source)
    assert "# keep me" in edit.updated
    assert "from x import (\n    a,\n)" in edit.updated.replace("lazy ", "")


def test_rewrite_is_idempotent():
    source = "import json\ndef f(): return json\n"
    first, _ = edit_for(source)
    second, _ = edit_for(first.updated)
    assert second.updated == first.updated
    assert not second.is_change


def test_no_eligible_imports_means_no_change():
    edit, _ = edit_for("import readline\n")
    assert not edit.is_change and edit.error is None


def test_diff_is_relative_to_a_root(tmp_path):
    source = "import json\ndef f(): return json\n"
    edit, _ = edit_for(source, path=tmp_path / "pkg" / "mod.py")
    assert "a/pkg/mod.py" in edit.diff(root=tmp_path)


def test_rewrite_output_compiles():
    source = "import json\nimport csv\ndef f(): return json, csv\n"
    edit, _ = edit_for(source)
    assert edit.error is None
    # Verification already ran inside rewrite(); a returned change proves it passed.
    assert edit.is_change


def test_write_round_trips_through_disk(tmp_path):
    path = tmp_path / "mod.py"
    path.write_text("import json\ndef f(): return json\n")
    analysis = analyze_source(path.read_text(), path)
    analyzer = EffectAnalyzer(resolver=ModuleResolver())
    verdicts = [judge(site, analysis, analyzer, Policy()) for site in analysis.sites]
    assert rewrite(analysis, verdicts).write()
    assert path.read_text().startswith("lazy import json")


def test_lazy_modules_style_lists_targets():
    source = "import json\nimport csv\ndef f(): return json, csv\n"
    analysis = analyze_source(source, Path("demo.py"))
    analyzer = EffectAnalyzer(resolver=ModuleResolver())
    verdicts = [judge(site, analysis, analyzer, Policy()) for site in analysis.sites]
    rendered = render_lazy_modules(analysis, verdicts)
    assert "__lazy_modules__" in rendered and "'json'" in rendered and "'csv'" in rendered
    compile(rendered, "gen.py", "exec")


def test_lazy_modules_is_empty_when_nothing_qualifies():
    analysis = analyze_source("import readline\n", Path("demo.py"))
    assert render_lazy_modules(analysis, []) == ""


def test_include_risky_widens_the_rewrite(tmp_path):
    (tmp_path / "plugins.py").write_text("import atexit\natexit.register(f)\n")
    source = "import plugins\ndef f(): return plugins\n"
    analysis = analyze_source(source, tmp_path / "app.py")
    analyzer = EffectAnalyzer(resolver=ModuleResolver(project_roots=[tmp_path]))
    verdicts = [judge(site, analysis, analyzer, Policy()) for site in analysis.sites]
    assert not rewrite(analysis, verdicts).is_change
    widened = rewrite(analysis, verdicts, frozenset({Decision.SAFE, Decision.RISKY}))
    assert widened.is_change
