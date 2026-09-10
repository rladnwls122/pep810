"""Walking a project tree and reading its configuration."""

from pep810.project import discover, load_config, module_name_for


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
    (tmp_path / "pyproject.toml").write_text('[tool.pep810]\nignore = ["W311"]\n')
    assert load_config(tmp_path) == {"ignore": ["W311"]}


def test_malformed_config_does_not_raise(tmp_path):
    (tmp_path / "pyproject.toml").write_text("this is not toml [[[")
    assert load_config(tmp_path) == {}
