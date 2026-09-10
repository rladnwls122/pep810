"""End-to-end behaviour of the command line, including exit codes."""

import json

import pytest

from lazyimp.cli import EXIT_ERROR, EXIT_FINDINGS, EXIT_OK, main


@pytest.fixture()
def project(tmp_path):
    """A small package with one safe, one unsafe and one risky import."""
    package = tmp_path / "myapp"
    package.mkdir()
    (package / "__init__.py").write_text("from .core import run\n__all__ = ['run']\n")
    (package / "plugins.py").write_text("import atexit\nREGISTRY = {}\natexit.register(REGISTRY.clear)\n")
    (package / "core.py").write_text(
        "import json\n"
        "import readline\n"
        "from . import plugins\n"
        "def run(payload):\n"
        "    return json.loads(payload), plugins.REGISTRY\n"
    )
    return tmp_path


def test_analyze_reports_without_failing(project, capsys):
    assert main(["analyze", str(project)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "safe" in out and "unsafe" in out


def test_analyze_json_is_machine_readable(project, capsys):
    main(["analyze", str(project), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["files"] == 3
    decisions = {
        entry["decision"]
        for file in payload["files"]
        for entry in file["imports"]
    }
    assert {"safe", "unsafe"} <= decisions


def test_analyze_markdown_renders_tables(project, capsys):
    main(["analyze", str(project), "--format", "markdown"])
    out = capsys.readouterr().out
    assert out.startswith("# lazyimp report") and "| decision | count |" in out


def test_apply_without_write_leaves_files_alone(project, capsys):
    before = (project / "myapp" / "core.py").read_text()
    assert main(["apply", str(project)]) == EXIT_OK
    assert (project / "myapp" / "core.py").read_text() == before
    assert "+lazy import json" in capsys.readouterr().out


def test_apply_write_edits_and_is_idempotent(project, capsys):
    assert main(["apply", str(project), "--write"]) == EXIT_OK
    text = (project / "myapp" / "core.py").read_text()
    assert text.startswith("lazy import json\nimport readline\n")
    capsys.readouterr()

    assert main(["apply", str(project), "--write"]) == EXIT_OK
    assert (project / "myapp" / "core.py").read_text() == text
    assert "rewrote 0 file" in capsys.readouterr().err


def test_check_fails_while_eligible_imports_remain(project, capsys):
    assert main(["check", str(project)]) == EXIT_FINDINGS
    capsys.readouterr()
    main(["apply", str(project), "--write"])
    capsys.readouterr()
    assert main(["check", str(project)]) == EXIT_OK


def test_check_honours_max_eager(project):
    assert main(["check", str(project), "--max-eager", "99"]) == EXIT_OK


def test_filter_writes_an_installable_module(project, tmp_path, capsys):
    output = tmp_path / "lazy_filter.py"
    assert main(["filter", str(project), "-o", str(output), "--lazy-only", "myapp"]) == EXIT_OK
    compile(output.read_text(), str(output), "exec")
    assert "readline" in output.read_text()


def test_ignore_suppresses_a_reason_code(project, capsys):
    main(["analyze", str(project), "--format", "json", "--ignore", "E201"])
    payload = json.loads(capsys.readouterr().out)
    codes = {
        reason["code"]
        for file in payload["files"]
        for entry in file["imports"]
        for reason in entry["reasons"]
    }
    assert "E201" not in codes


def test_bench_refuses_an_interpreter_without_pep810(capsys):
    assert main(["bench", "-e", "import json"]) == EXIT_ERROR
    assert "does not implement PEP 810" in capsys.readouterr().err


def test_syntax_errors_are_reported_not_raised(tmp_path, capsys):
    (tmp_path / "broken.py").write_text("def f(:\n")
    assert main(["analyze", str(tmp_path)]) == EXIT_ERROR
    assert "could not analyse" in capsys.readouterr().out


def test_lazy_modules_style_prints_declarations(project, capsys):
    assert main(["apply", str(project), "--style", "lazy-modules"]) == EXIT_OK
    assert "__lazy_modules__" in capsys.readouterr().out
