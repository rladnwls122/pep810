"""Classifying an import target as stdlib, third-party or first-party."""

import pytest

from pep810.resolver import ModuleKind, ModuleResolver


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
