"""Analyse one source file: where its imports are, and how they are used.

Two questions decide whether an import can become lazy, and this module answers
both for every import statement in a file.

**Is it allowed?**  PEP 810 restricts ``lazy`` to module scope: inside a
function, a class body, or a ``try``/``except``/``finally`` block it is a
:exc:`SyntaxError`, as are ``lazy from x import *`` and
``lazy from __future__ import ...``.  :class:`ImportContext` records where each
statement sits so those can be excluded outright.

**Is it worth it?**  A lazy import reifies the first time its bound name is
touched, so an import whose name is used at module level saves nothing -- the
proxy is resolved microseconds later.  The win comes from names used only inside
function bodies and annotations.  :class:`Usage` separates the two by walking the
file with a notion of *deferred context*: function bodies and lambda bodies run
later, and since PEP 649 so do annotations, while decorators, default arguments,
base classes and module-level statements all run immediately.

The third case matters most.  An import with *no* uses at all is usually
imported for its side effect (``import readline``) or re-exported from a package
``__init__``.  Making the first lazy does not delay the effect, it discards it,
because nothing ever touches the name.  Telling those two apart is what
:attr:`Usage.reexported` is for.
"""

from __future__ import annotations

import ast
import enum
from dataclasses import dataclass, field
from pathlib import Path

from ._syntax import StrippedSource, splitlines_keepends, strip_lazy

__all__ = [
    "ImportContext",
    "ImportBinding",
    "ImportSite",
    "Usage",
    "FileAnalysis",
    "analyze_source",
    "analyze_file",
]


class ImportContext(enum.Enum):
    """Where an import statement sits, which decides whether ``lazy`` is legal."""

    MODULE = "module"  #: Top level of the module: eligible.
    CONDITIONAL = "conditional"  #: Inside ``if``/``else`` at module level.
    TYPE_CHECKING = "type_checking"  #: Inside ``if TYPE_CHECKING:``; never runs.
    MAIN_GUARD = "main_guard"  #: Inside ``if __name__ == "__main__":``.
    FUNCTION = "function"  #: SyntaxError under PEP 810.
    CLASS = "class"  #: SyntaxError under PEP 810.
    TRY = "try"  #: SyntaxError under PEP 810.
    WITH = "with"  #: Not module scope in any useful sense; excluded.
    LOOP = "loop"  #: Ditto.
    MATCH = "match"  #: Ditto.

    @property
    def is_syntactically_eligible(self) -> bool:
        """Whether PEP 810 permits ``lazy`` here at all."""
        return self in (ImportContext.MODULE, ImportContext.CONDITIONAL)


@dataclass(frozen=True)
class ImportBinding:
    """One name an import statement binds into the module namespace."""

    name: str  #: The name bound locally.
    module: str  #: The module the statement pulls in (absolute where known).
    symbol: str | None = None  #: For ``from m import s``, the ``s``.
    #: True for ``import a.b`` without ``as``, which binds ``a`` but loads ``a.b``.
    is_submodule_chain: bool = False


@dataclass
class ImportSite:
    """A single ``import`` or ``from ... import`` statement."""

    lineno: int
    col: int  #: Column of the ``import``/``from`` keyword in the original source.
    end_lineno: int
    kind: str  #: ``"import"`` or ``"from"``.
    context: ImportContext
    bindings: list[ImportBinding]
    level: int = 0  #: Dots on a relative ``from`` import.
    module: str | None = None  #: Raw module text of a ``from`` import.
    already_lazy: bool = False
    is_star: bool = False
    is_future: bool = False
    source_line: str = ""

    @property
    def modules(self) -> list[str]:
        """Distinct modules this statement causes to be imported."""
        seen: dict[str, None] = {}
        for binding in self.bindings:
            seen.setdefault(binding.module, None)
        return list(seen)

    @property
    def names(self) -> list[str]:
        return [binding.name for binding in self.bindings]


@dataclass
class Usage:
    """How a bound name is used within its own module."""

    name: str
    eager: list[int] = field(default_factory=list)
    deferred: list[int] = field(default_factory=list)
    type_only: list[int] = field(default_factory=list)
    reexported: bool = False  #: Listed in ``__all__`` or re-exported ``as`` itself.
    shadowed: list[int] = field(default_factory=list)  #: Rebound at module level.
    deleted: list[int] = field(default_factory=list)

    @property
    def used_eagerly(self) -> bool:
        return bool(self.eager)

    @property
    def used_lazily(self) -> bool:
        return bool(self.deferred)

    @property
    def unused(self) -> bool:
        return not (self.eager or self.deferred or self.type_only)


@dataclass
class FileAnalysis:
    """The result of analysing one file."""

    path: Path
    module_name: str
    source: str
    stripped: StrippedSource
    sites: list[ImportSite] = field(default_factory=list)
    usages: dict[str, Usage] = field(default_factory=dict)
    #: True when the module inspects its own namespace dynamically, which makes
    #: any conclusion about "unused" names unreliable.
    dynamic_namespace: bool = False
    #: True when the module declares ``__all__``.  A package ``__init__`` that
    #: does not is assumed to re-export whatever it imports; one that does has
    #: told us exactly what it re-exports, and anything else is unused.
    has_dunder_all: bool = False
    syntax_error: str | None = None

    def usage_for(self, name: str) -> Usage:
        return self.usages.get(name, Usage(name=name))


class _ImportCollector(ast.NodeVisitor):
    """Find import statements and label the context each one sits in."""

    def __init__(self, stripped: StrippedSource, source_lines: list[str]) -> None:
        self.stripped = stripped
        self.source_lines = source_lines
        self.sites: list[ImportSite] = []
        self._context: list[ImportContext] = [ImportContext.MODULE]

    @property
    def context(self) -> ImportContext:
        return self._context[-1]

    def _push(self, context: ImportContext, body: list[ast.stmt]) -> None:
        self._context.append(context)
        for stmt in body:
            self.visit(stmt)
        self._context.pop()

    # -- scope-changing statements ------------------------------------------

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._push(ImportContext.FUNCTION, node.body)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._push(ImportContext.FUNCTION, node.body)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._push(ImportContext.CLASS, node.body)

    def visit_Try(self, node: ast.Try) -> None:
        self._push(ImportContext.TRY, node.body)
        for handler in node.handlers:
            self._push(ImportContext.TRY, handler.body)
        self._push(ImportContext.TRY, node.orelse)
        self._push(ImportContext.TRY, node.finalbody)

    def visit_TryStar(self, node: ast.stmt) -> None:  # pragma: no cover - 3.11+
        self.visit_Try(node)  # type: ignore[arg-type]

    def visit_With(self, node: ast.With) -> None:
        self._push(ImportContext.WITH, node.body)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self._push(ImportContext.WITH, node.body)

    def visit_For(self, node: ast.For) -> None:
        self._push(ImportContext.LOOP, node.body)
        self._push(ImportContext.LOOP, node.orelse)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self.visit_For(node)  # type: ignore[arg-type]

    def visit_While(self, node: ast.While) -> None:
        self._push(ImportContext.LOOP, node.body)
        self._push(ImportContext.LOOP, node.orelse)

    def visit_Match(self, node: ast.stmt) -> None:  # pragma: no cover - 3.10+
        for case in getattr(node, "cases", []):
            self._push(ImportContext.MATCH, case.body)

    def visit_If(self, node: ast.If) -> None:
        if _is_type_checking(node.test):
            self._push(ImportContext.TYPE_CHECKING, node.body)
            self._push(self._nested(ImportContext.CONDITIONAL), node.orelse)
            return
        if _is_main_guard(node.test):
            self._push(ImportContext.MAIN_GUARD, node.body)
            self._push(self._nested(ImportContext.CONDITIONAL), node.orelse)
            return
        nested = self._nested(ImportContext.CONDITIONAL)
        self._push(nested, node.body)
        self._push(nested, node.orelse)

    def _nested(self, wanted: ImportContext) -> ImportContext:
        """Keep the stronger label when nesting inside an ineligible context."""
        if self.context is ImportContext.MODULE:
            return wanted
        return self.context

    # -- the imports themselves ---------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        bindings = []
        for alias in node.names:
            if alias.asname:
                bindings.append(ImportBinding(alias.asname, alias.name))
            else:
                root = alias.name.split(".")[0]
                bindings.append(
                    ImportBinding(root, alias.name, is_submodule_chain="." in alias.name)
                )
        self._record(node, "import", bindings)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        prefix = "." * node.level
        target = f"{prefix}{module}"
        bindings = []
        is_star = False
        for alias in node.names:
            if alias.name == "*":
                is_star = True
                continue
            bindings.append(
                ImportBinding(
                    name=alias.asname or alias.name,
                    module=target,
                    symbol=alias.name,
                )
            )
        self._record(
            node,
            "from",
            bindings,
            level=node.level,
            module=module,
            is_star=is_star,
            is_future=module == "__future__",
        )

    def _record(
        self,
        node: ast.Import | ast.ImportFrom,
        kind: str,
        bindings: list[ImportBinding],
        level: int = 0,
        module: str | None = None,
        is_star: bool = False,
        is_future: bool = False,
    ) -> None:
        lineno = node.lineno
        col = self.stripped.original_col(lineno, node.col_offset)
        line = self.source_lines[lineno - 1] if lineno - 1 < len(self.source_lines) else ""
        self.sites.append(
            ImportSite(
                lineno=lineno,
                col=col,
                end_lineno=getattr(node, "end_lineno", lineno) or lineno,
                kind=kind,
                context=self.context,
                bindings=bindings,
                level=level,
                module=module,
                already_lazy=self.stripped.is_lazy(lineno),
                is_star=is_star,
                is_future=is_future,
                source_line=line.rstrip("\n"),
            )
        )


class _UsageCollector(ast.NodeVisitor):
    """Record every name load, tagged with whether it runs at import time."""

    def __init__(self) -> None:
        self.usages: dict[str, Usage] = {}
        self.dynamic_namespace = False
        self._defer = 0
        self._type_only = 0

    def usage(self, name: str) -> Usage:
        return self.usages.setdefault(name, Usage(name=name))

    # -- context helpers -----------------------------------------------------

    def _visit_deferred(self, nodes: list[ast.AST] | ast.AST | None) -> None:
        if nodes is None:
            return
        self._defer += 1
        for node in nodes if isinstance(nodes, list) else [nodes]:
            self.visit(node)
        self._defer -= 1

    def _visit_eager(self, nodes: list[ast.AST] | ast.AST | None) -> None:
        if nodes is None:
            return
        for node in nodes if isinstance(nodes, list) else [nodes]:
            self.visit(node)

    def _visit_annotation(self, node: ast.AST | None) -> None:
        """Annotations are lazily evaluated since PEP 649 (Python 3.14)."""
        if node is None:
            return
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            # A quoted annotation still names the module it needs.
            try:
                parsed = ast.parse(node.value, mode="eval")
            except SyntaxError:
                return
            ast.fix_missing_locations(parsed)
            for sub in ast.walk(parsed.body):
                if isinstance(sub, ast.Name):
                    self.usage(sub.id).type_only.append(getattr(node, "lineno", 0))
            return
        self._defer += 1
        self._type_only += 1
        self.visit(node)
        self._type_only -= 1
        self._defer -= 1

    # -- scopes --------------------------------------------------------------

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function(node)

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        # Decorators and defaults are evaluated when the `def` executes.
        self._visit_eager(list(node.decorator_list))
        args = node.args
        self._visit_eager([d for d in args.defaults])
        self._visit_eager([d for d in args.kw_defaults if d is not None])
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs,
                    *( [args.vararg] if args.vararg else [] ),
                    *( [args.kwarg] if args.kwarg else [] )]:
            self._visit_annotation(arg.annotation)
        self._visit_annotation(node.returns)
        self._visit_deferred(list(node.body))

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._visit_eager([d for d in node.args.defaults])
        self._visit_eager([d for d in node.args.kw_defaults if d is not None])
        self._visit_deferred(node.body)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        # Everything about a class runs when the statement runs.
        self._visit_eager(list(node.decorator_list))
        self._visit_eager(list(node.bases))
        self._visit_eager([kw.value for kw in node.keywords])
        self._visit_eager(list(node.body))

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._visit_annotation(node.annotation)
        self._visit_eager(node.value)
        self._visit_eager(node.target)

    def visit_If(self, node: ast.If) -> None:
        if _is_type_checking(node.test):
            # The guard itself is evaluated every run, so TYPE_CHECKING is a
            # genuine eager use of the name imported from `typing`.
            self._visit_eager(node.test)
            self._type_only += 1
            self._defer += 1
            self._visit_eager(list(node.body))
            self._defer -= 1
            self._type_only -= 1
            self._visit_eager(list(node.orelse))
            return
        self._visit_eager(node.test)
        self._visit_eager(list(node.body))
        self._visit_eager(list(node.orelse))

    # -- the leaves we care about --------------------------------------------

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in ("globals", "vars", "locals"):
            self.dynamic_namespace = True
        if isinstance(node.ctx, ast.Load):
            usage = self.usage(node.id)
            if self._type_only:
                usage.type_only.append(node.lineno)
            elif self._defer:
                usage.deferred.append(node.lineno)
            else:
                usage.eager.append(node.lineno)
        elif isinstance(node.ctx, ast.Store) and not self._defer:
            self.usage(node.id).shadowed.append(node.lineno)
        elif isinstance(node.ctx, ast.Del):
            self.usage(node.id).deleted.append(node.lineno)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            if isinstance(target, ast.Name):
                self.usage(target.id).deleted.append(node.lineno)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        # An import binds a name; that is not a use of it.
        return

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        return


def _collect_all(tree: ast.Module) -> set[str]:
    """Names listed in a module-level ``__all__``."""
    exported: set[str] = set()
    for stmt in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(stmt, ast.Assign):
            targets, value = stmt.targets, stmt.value
        elif isinstance(stmt, ast.AugAssign):
            targets, value = [stmt.target], stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets, value = [stmt.target], stmt.value
        if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
            continue
        for node in ast.walk(value) if value is not None else []:
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                exported.add(node.value)
    return exported


def _declares_all(tree: ast.Module) -> bool:
    """Whether the module assigns ``__all__`` at its top level."""
    for stmt in tree.body:
        targets: list[ast.expr] = []
        if isinstance(stmt, ast.Assign):
            targets = list(stmt.targets)
        elif isinstance(stmt, (ast.AugAssign, ast.AnnAssign)):
            targets = [stmt.target]
        if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
            return True
    return False


def analyze_source(source: str, path: Path, module_name: str = "") -> FileAnalysis:
    """Analyse source text that may contain PEP 810 ``lazy`` prefixes."""
    stripped = strip_lazy(source)
    analysis = FileAnalysis(
        path=path,
        module_name=module_name,
        source=source,
        stripped=stripped,
    )
    try:
        tree = ast.parse(stripped.text, filename=str(path))
    except SyntaxError as exc:
        analysis.syntax_error = f"{exc.msg} (line {exc.lineno})"
        return analysis

    lines = splitlines_keepends(source)
    collector = _ImportCollector(stripped, lines)
    for stmt in tree.body:
        collector.visit(stmt)
    analysis.sites = collector.sites

    usage_collector = _UsageCollector()
    for stmt in tree.body:
        usage_collector.visit(stmt)
    analysis.usages = usage_collector.usages
    analysis.dynamic_namespace = usage_collector.dynamic_namespace

    exported = _collect_all(tree)
    analysis.has_dunder_all = _declares_all(tree)
    for site in analysis.sites:
        for binding in site.bindings:
            usage = analysis.usages.setdefault(binding.name, Usage(name=binding.name))
            if binding.name in exported:
                usage.reexported = True
            # `from x import y as y` is the typing convention for an explicit
            # re-export, and so is any import inside a package's __init__.
            if binding.symbol is not None and binding.symbol == binding.name:
                if _is_explicit_reexport(site, binding):
                    usage.reexported = True

    return analysis


def _is_explicit_reexport(site: ImportSite, binding: ImportBinding) -> bool:
    """Whether ``import x as x`` was written to mean "re-export x"."""
    if site.kind != "from":
        return False
    line = site.source_line
    return f"{binding.symbol} as {binding.name}" in line


def analyze_file(path: Path, module_name: str = "") -> FileAnalysis:
    """Read and analyse a file, tolerating unreadable or undecodable input."""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        analysis = FileAnalysis(
            path=path,
            module_name=module_name,
            source="",
            stripped=strip_lazy(""),
        )
        analysis.syntax_error = f"could not read file: {exc}"
        return analysis
    return analyze_source(source, path, module_name)


def _is_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _is_main_guard(test: ast.expr) -> bool:
    if not isinstance(test, ast.Compare) or len(test.comparators) != 1:
        return False
    right = test.comparators[0]
    return (
        isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and isinstance(right, ast.Constant)
        and right.value == "__main__"
    )
