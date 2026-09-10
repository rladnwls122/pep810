"""Render analysis results as text, JSON or Markdown.

The text renderer is the one people read, so it is built around a single claim:
a finding is only useful if you can act on it.  Every line therefore carries the
file and line to open, the decision, and the reason -- never a bare count.

Colour is applied only when the stream is a terminal, so piping to a file or a
CI log produces clean text.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from .api import AnalysisResult
from .bench import Comparison
from .importtime import ImportTimeTree
from .verdict import Decision

__all__ = ["Style", "render_text", "render_json", "render_markdown", "render_benchmark"]

_DECISION_ORDER = [
    Decision.SAFE,
    Decision.RISKY,
    Decision.UNSAFE,
    Decision.LOW_BENEFIT,
    Decision.INELIGIBLE,
    Decision.SKIPPED,
    Decision.ALREADY_LAZY,
]

_COLOURS = {
    Decision.SAFE: "32",  # green
    Decision.RISKY: "33",  # yellow
    Decision.UNSAFE: "31",  # red
    Decision.LOW_BENEFIT: "36",  # cyan
    Decision.INELIGIBLE: "90",  # grey
    Decision.SKIPPED: "90",
    Decision.ALREADY_LAZY: "90",
}


@dataclass
class Style:
    """Terminal styling, disabled unless the stream is a TTY."""

    enabled: bool = True

    @classmethod
    def for_stream(cls, stream: IO[str]) -> "Style":
        if os.environ.get("NO_COLOR"):
            return cls(enabled=False)
        return cls(enabled=bool(getattr(stream, "isatty", lambda: False)()))

    def paint(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, text: str) -> str:
        return self.paint(text, "1")

    def dim(self, text: str) -> str:
        return self.paint(text, "2")

    def decision(self, decision: Decision) -> str:
        return self.paint(decision.value, _COLOURS.get(decision, "0"))


def render_text(
    result: AnalysisResult,
    show: frozenset[Decision] | None = None,
    style: Style | None = None,
    root: Path | None = None,
    verbose: bool = False,
) -> str:
    """Render the per-import findings and a summary."""
    style = style or Style(enabled=False)
    show = show or frozenset({Decision.SAFE, Decision.RISKY, Decision.UNSAFE})
    root = root or result.project.root
    lines: list[str] = []

    for file in result.files:
        shown = [verdict for verdict in file.verdicts if verdict.decision in show]
        if not shown:
            continue
        lines.append(style.bold(_relative(file.path, root)))
        for verdict in shown:
            site = verdict.site
            statement = site.source_line.strip() or f"{site.kind} ..."
            lines.append(
                f"  {site.lineno:>5}  {style.decision(verdict.decision):<22} {statement}"
            )
            reasons = verdict.reasons if verbose else verdict.reasons[:1]
            for reason in reasons:
                lines.append(f"         {style.dim(str(reason))}")
        lines.append("")

    if result.errors:
        lines.append(style.bold("could not analyse"))
        for path, error in result.errors:
            lines.append(f"  {_relative(path, root)}: {error}")
        lines.append("")

    lines.append(_summary(result, style))
    return "\n".join(lines)


def _summary(result: AnalysisResult, style: Style) -> str:
    counts = result.counts
    parts = [
        style.bold(
            f"{len(result.files)} files, {result.total_imports} import statements"
        )
    ]
    for decision in _DECISION_ORDER:
        count = counts.get(decision, 0)
        if count:
            parts.append(f"  {style.decision(decision):<22} {count}")

    if result.importtime is not None and result.importtime:
        saved_ms = result.estimated_saving_us() / 1000
        total_ms = result.importtime.total_us / 1000
        share = (saved_ms / total_ms * 100) if total_ms else 0.0
        parts.append("")
        parts.append(
            style.bold(
                f"measured import cost {total_ms:.1f} ms; "
                f"safe-to-defer imports account for {saved_ms:.1f} ms ({share:.0f}%)"
            )
        )
        parts.append(
            style.dim(
                "  this is an upper bound: a deferred import still runs if the "
                "code path that uses it runs"
            )
        )

    return "\n".join(parts)


def render_hotspots(
    result: AnalysisResult,
    tree: ImportTimeTree,
    limit: int = 15,
    style: Style | None = None,
) -> str:
    """Rank measured import costs and say which the analysis found deferrable.

    This is the table that decides what to do first: an import that costs 40 ms
    and is safe to defer is worth more than forty that cost 0.1 ms each.
    """
    style = style or Style(enabled=False)
    safe: dict[str, int] = {}
    other: dict[str, Decision] = {}
    for _, verdict in result.verdicts():
        for module in verdict.site.modules:
            if module.startswith("."):
                continue
            if verdict.decision is Decision.SAFE:
                safe[module] = safe.get(module, 0) + 1
            else:
                other.setdefault(module, verdict.decision)

    lines = [style.bold(f"{'cost':>10}  {'verdict':<14} module"), ""]
    for record in tree.top_level(limit):
        if record.module in safe:
            verdict_text = style.decision(Decision.SAFE)
        elif record.module in other:
            verdict_text = style.decision(other[record.module])
        else:
            verdict_text = style.dim("not imported directly")
        lines.append(
            f"{record.cumulative_ms:>7.1f} ms  {verdict_text:<14} {record.module}"
        )
    return "\n".join(lines)


def render_benchmark(comparison: Comparison, style: Style | None = None) -> str:
    """Render a before/after startup comparison."""
    style = style or Style(enabled=False)
    lines = []

    for measurement in (comparison.baseline, comparison.lazy):
        if measurement.error:
            lines.append(f"{measurement.label}: {style.paint(measurement.error, '31')}")
            continue
        low, high = measurement.spread_ms
        lines.append(
            f"{measurement.label:<30} "
            f"{measurement.wall_ms:7.1f} ms process  "
            f"{measurement.inner_ms:7.1f} ms import  "
            f"(range {low:.1f}-{high:.1f})  "
            f"{measurement.modules:.0f} modules  "
            f"{measurement.rss_kb / 1024:.1f} MiB"
        )

    if comparison.baseline.error or comparison.lazy.error:
        return "\n".join(lines)

    lines.append("")
    lines.append(
        style.bold(
            f"process {comparison.wall_delta_pct:+.1f}%   "
            f"import {comparison.inner_delta_pct:+.1f}%   "
            f"memory {comparison.rss_delta_pct:+.1f}%   "
            f"modules {comparison.modules_delta:+.0f}"
        )
    )
    if comparison.lazy.lazy_modules:
        lines.append(
            style.dim(f"  {comparison.lazy.lazy_modules:.0f} imports still unreified at exit")
        )
    if not comparison.is_significant():
        lines.append(
            style.dim(
                "  the two sample ranges overlap, so this difference is not "
                "distinguishable from noise -- raise --runs"
            )
        )
    return "\n".join(lines)


def render_json(result: AnalysisResult, root: Path | None = None) -> str:
    """Render the full result as JSON, for CI and editor integrations."""
    root = root or result.project.root
    payload = {
        "summary": {
            "files": len(result.files),
            "imports": result.total_imports,
            "decisions": {
                decision.value: result.counts.get(decision, 0)
                for decision in _DECISION_ORDER
            },
        },
        "files": [
            {
                "path": _relative(file.path, root),
                "module": file.analysis.module_name,
                "imports": [
                    {
                        "line": verdict.site.lineno,
                        "column": verdict.site.col,
                        "statement": verdict.site.source_line.strip(),
                        "modules": verdict.site.modules,
                        "names": verdict.site.names,
                        "context": verdict.site.context.value,
                        "decision": verdict.decision.value,
                        "reasons": [
                            {
                                "code": reason.code,
                                "message": reason.message,
                                "confidence": reason.confidence.name.lower(),
                            }
                            for reason in verdict.reasons
                        ],
                    }
                    for verdict in file.verdicts
                ],
            }
            for file in result.files
        ],
        "errors": [
            {"path": _relative(path, root), "error": error}
            for path, error in result.errors
        ],
    }
    if result.importtime is not None and result.importtime:
        payload["importtime"] = {
            "total_us": result.importtime.total_us,
            "estimated_deferrable_us": result.estimated_saving_us(),
            "top": [
                {"module": record.module, "cumulative_us": record.cumulative_us}
                for record in result.importtime.top_level(25)
            ],
        }
    return json.dumps(payload, indent=2)


def render_markdown(result: AnalysisResult, root: Path | None = None) -> str:
    """Render a summary suitable for a pull-request comment."""
    root = root or result.project.root
    counts = result.counts
    lines = [
        "# pep810 report",
        "",
        f"Analysed **{len(result.files)}** files and "
        f"**{result.total_imports}** import statements.",
        "",
        "| decision | count |",
        "| --- | ---: |",
    ]
    for decision in _DECISION_ORDER:
        count = counts.get(decision, 0)
        if count:
            lines.append(f"| {decision.value} | {count} |")

    safe = result.verdicts(Decision.SAFE)
    if safe:
        lines += ["", "## Safe to defer", "", "| file | line | import |", "| --- | ---: | --- |"]
        for file, verdict in safe[:50]:
            statement = verdict.site.source_line.strip().replace("|", "\\|")
            lines.append(
                f"| `{_relative(file.path, root)}` | {verdict.site.lineno} | `{statement}` |"
            )
        if len(safe) > 50:
            lines.append(f"| ... | | {len(safe) - 50} more |")

    risky = result.verdicts(Decision.RISKY, Decision.UNSAFE)
    if risky:
        lines += ["", "## Needs review", "", "| file | line | decision | why |", "| --- | ---: | --- | --- |"]
        for file, verdict in risky[:50]:
            why = verdict.headline.replace("|", "\\|")
            lines.append(
                f"| `{_relative(file.path, root)}` | {verdict.site.lineno} "
                f"| {verdict.decision.value} | {why} |"
            )
        if len(risky) > 50:
            lines.append(f"| ... | | | {len(risky) - 50} more |")

    return "\n".join(lines)


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
