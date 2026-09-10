"""Find, apply and measure PEP 810 lazy imports.

PEP 810 adds a ``lazy`` soft keyword to Python 3.15 that defers a module's
loading until its name is first used.  The savings are real -- the PEP cites
50-70% off startup and 30-40% off memory on real workloads -- but the keyword
alone does not tell you which of your imports are safe to defer, and getting it
wrong is silent: an import kept only for its side effect simply stops happening.

This package answers that question statically and then acts on the answer.

    >>> from pathlib import Path
    >>> from lazyimp import analyze_paths
    >>> result = analyze_paths([Path("src")])          # doctest: +SKIP
    >>> result.counts[Decision.SAFE]                   # doctest: +SKIP
    42

The pipeline is four steps, each usable on its own:

:mod:`lazyimp.analyzer`
    Where each import sits and how its names are used in that file.
:mod:`lazyimp.effects`
    What importing the target module actually does, transitively.
:mod:`lazyimp.verdict`
    The decision, with reason codes you can suppress like lint rules.
:mod:`lazyimp.codemod` / :mod:`lazyimp.filters`
    Rewrite the source, or generate the runtime filter PEP 810 invites instead.

:mod:`lazyimp.bench` closes the loop by measuring startup with lazy imports
forced off and then on, so the claim can be checked rather than assumed.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .analyzer import FileAnalysis, ImportContext, ImportSite, analyze_file, analyze_source
from .api import AnalysisResult, FileResult, analyze_paths, build_filter_plan
from .bench import Comparison, Measurement, run_benchmark, supports_pep810
from .codemod import FileEdit, render_lazy_modules, rewrite
from .effects import Effect, EffectAnalyzer, ModuleEffects, scan_source
from .filters import FilterPlan, render_filter_module
from .importtime import ImportTimeTree, measure_importtime, parse_importtime
from .knowledge import Confidence, EffectKind
from .project import Project, discover
from .resolver import ModuleKind, ModuleResolver
from .verdict import Decision, Policy, Reason, Verdict, judge

__all__ = [
    "__version__",
    "AnalysisResult",
    "Comparison",
    "Confidence",
    "Decision",
    "Effect",
    "EffectAnalyzer",
    "EffectKind",
    "FileAnalysis",
    "FileEdit",
    "FileResult",
    "FilterPlan",
    "ImportContext",
    "ImportSite",
    "ImportTimeTree",
    "Measurement",
    "ModuleEffects",
    "ModuleKind",
    "ModuleResolver",
    "Policy",
    "Project",
    "Reason",
    "Verdict",
    "analyze_file",
    "analyze_paths",
    "analyze_source",
    "build_filter_plan",
    "discover",
    "judge",
    "measure_importtime",
    "parse_importtime",
    "render_filter_module",
    "render_lazy_modules",
    "rewrite",
    "run_benchmark",
    "scan_source",
    "supports_pep810",
]
