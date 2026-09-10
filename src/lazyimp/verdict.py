"""Turn the analysis signals into a decision about one import statement.

Three inputs meet here: where the statement sits (:mod:`lazyimp.analyzer`), how
its bound names are used in the same file, and what importing its target
actually does (:mod:`lazyimp.effects`).  The output is a :class:`Decision` plus
the reasons behind it, each carrying a stable code so a project can silence an
individual class of finding in CI the way it would silence a lint rule.

The ordering of the checks encodes the priority:

1. **Legality.**  PEP 810 forbids ``lazy`` outside module scope, on star imports
   and on ``__future__`` imports.  No amount of benefit overrides a SyntaxError.
2. **Semantics.**  An import whose name is never used exists for its side
   effect; deferring it means it never happens.  That is a behaviour change, not
   an optimisation, so it is rejected outright rather than merely warned about.
3. **Target effects.**  Importing the target may register a codec, patch a
   module or write a file.  Laziness delays those to first use, which is usually
   fine and occasionally catastrophic -- so it is surfaced as a risk to review,
   not an automatic rejection.
4. **Benefit.**  Whatever is left is legal and safe; the last question is
   whether it buys anything.

Reason codes are grouped by first letter: ``E`` blocks the rewrite, ``W`` marks
it risky, and ``I`` is informational.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from .analyzer import FileAnalysis, ImportContext, ImportSite, Usage
from .effects import Effect, EffectAnalyzer
from .knowledge import Confidence, EffectKind, is_startup_module
from .resolver import ModuleKind, ModuleResolver

__all__ = ["Decision", "Reason", "Verdict", "Policy", "judge"]


class Decision(enum.Enum):
    """What the tool recommends for one import statement."""

    SAFE = "safe"  #: Convert it; deferring changes nothing observable.
    LOW_BENEFIT = "low-benefit"  #: Legal and safe, but reified immediately anyway.
    RISKY = "risky"  #: Legal, but importing the target has side effects.
    UNSAFE = "unsafe"  #: Deferring would change behaviour.
    INELIGIBLE = "ineligible"  #: PEP 810 does not allow ``lazy`` here.
    ALREADY_LAZY = "already-lazy"  #: Nothing to do.
    SKIPPED = "skipped"  #: Never executes at runtime, so laziness is moot.

    @property
    def is_actionable(self) -> bool:
        """Whether a codemod should rewrite this statement by default."""
        return self is Decision.SAFE


@dataclass(frozen=True)
class Reason:
    """One justification for a decision."""

    code: str
    message: str
    confidence: Confidence = Confidence.HIGH

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


@dataclass
class Verdict:
    """The decision for one import statement, with its supporting reasons."""

    site: ImportSite
    decision: Decision
    reasons: list[Reason] = field(default_factory=list)
    effects: list[Effect] = field(default_factory=list)

    @property
    def codes(self) -> list[str]:
        return [reason.code for reason in self.reasons]

    @property
    def headline(self) -> str:
        return self.reasons[0].message if self.reasons else self.decision.value


@dataclass
class Policy:
    """Tunables that decide how conservative the analysis is."""

    #: Effects at or above this confidence make an import risky rather than safe.
    block_at: Confidence = Confidence.MEDIUM
    #: Allow rewriting imports nested in a module-level ``if``.  PEP 810 permits
    #: module scope, and an ``if`` body at module level is module scope, but the
    #: PEP spells out only the illegal contexts -- so this stays switchable.
    allow_conditional: bool = True
    #: Treat a name that is only re-exported as worth deferring.  Consumers
    #: reify it on attribute access, so the deferral survives the re-export.
    allow_reexport: bool = True
    #: Consider imports of modules that ship with CPython.
    include_stdlib: bool = True
    #: Consider imports whose names are used at module level anyway.
    include_low_benefit: bool = False
    #: Reason codes to ignore entirely.
    ignore: frozenset[str] = frozenset()


def judge(
    site: ImportSite,
    analysis: FileAnalysis,
    effect_analyzer: EffectAnalyzer | None = None,
    policy: Policy | None = None,
) -> Verdict:
    """Decide what should happen to one import statement."""
    policy = policy or Policy()
    reasons: list[Reason] = []

    if site.already_lazy:
        return Verdict(site, Decision.ALREADY_LAZY, [Reason("I400", "already lazy")])

    # --- 1. Legality --------------------------------------------------------

    if site.is_future:
        return Verdict(
            site,
            Decision.INELIGIBLE,
            [Reason("E103", "`lazy from __future__ import ...` is a SyntaxError")],
        )
    if site.is_star:
        return Verdict(
            site,
            Decision.INELIGIBLE,
            [Reason("E102", "`lazy from ... import *` is a SyntaxError")],
        )
    if site.context in (ImportContext.TYPE_CHECKING, ImportContext.MAIN_GUARD):
        label = (
            "inside `if TYPE_CHECKING:`, which never runs at runtime"
            if site.context is ImportContext.TYPE_CHECKING
            else "inside a `__main__` guard, which does not run on import"
        )
        return Verdict(site, Decision.SKIPPED, [Reason("I402", label)])
    if not site.context.is_syntactically_eligible:
        return Verdict(
            site,
            Decision.INELIGIBLE,
            [
                Reason(
                    "E101",
                    f"`lazy` is only allowed at module scope; this import is in a "
                    f"{site.context.value} block",
                )
            ],
        )
    if site.context is ImportContext.CONDITIONAL and not policy.allow_conditional:
        return Verdict(
            site,
            Decision.INELIGIBLE,
            [Reason("E104", "nested in a module-level `if` and conditionals are disabled")],
        )

    # --- 2. Semantics of this file -----------------------------------------

    usages = [analysis.usage_for(binding.name) for binding in site.bindings]

    blocking = _semantic_blockers(site, usages, analysis, policy)
    if blocking:
        return Verdict(site, Decision.UNSAFE, blocking)

    # --- 3. Benefit ---------------------------------------------------------
    #
    # Checked before effect analysis, which is the expensive step: an import
    # whose name is used at module level is reified immediately, so whether its
    # target has side effects makes no difference to the recommendation.

    benefit_reasons = _benefit(site, usages, policy)
    if not policy.include_low_benefit and any(r.code == "I401" for r in benefit_reasons):
        return Verdict(site, Decision.LOW_BENEFIT, _filter(benefit_reasons, policy))

    # --- 4. What importing the target does ----------------------------------

    effects: list[Effect] = []
    targets = (
        _targets(site, analysis, effect_analyzer.resolver)
        if effect_analyzer is not None
        else []
    )
    if targets and all(is_startup_module(module) for module in targets):
        # Already in sys.modules before this file runs, so the proxy would
        # resolve against a module that is loaded either way.
        return Verdict(
            site,
            Decision.LOW_BENEFIT,
            [
                Reason(
                    "I406",
                    f"{', '.join(targets)} is imported during interpreter startup, "
                    f"so deferring it saves nothing",
                    Confidence.MEDIUM,
                )
            ],
        )
    if effect_analyzer is not None:
        for module in targets:
            if not policy.include_stdlib:
                resolved = effect_analyzer.resolver.resolve(module)
                if resolved.is_stdlib:
                    reasons.append(
                        Reason("I404", f"{module} is part of the standard library",
                               Confidence.LOW)
                    )
                    continue
            effects.extend(effect_analyzer.effects_of(module))

    risky = [effect for effect in effects if effect.confidence >= policy.block_at]
    risky = [effect for effect in risky if effect.kind is not EffectKind.UNKNOWN_CALL]

    if risky:
        top = sorted(risky, key=lambda e: -int(e.confidence))[:3]
        reasons = [
            Reason(
                _code_for(effect.kind),
                f"importing {effect.module} {effect.detail}"
                if not effect.via
                else f"importing {' -> '.join(effect.via)} reaches {effect.module}, which {effect.detail}",
                effect.confidence,
            )
            for effect in top
        ] + reasons + benefit_reasons
        reasons = _filter(reasons, policy)
        if not any(reason.code.startswith("W") for reason in reasons):
            # Everything risky was explicitly ignored, so fall through to safe.
            return _safe_or_low_benefit(site, reasons, effects, benefit_reasons, policy)
        return Verdict(site, Decision.RISKY, reasons, effects)

    reasons = _filter(reasons + benefit_reasons, policy)
    return _safe_or_low_benefit(site, reasons, effects, benefit_reasons, policy)


def _safe_or_low_benefit(
    site: ImportSite,
    reasons: list[Reason],
    effects: list[Effect],
    benefit_reasons: list[Reason],
    policy: Policy,
) -> Verdict:
    low = any(reason.code == "I401" for reason in benefit_reasons)
    if low and not policy.include_low_benefit:
        return Verdict(site, Decision.LOW_BENEFIT, reasons, effects)
    if not reasons:
        reasons = [Reason("S500", "no import-time side effects found; safe to defer")]
    return Verdict(site, Decision.SAFE, reasons, effects)


def _semantic_blockers(
    site: ImportSite,
    usages: list[Usage],
    analysis: FileAnalysis,
    policy: Policy,
) -> list[Reason]:
    """Checks that reject a rewrite because behaviour would change."""
    blockers: list[Reason] = []

    for usage in usages:
        if usage.deleted:
            blockers.append(
                Reason(
                    "E202",
                    f"`{usage.name}` is deleted at module level (line "
                    f"{usage.deleted[0]}); a lazy import would be reified by the del",
                )
            )
        if usage.shadowed:
            blockers.append(
                Reason(
                    "E203",
                    f"`{usage.name}` is rebound at module level (line "
                    f"{usage.shadowed[0]}), so the import may be a fallback",
                )
            )

    unused = [usage for usage in usages if usage.unused and not usage.reexported]
    if unused and not _looks_reexporting(analysis):
        names = ", ".join(f"`{usage.name}`" for usage in unused)
        blockers.append(
            Reason(
                "E201",
                f"{names} is never used in this module, so the import exists for its "
                f"side effect -- deferring it would cancel the effect, not delay it",
            )
        )

    if analysis.dynamic_namespace and any(usage.unused for usage in usages):
        blockers.append(
            Reason(
                "E204",
                "this module inspects its own namespace with globals()/vars(), so "
                "unused-name analysis is unreliable here",
                Confidence.MEDIUM,
            )
        )

    return [reason for reason in blockers if reason.code not in policy.ignore]


def _looks_reexporting(analysis: FileAnalysis) -> bool:
    """Whether an unused name in this file is probably an implicit re-export.

    A package ``__init__`` with no ``__all__`` re-exports whatever it imports,
    so an unused name there proves nothing.  One that *does* declare ``__all__``
    has already said what it re-exports, and a name missing from that list is
    genuinely unused -- which usually means it was imported for its side effect.
    """
    return analysis.path.name == "__init__.py" and not analysis.has_dunder_all


def _benefit(site: ImportSite, usages: list[Usage], policy: Policy) -> list[Reason]:
    reasons: list[Reason] = []
    eager = [usage for usage in usages if usage.used_eagerly]
    if eager:
        names = ", ".join(f"`{usage.name}`" for usage in eager)
        reasons.append(
            Reason(
                "I401",
                f"{names} is used at module level (line {eager[0].eager[0]}), so the "
                f"lazy import would be reified immediately",
                Confidence.MEDIUM,
            )
        )
        return reasons

    if all(usage.type_only and not usage.deferred for usage in usages if not usage.unused):
        used = [usage for usage in usages if usage.type_only]
        if used:
            reasons.append(
                Reason("I403", "used only in annotations, which are lazy since PEP 649",
                       Confidence.LOW)
            )
    if any(usage.reexported for usage in usages):
        reasons.append(
            Reason("I405", "re-exported; consumers reify it on attribute access",
                   Confidence.LOW)
        )
    return reasons


def _targets(site: ImportSite, analysis: FileAnalysis, resolver: ModuleResolver) -> list[str]:
    """Absolute module names importing this statement would load.

    Two cases need care.  A relative import carries no absolute name at all, so
    it is resolved against the importing file's package -- and skipping that
    step would blind the analysis to first-party side effects, which is exactly
    where a project's own registration code lives.

    And ``from pkg import name`` loads ``pkg.name`` when ``name`` is a submodule
    but only ``pkg`` when it is an attribute.  Since the difference is not
    visible in the syntax, both are checked against the filesystem.
    """
    targets: list[str] = []

    if site.kind == "import":
        targets = [binding.module for binding in site.bindings]
    else:
        base = site.module or ""
        if site.level:
            base = resolver.resolve_from(base, site.level, analysis.path) or ""
        if base:
            targets.append(base)
            for binding in site.bindings:
                if not binding.symbol:
                    continue
                candidate = f"{base}.{binding.symbol}"
                if resolver.resolve(candidate).kind is not ModuleKind.NOT_FOUND:
                    targets.append(candidate)

    seen: dict[str, None] = {}
    for target in targets:
        if target and not target.startswith("."):
            seen.setdefault(target, None)
    return list(seen)


_CODES: dict[EffectKind, str] = {
    EffectKind.REGISTRATION: "W301",
    EffectKind.MONKEYPATCH: "W302",
    EffectKind.GLOBAL_CONFIG: "W303",
    EffectKind.IO: "W304",
    EffectKind.OUTPUT: "W305",
    EffectKind.CONCURRENCY: "W306",
    EffectKind.EXIT: "W307",
    EffectKind.RANDOMNESS: "W308",
    EffectKind.DECORATOR: "W309",
    EffectKind.DYNAMIC_IMPORT: "W310",
    EffectKind.CONTROL_FLOW: "W311",
    EffectKind.MUTATION: "W312",
    EffectKind.OPAQUE: "W313",
    EffectKind.UNKNOWN_CALL: "W314",
}


def _code_for(kind: EffectKind) -> str:
    return _CODES.get(kind, "W399")


def _filter(reasons: list[Reason], policy: Policy) -> list[Reason]:
    seen: set[str] = set()
    kept: list[Reason] = []
    for reason in reasons:
        if reason.code in policy.ignore:
            continue
        key = f"{reason.code}:{reason.message}"
        if key in seen:
            continue
        seen.add(key)
        kept.append(reason)
    return kept
