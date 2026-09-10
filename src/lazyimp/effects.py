"""Detect what a module does while it is being imported.

PEP 810 says a static analyser "could detect modules with side effects and
automatically configure filters".  This module is that detector.

The unit of analysis is a module's top level: the statements that run the moment
the module is first imported.  Function and class *bodies* are skipped -- they
run later, if ever -- but decorators, base classes and default arguments are
evaluated when the ``def`` executes, so those are analysed.

Deferring an import also defers everything that import pulls in, so the walk is
transitive: ``import a`` where ``a`` imports ``b`` where ``b`` registers a codec
means making ``import a`` lazy delays the codec registration.  :class:`EffectAnalyzer`
follows those eager edges to a bounded depth and reports the chain that led to
each finding, so a warning always comes with the path that produced it.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

from ._pep810 import strip_lazy
from .knowledge import (
    Confidence,
    EffectKind,
    PURE_DECORATORS,
    PURE_MODULES,
    lookup_call,
    side_effect_reason,
)
from .resolver import ModuleKind, ModuleResolver

__all__ = ["Effect", "ModuleEffects", "EffectAnalyzer", "scan_source"]

#: How far to follow eager imports when accumulating transitive effects.  Deep
#: enough to cross a package boundary or two, shallow enough that analysing a
#: large dependency does not walk the whole world.
DEFAULT_MAX_DEPTH = 3


@dataclass(frozen=True)
class Effect:
    """One import-time side effect, with where it came from."""

    kind: EffectKind
    confidence: Confidence
    detail: str
    module: str
    lineno: int = 0
    #: Modules traversed to reach this effect, starting at the import site's
    #: target.  Empty when the effect is in the target module itself.
    via: tuple[str, ...] = ()

    def describe(self) -> str:
        location = f"{self.module}:{self.lineno}" if self.lineno else self.module
        chain = " -> ".join([*self.via, self.module]) if self.via else location
        if self.via:
            return f"{self.detail} ({chain}:{self.lineno})"
        return f"{self.detail} ({location})"


@dataclass
class ModuleEffects:
    """Everything the scanner learned about one module's top level."""

    module: str
    effects: list[Effect] = field(default_factory=list)
    #: Modules this one imports eagerly at its top level.
    eager_imports: list[str] = field(default_factory=list)
    #: Modules this one already imports lazily; those do not propagate effects.
    lazy_imports: list[str] = field(default_factory=list)
    analysable: bool = True


class _TopLevelScanner(ast.NodeVisitor):
    """Classify the statements that execute when a module is imported."""

    def __init__(self, module: str, lazy_lines: set[int]) -> None:
        self.module = module
        self.lazy_lines = lazy_lines
        self.effects: list[Effect] = []
        self.eager_imports: list[str] = []
        self.lazy_imports: list[str] = []
        # A `raise` or `sys.exit` guarded by a module-level `if` is a platform
        # or version check, not an unconditional failure.  Nearly every stdlib
        # module has one, so counting them as high confidence would make the
        # whole standard library look dangerous.
        self._conditional_depth = 0
        # Alias map so `from os import path as p` lets `p.join` resolve to
        # `os.path.join` in the call tables.
        self.aliases: dict[str, str] = {}

    # -- entry point --------------------------------------------------------

    def scan(self, tree: ast.Module) -> None:
        self._scan_body(tree.body)

    def _scan_body(self, body: list[ast.stmt]) -> None:
        for stmt in body:
            self._scan_stmt(stmt)

    def _scan_stmt(self, node: ast.stmt) -> None:
        handler = getattr(self, f"_stmt_{type(node).__name__}", None)
        if handler is not None:
            handler(node)
        else:
            self._add(
                EffectKind.CONTROL_FLOW,
                Confidence.LOW,
                f"{type(node).__name__} statement at module level",
                node.lineno,
            )

    # -- statements that are inert ------------------------------------------

    def _stmt_Pass(self, node: ast.Pass) -> None:
        pass

    def _stmt_Global(self, node: ast.Global) -> None:
        pass

    def _stmt_Nonlocal(self, node: ast.Nonlocal) -> None:
        pass

    # -- imports ------------------------------------------------------------

    def _stmt_Import(self, node: ast.Import) -> None:
        lazy = node.lineno in self.lazy_lines
        for alias in node.names:
            bound = alias.asname or alias.name.split(".")[0]
            self.aliases[bound] = alias.name if alias.asname else alias.name.split(".")[0]
            (self.lazy_imports if lazy else self.eager_imports).append(alias.name)

    def _stmt_ImportFrom(self, node: ast.ImportFrom) -> None:
        lazy = node.lineno in self.lazy_lines
        if node.level:
            # Relative targets are resolved by the caller, which knows the file.
            target = "." * node.level + (node.module or "")
        else:
            target = node.module or ""
        for alias in node.names:
            bound = alias.asname or alias.name
            if target and not node.level:
                self.aliases[bound] = f"{target}.{alias.name}"
        if target:
            (self.lazy_imports if lazy else self.eager_imports).append(target)

    # -- definitions --------------------------------------------------------

    def _stmt_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._definition(node)

    def _stmt_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._definition(node)

    def _stmt_ClassDef(self, node: ast.ClassDef) -> None:
        self._definition(node)
        # A base class from elsewhere may register subclasses via
        # __init_subclass__ or a metaclass; we cannot see that from here.
        for base in node.bases:
            dotted = _dotted(base)
            if dotted and "." in dotted:
                self._add(
                    EffectKind.REGISTRATION,
                    Confidence.LOW,
                    f"class {node.name} subclasses {dotted}, which may self-register",
                    node.lineno,
                )
                break

    def _definition(self, node: ast.stmt) -> None:
        """Decorators and signature defaults run; the body does not."""
        for decorator in getattr(node, "decorator_list", []):
            dotted = _dotted(decorator) or _dotted(getattr(decorator, "func", None))
            if dotted is None:
                continue
            resolved = self._resolve(dotted)
            if resolved in PURE_DECORATORS or dotted in PURE_DECORATORS:
                continue
            self._add(
                EffectKind.DECORATOR,
                Confidence.MEDIUM if "." in dotted else Confidence.LOW,
                f"@{dotted} runs at import time and may register {_name_of(node)!r}",
                node.lineno,
            )
        args = getattr(node, "args", None)
        if args is not None:
            for default in [*args.defaults, *[d for d in args.kw_defaults if d is not None]]:
                self._scan_expr(default, node.lineno)

    # -- assignments --------------------------------------------------------

    def _stmt_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._assign_target(target, node.lineno)
        self._scan_expr(node.value, node.lineno)

    def _stmt_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._assign_target(node.target, node.lineno)
        if node.value is not None:
            self._scan_expr(node.value, node.lineno)

    def _stmt_AugAssign(self, node: ast.AugAssign) -> None:
        self._assign_target(node.target, node.lineno, augmented=True)
        self._scan_expr(node.value, node.lineno)

    def _assign_target(self, target: ast.expr, lineno: int, augmented: bool = False) -> None:
        if isinstance(target, ast.Attribute):
            dotted = _dotted(target)
            owner = dotted.rsplit(".", 1)[0] if dotted and "." in dotted else None
            if owner and owner in self.aliases:
                self._add(
                    EffectKind.MONKEYPATCH,
                    Confidence.HIGH,
                    f"assigns to {dotted}, mutating another module",
                    lineno,
                )
            else:
                self._add(
                    EffectKind.MUTATION,
                    Confidence.LOW,
                    f"assigns to attribute {dotted or '<expr>'}",
                    lineno,
                )
        elif isinstance(target, ast.Subscript):
            dotted = _dotted(target.value)
            if dotted and dotted.startswith(("os.environ", "sys.modules")):
                self._add(
                    EffectKind.GLOBAL_CONFIG,
                    Confidence.HIGH,
                    f"writes to {dotted} at import time",
                    lineno,
                )
            else:
                self._add(
                    EffectKind.MUTATION,
                    Confidence.MEDIUM if augmented else Confidence.LOW,
                    f"mutates {dotted or '<expr>'} by subscript",
                    lineno,
                )
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._assign_target(element, lineno, augmented)

    def _stmt_Delete(self, node: ast.Delete) -> None:
        # Almost always `del _helper` tidying the module namespace.  A `del` of a
        # name *this* file imported is a real hazard, but that is the importing
        # side's problem and the verdict layer reports it as E202.
        self._add(
            EffectKind.MUTATION,
            Confidence.LOW,
            "deletes a module-level name at import time",
            node.lineno,
        )

    # -- expressions and control flow ---------------------------------------

    def _stmt_Expr(self, node: ast.Expr) -> None:
        if isinstance(node.value, ast.Constant):
            return  # A docstring or a stray literal.
        self._scan_expr(node.value, node.lineno)

    def _stmt_If(self, node: ast.If) -> None:
        if _is_type_checking(node.test):
            return  # Never executes at runtime.
        if _is_main_guard(node.test):
            return  # Never executes on import.
        self._scan_expr(node.test, node.lineno)
        self._conditional_depth += 1
        self._scan_body(node.body)
        self._scan_body(node.orelse)
        self._conditional_depth -= 1

    def _stmt_Try(self, node: ast.Try) -> None:
        # An import guarded by try/except is a compatibility shim; the guard
        # itself is not an effect, but everything inside still runs.
        self._scan_body(node.body)
        for handler in node.handlers:
            self._scan_body(handler.body)
        self._scan_body(node.orelse)
        self._scan_body(node.finalbody)

    if hasattr(ast, "TryStar"):  # pragma: no cover - 3.11+

        def _stmt_TryStar(self, node: ast.stmt) -> None:
            self._stmt_Try(node)  # type: ignore[arg-type]

    def _stmt_For(self, node: ast.For) -> None:
        self._add(
            EffectKind.CONTROL_FLOW,
            Confidence.LOW,
            "runs a for loop at import time",
            node.lineno,
        )
        self._scan_expr(node.iter, node.lineno)
        self._scan_body(node.body)
        self._scan_body(node.orelse)

    def _stmt_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._stmt_For(node)  # type: ignore[arg-type]

    def _stmt_While(self, node: ast.While) -> None:
        self._add(
            EffectKind.CONTROL_FLOW,
            Confidence.LOW,
            "runs a while loop at import time",
            node.lineno,
        )
        self._scan_body(node.body)
        self._scan_body(node.orelse)

    def _stmt_With(self, node: ast.With) -> None:
        self._add(
            EffectKind.CONTROL_FLOW,
            Confidence.LOW,
            "enters a context manager at import time",
            node.lineno,
        )
        for item in node.items:
            self._scan_expr(item.context_expr, node.lineno)
        self._scan_body(node.body)

    def _stmt_AsyncWith(self, node: ast.AsyncWith) -> None:
        self._stmt_With(node)  # type: ignore[arg-type]

    def _stmt_Raise(self, node: ast.Raise) -> None:
        if self._conditional_depth:
            self._add(
                EffectKind.CONTROL_FLOW,
                Confidence.LOW,
                "raises at import time under a conditional guard",
                node.lineno,
            )
            return
        self._add(
            EffectKind.EXIT,
            Confidence.HIGH,
            "raises unconditionally at import time",
            node.lineno,
        )

    def _stmt_Assert(self, node: ast.Assert) -> None:
        self._add(
            EffectKind.CONTROL_FLOW,
            Confidence.LOW,
            "asserts at import time",
            node.lineno,
        )

    def _stmt_Match(self, node: ast.stmt) -> None:  # pragma: no cover - 3.10+
        self._add(
            EffectKind.CONTROL_FLOW,
            Confidence.MEDIUM,
            "runs a match statement at import time",
            node.lineno,
        )
        for case in getattr(node, "cases", []):
            self._scan_body(case.body)

    # -- expression walking --------------------------------------------------

    def _scan_expr(self, node: ast.expr | None, lineno: int) -> None:
        if node is None:
            return
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                self._scan_call(child, lineno)
            elif isinstance(child, ast.NamedExpr):
                self._add(
                    EffectKind.MUTATION,
                    Confidence.LOW,
                    "walrus assignment at import time",
                    getattr(child, "lineno", lineno),
                )

    def _scan_call(self, node: ast.Call, lineno: int) -> None:
        dotted = _dotted(node.func)
        if dotted is None:
            return
        resolved = self._resolve(dotted)
        verdict = lookup_call(resolved)
        if verdict is None:
            return
        kind, confidence = verdict
        detail = (
            f"calls {resolved}()"
            if kind is not EffectKind.UNKNOWN_CALL
            else f"calls {resolved}(), which we cannot classify"
        )
        self._add(kind, confidence, detail, getattr(node, "lineno", lineno))

    def _resolve(self, dotted: str) -> str:
        """Expand the leading segment of ``dotted`` through the alias map."""
        head, _, tail = dotted.partition(".")
        target = self.aliases.get(head)
        if target is None:
            return dotted
        return f"{target}.{tail}" if tail else target

    def _add(self, kind: EffectKind, confidence: Confidence, detail: str, lineno: int) -> None:
        self.effects.append(
            Effect(
                kind=kind,
                confidence=confidence,
                detail=detail,
                module=self.module,
                lineno=lineno,
            )
        )


def scan_source(source: str, module: str = "<unknown>") -> ModuleEffects:
    """Scan module source text for import-time side effects."""
    stripped = strip_lazy(source)
    try:
        tree = ast.parse(stripped.text)
    except SyntaxError as exc:
        return ModuleEffects(
            module=module,
            effects=[
                Effect(
                    kind=EffectKind.OPAQUE,
                    confidence=Confidence.MEDIUM,
                    detail=f"could not be parsed ({exc.msg})",
                    module=module,
                    lineno=exc.lineno or 0,
                )
            ],
            analysable=False,
        )

    scanner = _TopLevelScanner(module, set(stripped.prefixes))
    scanner.scan(tree)
    return ModuleEffects(
        module=module,
        effects=scanner.effects,
        eager_imports=scanner.eager_imports,
        lazy_imports=scanner.lazy_imports,
    )


@dataclass
class EffectAnalyzer:
    """Accumulate a module's own effects plus those of its eager imports."""

    resolver: ModuleResolver
    max_depth: int = DEFAULT_MAX_DEPTH
    #: Skip modules that ship with CPython.  Their import-time behaviour is
    #: stable and mostly harmless, and walking them dominates run time.
    skip_stdlib: bool = True
    _scans: dict[str, ModuleEffects] = field(default_factory=dict, repr=False)
    _transitive: dict[tuple[str, int], list[Effect]] = field(default_factory=dict, repr=False)

    def scan_module(self, module: str) -> ModuleEffects:
        """Scan a single module by name, without following its imports."""
        if module in self._scans:
            return self._scans[module]

        resolved = self.resolver.resolve(module)
        if resolved.kind is ModuleKind.EXTENSION:
            result = ModuleEffects(
                module=module,
                effects=[
                    Effect(
                        EffectKind.OPAQUE,
                        Confidence.MEDIUM,
                        "is a compiled extension, so its import-time behaviour is not visible to static analysis",
                        module,
                    )
                ],
                analysable=False,
            )
        elif resolved.kind in (ModuleKind.BUILTIN, ModuleKind.FROZEN):
            result = ModuleEffects(module=module, analysable=False)
        elif resolved.kind is ModuleKind.NAMESPACE:
            result = ModuleEffects(module=module)
        elif resolved.is_analysable:
            assert resolved.path is not None
            result = self._scan_path(resolved.path, module)
        else:
            result = ModuleEffects(
                module=module,
                effects=[
                    Effect(
                        EffectKind.OPAQUE,
                        Confidence.LOW,
                        "could not be located on the search path",
                        module,
                    )
                ],
                analysable=False,
            )

        self._scans[module] = result
        return result

    def _scan_path(self, path: Path, module: str) -> ModuleEffects:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ModuleEffects(
                module=module,
                effects=[
                    Effect(EffectKind.OPAQUE, Confidence.LOW, f"could not be read ({exc})", module)
                ],
                analysable=False,
            )
        result = scan_source(source, module)
        result.eager_imports = [
            absolute
            for target in result.eager_imports
            if (absolute := self._absolutise(target, path)) is not None
        ]
        return result

    def _absolutise(self, target: str, importer: Path) -> str | None:
        if not target.startswith("."):
            return target
        level = len(target) - len(target.lstrip("."))
        return self.resolver.resolve_from(target[level:], level, importer)

    def effects_of(self, module: str) -> list[Effect]:
        """Effects of importing ``module``, including its eager dependencies."""
        return self._walk(module, depth=self.max_depth, seen=set(), via=())

    def _walk(
        self, module: str, depth: int, seen: set[str], via: tuple[str, ...]
    ) -> list[Effect]:
        if module in seen or depth < 0:
            return []
        if module in PURE_MODULES:
            # Curated as effect-free, which is what makes the walk affordable:
            # `typing` and `dataclasses` sit under a large share of all imports.
            return []
        cached = self._transitive.get((module, depth))
        if cached is not None and not via:
            return cached

        seen = seen | {module}
        collected: list[Effect] = []

        reason = side_effect_reason(module)
        if reason is not None:
            collected.append(
                Effect(
                    kind=EffectKind.REGISTRATION,
                    confidence=Confidence.HIGH,
                    detail=reason,
                    module=module,
                    via=via,
                )
            )

        scan = self.scan_module(module)
        for effect in scan.effects:
            collected.append(
                Effect(
                    kind=effect.kind,
                    confidence=effect.confidence,
                    detail=effect.detail,
                    module=effect.module,
                    lineno=effect.lineno,
                    via=via,
                )
            )

        if depth > 0:
            for target in scan.eager_imports:
                if self._should_skip(target):
                    continue
                collected.extend(self._walk(target, depth - 1, seen, (*via, module)))

        if not via:
            self._transitive[(module, depth)] = collected
        return collected

    def _should_skip(self, module: str) -> bool:
        root = module.split(".")[0]
        if module in PURE_MODULES or root in PURE_MODULES:
            return True
        if not self.skip_stdlib:
            return False
        resolved = self.resolver.resolve(module)
        return resolved.is_stdlib


def _dotted(node: ast.AST | None) -> str | None:
    """Render ``a.b.c`` / ``a.b.c()`` as a dotted string, or ``None``."""
    if node is None:
        return None
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        if isinstance(node.value, ast.Call):
            # `f().attr` is not a dotted name; pretending otherwise invents
            # callees like `__import__.compile` that match nothing meaningful.
            return None
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    if isinstance(node, ast.Subscript):
        return _dotted(node.value)
    return None


def _name_of(node: ast.stmt) -> str:
    return getattr(node, "name", "<anonymous>")


def _is_type_checking(test: ast.expr) -> bool:
    dotted = _dotted(test)
    return dotted in ("TYPE_CHECKING", "typing.TYPE_CHECKING", "t.TYPE_CHECKING")


def _is_main_guard(test: ast.expr) -> bool:
    if not isinstance(test, ast.Compare) or len(test.comparators) != 1:
        return False
    left = _dotted(test.left)
    right = test.comparators[0]
    return (
        left == "__name__"
        and isinstance(right, ast.Constant)
        and right.value == "__main__"
    )
