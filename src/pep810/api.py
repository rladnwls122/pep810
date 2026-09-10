"""The orchestration layer: run the whole analysis over a set of paths.

Everything below this module answers one narrow question about one file or one
module.  This is where those answers are combined into a result a report, a
codemod or a CI gate can consume, and where the expensive pieces are shared:
one resolver and one effect analyzer serve every file, so a dependency scanned
for the first file is not rescanned for the two hundredth.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .analyzer import FileAnalysis, analyze_file
from .effects import DEFAULT_MAX_DEPTH, EffectAnalyzer
from .filters import FilterPlan
from .importtime import ImportTimeTree
from .knowledge import EffectKind
from .project import DEFAULT_EXCLUDES, Project, discover
from .resolver import ModuleResolver
from .verdict import Decision, Policy, Verdict, judge

__all__ = ["FileResult", "AnalysisResult", "analyze_paths", "build_filter_plan"]


@dataclass
class FileResult:
    """One file's analysis and the verdict for each of its imports."""

    analysis: FileAnalysis
    verdicts: list[Verdict] = field(default_factory=list)

    @property
    def path(self) -> Path:
        return self.analysis.path

    def by_decision(self, decision: Decision) -> list[Verdict]:
        return [verdict for verdict in self.verdicts if verdict.decision is decision]

    @property
    def has_findings(self) -> bool:
        return any(
            verdict.decision in (Decision.SAFE, Decision.RISKY, Decision.UNSAFE)
            for verdict in self.verdicts
        )


@dataclass
class AnalysisResult:
    """The result of analysing a whole project."""

    project: Project
    files: list[FileResult] = field(default_factory=list)
    policy: Policy = field(default_factory=Policy)
    importtime: ImportTimeTree | None = None
    errors: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def counts(self) -> Counter:
        counter: Counter = Counter()
        for file in self.files:
            for verdict in file.verdicts:
                counter[verdict.decision] += 1
        return counter

    @property
    def total_imports(self) -> int:
        return sum(len(file.verdicts) for file in self.files)

    def verdicts(self, *decisions: Decision) -> list[tuple[FileResult, Verdict]]:
        """Every verdict matching ``decisions``, paired with its file."""
        wanted = set(decisions)
        return [
            (file, verdict)
            for file in self.files
            for verdict in file.verdicts
            if not wanted or verdict.decision in wanted
        ]

    def estimated_saving_us(self) -> float:
        """Import-time microseconds attributable to the safe-to-defer imports.

        Only meaningful when :attr:`importtime` was measured.  Each module is
        counted once even if several files import it: the second import of a
        module is a dict lookup, so deferring it twice saves the cost once.
        """
        if self.importtime is None:
            return 0.0
        counted: set[str] = set()
        total = 0.0
        for _, verdict in self.verdicts(Decision.SAFE):
            for module in verdict.site.modules:
                if module in counted or module.startswith("."):
                    continue
                counted.add(module)
                total += self.importtime.cost_of(module)
        return total


def analyze_paths(
    paths: list[Path],
    policy: Policy | None = None,
    excludes: tuple[str, ...] = DEFAULT_EXCLUDES,
    max_depth: int = DEFAULT_MAX_DEPTH,
    skip_stdlib_effects: bool = True,
    extra_search_paths: list[Path] | None = None,
) -> AnalysisResult:
    """Analyse every Python file under ``paths``."""
    policy = policy or Policy()
    project = discover(paths, excludes=excludes)

    resolver = ModuleResolver(
        project_roots=list(project.source_roots),
        search_paths=list(extra_search_paths) if extra_search_paths else [],
    )
    effect_analyzer = EffectAnalyzer(
        resolver=resolver,
        max_depth=max_depth,
        skip_stdlib=skip_stdlib_effects,
    )

    result = AnalysisResult(project=project, policy=policy)
    for path in project.files:
        analysis = analyze_file(path, module_name=project.module_name(path))
        if analysis.syntax_error is not None:
            result.errors.append((path, analysis.syntax_error))
            continue
        verdicts = [
            judge(site, analysis, effect_analyzer, policy) for site in analysis.sites
        ]
        result.files.append(FileResult(analysis=analysis, verdicts=verdicts))

    return result


def build_filter_plan(
    result: AnalysisResult,
    lazy_only_prefixes: list[str] | None = None,
    mode: str = "all",
) -> FilterPlan:
    """Derive a :class:`~pep810.filters.FilterPlan` from an analysis.

    Every module that made an import risky or unsafe goes on the deny list, so
    running under ``-X lazy_imports=all`` reproduces the behaviour the codemod
    would have produced -- without editing a line of source.
    """
    plan = FilterPlan(
        lazy_only_prefixes=list(lazy_only_prefixes or []),
        mode=mode,
    )

    for _, verdict in result.verdicts(Decision.RISKY):
        for effect in verdict.effects:
            if effect.kind is EffectKind.UNKNOWN_CALL:
                continue
            if effect.confidence < result.policy.block_at:
                continue
            # Deny the module the effect is *in*, and the module that was
            # imported to reach it: either one being lazy defers the effect.
            plan.deny(effect.module, effect.detail)
            if effect.via:
                plan.deny(effect.via[0], f"reaches {effect.module}, which {effect.detail}")

    for _, verdict in result.verdicts(Decision.UNSAFE):
        for module in verdict.site.modules:
            if module and not module.startswith("."):
                plan.deny(module, verdict.headline)

    return plan
