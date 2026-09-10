"""Find the Python files to analyse and name the modules they define.

Nothing here is clever, but getting it wrong is expensive: analysing a
``.venv/`` walks tens of thousands of third-party files, and a wrong
path-to-module mapping makes every relative import unresolvable.

Source roots are inferred from where packages actually start.  For a file at
``src/pkg/sub/mod.py`` the walk goes up while ``__init__.py`` exists, stops at
``src/``, and yields the module name ``pkg.sub.mod`` -- which works for src
layouts, flat layouts and loose scripts without being told which is in use.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["DEFAULT_EXCLUDES", "Project", "discover", "module_name_for", "load_config"]

#: Directories that are never worth analysing.  Virtual environments dominate
#: this list because they are both huge and not the project's code.
DEFAULT_EXCLUDES: tuple[str, ...] = (
    ".git", ".hg", ".svn", ".tox", ".nox", ".venv", "venv", "env",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "node_modules", "build", "dist", "site-packages", ".eggs",
    "*.egg-info",
)


@dataclass
class Project:
    """A set of files to analyse plus the roots their module names hang off."""

    root: Path
    files: list[Path] = field(default_factory=list)
    source_roots: list[Path] = field(default_factory=list)

    def module_name(self, file: Path) -> str:
        return module_name_for(file, self.source_roots)


def _is_excluded(path: Path, excludes: tuple[str, ...]) -> bool:
    return any(
        fnmatch.fnmatch(part, pattern)
        for part in path.parts
        for pattern in excludes
    )


def discover(
    paths: list[Path],
    excludes: tuple[str, ...] = DEFAULT_EXCLUDES,
    follow_symlinks: bool = False,
) -> Project:
    """Collect ``.py`` files under ``paths``, skipping excluded directories.

    A path given explicitly is always analysed, even if it matches an exclude:
    pointing the tool at ``.venv/lib/foo.py`` on purpose should work.
    """
    files: list[Path] = []
    roots: set[Path] = set()

    for given in paths:
        given = Path(given)
        if given.is_file():
            files.append(given.resolve())
            continue
        if not given.is_dir():
            continue
        for candidate in sorted(given.rglob("*.py")):
            if not follow_symlinks and candidate.is_symlink():
                continue
            relative = candidate.relative_to(given)
            if _is_excluded(relative, excludes):
                continue
            files.append(candidate.resolve())

    files = sorted(set(files))
    for file in files:
        roots.add(_source_root_of(file))

    root = Path(paths[0]).resolve() if paths else Path.cwd()
    if root.is_file():
        root = root.parent
    return Project(root=root, files=files, source_roots=sorted(roots))


def _source_root_of(file: Path) -> Path:
    """The directory a file's dotted module name is relative to."""
    directory = file.parent
    while (directory / "__init__.py").exists():
        parent = directory.parent
        if parent == directory:
            break
        directory = parent
    return directory


def module_name_for(file: Path, source_roots: list[Path] | None = None) -> str:
    """Dotted module name for ``file``.

    ``source_roots`` is only a hint; when it does not contain the file the name
    is derived the same way :func:`_source_root_of` derives it.
    """
    file = file.resolve()
    root = _source_root_of(file)
    if source_roots:
        for candidate in sorted(source_roots, key=lambda p: len(p.parts), reverse=True):
            try:
                file.relative_to(candidate)
            except ValueError:
                continue
            root = candidate if len(candidate.parts) > len(root.parts) else root
            break

    try:
        relative = file.relative_to(root)
    except ValueError:
        return file.stem

    parts = list(relative.parts)
    if parts[-1] == "__init__.py":
        parts.pop()
    else:
        parts[-1] = parts[-1][: -len(".py")] if parts[-1].endswith(".py") else parts[-1]
    return ".".join(parts)


def load_config(root: Path) -> dict:
    """Read ``[tool.pep810]`` from ``pyproject.toml``, if there is one.

    Returns an empty mapping when the file, the table or a TOML parser is
    missing; configuration is a convenience, never a requirement.
    """
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return {}
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ModuleNotFoundError:
            return {}
    try:
        with pyproject.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError):
        return {}
    tool = data.get("tool")
    if not isinstance(tool, dict):
        return {}
    config = tool.get("pep810")
    return config if isinstance(config, dict) else {}
