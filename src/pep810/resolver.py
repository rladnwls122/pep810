"""Locate the source of a module without importing anything.

Deciding whether ``import foo`` is safe to defer requires reading ``foo``'s
source.  The obvious way to find it -- :func:`importlib.util.find_spec` -- must
import every parent package to consult its ``__path__``, so pointing it at a
dependency tree executes exactly the import-time side effects this tool exists
to warn about.  A tool that has to run the code to tell you the code is unsafe
is not much of a static analyser.

So resolution here walks the filesystem itself: for ``a.b.c`` it looks for
``a/`` on the search path, then ``b/`` inside it, then ``c.py`` or ``c/``.  That
covers regular packages, namespace packages and extension modules, and it never
executes a line of the code under analysis.

Not covered: importers that are not path-based (zipimport, frozen modules,
``__init__`` files that extend ``__path__`` at runtime).  Those resolve to
:attr:`ModuleKind.NOT_FOUND` and the risk model treats them as unknown rather
than safe.
"""

from __future__ import annotations

import enum
import os
import sys
import sysconfig
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["ModuleKind", "ResolvedModule", "ModuleResolver"]

#: Suffixes for compiled extension modules, longest first so that
#: ``.cpython-315-x86_64-linux-gnu.so`` wins over a bare ``.so``.
_EXTENSION_SUFFIXES = (
    ".abi3t.so",
    ".abi3.so",
    ".so",
    ".pyd",
    ".dylib",
)

_SOURCE_SUFFIXES = (".py", ".pyi")


class ModuleKind(enum.Enum):
    """What kind of module a name resolved to."""

    SOURCE = "source"  #: A ``.py`` file we can parse.
    PACKAGE = "package"  #: A directory with ``__init__.py``.
    NAMESPACE = "namespace"  #: A directory without ``__init__.py`` (PEP 420).
    EXTENSION = "extension"  #: A compiled extension; opaque to static analysis.
    BUILTIN = "builtin"  #: Compiled into the interpreter (``sys``, ``_io`` ...).
    FROZEN = "frozen"  #: Frozen into the interpreter (``os``, ``codecs`` ...).
    NOT_FOUND = "not_found"  #: Nothing on the search path matches.


@dataclass(frozen=True)
class ResolvedModule:
    """The outcome of resolving one dotted module name."""

    name: str
    kind: ModuleKind
    path: Path | None = None
    #: True when the module lives inside the analysed project rather than in
    #: site-packages or the standard library.
    is_first_party: bool = False
    #: True when the module ships with CPython itself.
    is_stdlib: bool = False

    @property
    def is_analysable(self) -> bool:
        """Whether :attr:`path` points at source this tool can parse."""
        return self.kind in (ModuleKind.SOURCE, ModuleKind.PACKAGE) and self.path is not None


@dataclass
class ModuleResolver:
    """Resolve dotted module names against a fixed set of search paths.

    ``project_roots`` are searched first and mark their modules first-party.
    ``search_paths`` defaults to :data:`sys.path`, which makes the analysis
    reflect the environment the project actually runs in.
    """

    project_roots: list[Path] = field(default_factory=list)
    search_paths: list[Path] = field(default_factory=list)
    _cache: dict[str, ResolvedModule] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.project_roots = [Path(p).resolve() for p in self.project_roots]
        if not self.search_paths:
            self.search_paths = [Path(p).resolve() for p in sys.path if p and os.path.isdir(p)]
        else:
            self.search_paths = [Path(p).resolve() for p in self.search_paths]
        self._stdlib_dirs = _stdlib_dirs()

    def resolve(self, name: str) -> ResolvedModule:
        """Resolve ``name``, caching both hits and misses."""
        if name in self._cache:
            return self._cache[name]
        resolved = self._resolve_uncached(name)
        self._cache[name] = resolved
        return resolved

    def resolve_from(self, module: str, level: int, current_file: Path) -> str | None:
        """Turn a relative ``from`` import into an absolute module name.

        ``level`` is :attr:`ast.ImportFrom.level`: 1 for ``from . import x``, 2
        for ``from .. import x`` and so on.  Returns ``None`` when the relative
        import climbs past the top of its package, which means we cannot tell
        what it refers to.
        """
        if level == 0:
            return module or None

        package = self._package_of(current_file)
        if package is None:
            return None

        parts = package.split(".") if package else []
        # `from . import x` inside a/b/__init__.py is rooted at a.b, but inside
        # a/b/c.py it is rooted at a.b -- _package_of already returns the
        # containing package, so only levels beyond the first climb further.
        climb = level - 1
        if climb > len(parts):
            return None
        base = parts[: len(parts) - climb] if climb else parts

        if module:
            return ".".join([*base, module])
        return ".".join(base) if base else None

    def _package_of(self, file: Path) -> str | None:
        """Dotted name of the package containing ``file``, or ``None``."""
        file = file.resolve()
        # A package's own __init__.py sits inside the package it names, so
        # starting from the containing directory is right in both cases.
        directory = file.parent
        parts: list[str] = []
        current = directory
        while (current / "__init__.py").exists():
            parts.append(current.name)
            parent = current.parent
            if parent == current:
                break
            current = parent
        if not parts:
            return ""
        return ".".join(reversed(parts))

    def _resolve_uncached(self, name: str) -> ResolvedModule:
        if name in sys.builtin_module_names:
            return ResolvedModule(name, ModuleKind.BUILTIN, is_stdlib=True)

        parts = name.split(".")
        roots = [(root, True) for root in self.project_roots]
        roots += [(root, False) for root in self.search_paths]

        for root, first_party in roots:
            hit = self._search(root, parts)
            if hit is not None:
                kind, path = hit
                return ResolvedModule(
                    name=name,
                    kind=kind,
                    path=path,
                    is_first_party=first_party or self._under_project(path),
                    is_stdlib=self._under_stdlib(path),
                )

        if name in getattr(sys, "stdlib_module_names", frozenset()):
            # Frozen stdlib modules have no file on the search path.
            return ResolvedModule(name, ModuleKind.FROZEN, is_stdlib=True)
        return ResolvedModule(name, ModuleKind.NOT_FOUND)

    def _search(self, root: Path, parts: list[str]) -> tuple[ModuleKind, Path] | None:
        """Walk ``parts`` down from ``root`` on the filesystem."""
        current = root
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            candidate = current / part

            if last:
                for suffix in _SOURCE_SUFFIXES:
                    file = current / f"{part}{suffix}"
                    if file.is_file():
                        return ModuleKind.SOURCE, file
                for suffix in _EXTENSION_SUFFIXES:
                    file = current / f"{part}{suffix}"
                    if file.is_file():
                        return ModuleKind.EXTENSION, file
                if candidate.is_dir():
                    init = candidate / "__init__.py"
                    if init.is_file():
                        return ModuleKind.PACKAGE, init
                    return ModuleKind.NAMESPACE, candidate
                # A versioned extension such as foo.cpython-311-x86_64-linux-gnu.so.
                match = _glob_extension(current, part)
                if match is not None:
                    return ModuleKind.EXTENSION, match
                return None

            if not candidate.is_dir():
                return None
            current = candidate

        return None

    def _under_project(self, path: Path | None) -> bool:
        if path is None:
            return False
        return any(_is_relative_to(path, root) for root in self.project_roots)

    def _under_stdlib(self, path: Path | None) -> bool:
        if path is None:
            return False
        if "site-packages" in path.parts or "dist-packages" in path.parts:
            return False
        return any(_is_relative_to(path, d) for d in self._stdlib_dirs)


def _glob_extension(directory: Path, stem: str) -> Path | None:
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.is_file():
            continue
        name = entry.name
        if name.startswith(stem + ".") and name.endswith((".so", ".pyd", ".dylib")):
            return entry
    return None


def _is_relative_to(path: Path, other: Path) -> bool:
    # Path.is_relative_to landed in 3.9; this keeps the floor at 3.9 without a
    # version check at every call site.
    try:
        path.relative_to(other)
    except ValueError:
        return False
    return True


def _stdlib_dirs() -> list[Path]:
    dirs = []
    for key in ("stdlib", "platstdlib"):
        value = sysconfig.get_paths().get(key)
        if value:
            dirs.append(Path(value).resolve())
    return dirs
