"""Measure what the migration actually bought.

PEP 810 quotes savings of 50-70% on startup and 30-40% on memory for real
workloads.  Those are someone else's workloads.  This module measures yours.

The comparison uses the interpreter's own switch rather than two copies of the
tree: ``-X lazy_imports=none`` forces every ``lazy import`` back to eager, so the
same code, the same files and the same interpreter produce both sides of the
measurement.  Nothing is stashed, checked out, or copied, and the two runs differ
in exactly one variable.

Each side is run in a fresh subprocess several times and compared by median.
Startup benchmarks are noisy -- a cold page cache or a scheduler hiccup shows up
as a 30% outlier -- so the report also carries the spread, and
:meth:`Comparison.is_significant` refuses to call a difference real when the two
sets of samples overlap.
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "Sample",
    "Measurement",
    "Comparison",
    "LazyImportsUnsupported",
    "supports_pep810",
    "measure",
    "run_benchmark",
]


class LazyImportsUnsupported(RuntimeError):
    """Raised when the interpreter under test does not implement PEP 810."""

#: Runs inside the child, prints one JSON line, and is deliberately tiny so that
#: what it measures is the target's import cost and not its own.
_HARNESS = r"""
import json, sys, time
_t0 = time.perf_counter()
{body}
_elapsed = time.perf_counter() - _t0
_rss = 0
try:
    import resource
    _rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # ru_maxrss is kilobytes on Linux and bytes on macOS.
    if sys.platform == "darwin":
        _rss //= 1024
except Exception:
    pass
_lazy = 0
try:
    _lazy = len(sys.lazy_modules)
except Exception:
    pass
print(json.dumps({{
    "elapsed_s": _elapsed,
    "modules": len(sys.modules),
    "rss_kb": _rss,
    "lazy_modules": _lazy,
}}), file=sys.stderr)
"""


@dataclass
class Sample:
    """One subprocess run."""

    wall_s: float  #: Measured by the parent: process spawn included.
    inner_s: float  #: Measured by the child: the import itself.
    modules: int  #: ``len(sys.modules)`` once the target is imported.
    rss_kb: int  #: Peak resident set of the child.
    lazy_modules: int  #: ``len(sys.lazy_modules)``; 0 before PEP 810.


@dataclass
class Measurement:
    """Repeated samples of one configuration."""

    label: str
    samples: list[Sample] = field(default_factory=list)
    error: str | None = None

    def _median(self, attribute: str) -> float:
        values = [getattr(sample, attribute) for sample in self.samples]
        return statistics.median(values) if values else 0.0

    @property
    def wall_ms(self) -> float:
        return self._median("wall_s") * 1000

    @property
    def inner_ms(self) -> float:
        return self._median("inner_s") * 1000

    @property
    def rss_kb(self) -> float:
        return self._median("rss_kb")

    @property
    def modules(self) -> float:
        return self._median("modules")

    @property
    def lazy_modules(self) -> float:
        return self._median("lazy_modules")

    @property
    def spread_ms(self) -> tuple[float, float]:
        """Fastest and slowest wall-clock sample, in milliseconds."""
        if not self.samples:
            return (0.0, 0.0)
        values = sorted(sample.wall_s * 1000 for sample in self.samples)
        return (values[0], values[-1])


@dataclass
class Comparison:
    """Baseline against lazy, with the deltas a report wants."""

    baseline: Measurement
    lazy: Measurement

    @staticmethod
    def _delta(before: float, after: float) -> float:
        """Percentage change; negative means the lazy run was smaller."""
        if before <= 0:
            return 0.0
        return (after - before) / before * 100.0

    @property
    def wall_delta_pct(self) -> float:
        return self._delta(self.baseline.wall_ms, self.lazy.wall_ms)

    @property
    def inner_delta_pct(self) -> float:
        return self._delta(self.baseline.inner_ms, self.lazy.inner_ms)

    @property
    def rss_delta_pct(self) -> float:
        return self._delta(self.baseline.rss_kb, self.lazy.rss_kb)

    @property
    def modules_delta(self) -> float:
        return self.lazy.modules - self.baseline.modules

    def is_significant(self, attribute: str = "wall_s") -> bool:
        """Whether the two sample sets are actually distinguishable.

        A startup benchmark that ran three times and saw a 4% difference has
        measured its own noise.  Requiring the ranges to be disjoint is a blunt
        test, but it is the honest one for samples this few.
        """
        left = sorted(getattr(sample, attribute) for sample in self.baseline.samples)
        right = sorted(getattr(sample, attribute) for sample in self.lazy.samples)
        if len(left) < 3 or len(right) < 3:
            return False
        return left[-1] < right[0] or right[-1] < left[0]


def supports_pep810(python: str | Path = sys.executable) -> bool:
    """Whether ``python`` actually implements PEP 810.

    Passing ``-X lazy_imports=none`` proves nothing: CPython accepts any
    ``-X key=value`` it does not recognise and files it under
    ``sys._xoptions``.  An interpreter without PEP 810 would therefore run the
    "baseline" and the "lazy" side identically and the benchmark would report
    pure noise as a result.  So ask for the API instead.
    """
    probe = "import sys; print(int(hasattr(sys, 'set_lazy_imports')))"
    try:
        completed = subprocess.run(
            [str(python), "-c", probe],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and completed.stdout.strip() == "1"


def _run_once(
    python: str | Path,
    body: str,
    extra_args: list[str],
    cwd: Path | None,
    env: dict[str, str] | None,
    timeout: float,
) -> Sample | str:
    code = _HARNESS.format(body=body)
    command = [str(python), *extra_args, "-c", code]
    start = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            cwd=str(cwd) if cwd else None,
            env=env,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"could not run {python}: {exc}"
    wall = time.perf_counter() - start

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        return detail[-1] if detail else f"exited with status {completed.returncode}"

    payload = _last_json_line(completed.stderr)
    if payload is None:
        return "benchmark harness produced no result"
    return Sample(
        wall_s=wall,
        inner_s=float(payload.get("elapsed_s", 0.0)),
        modules=int(payload.get("modules", 0)),
        rss_kb=int(payload.get("rss_kb", 0)),
        lazy_modules=int(payload.get("lazy_modules", 0)),
    )


def _last_json_line(text: str) -> dict | None:
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def measure(
    label: str,
    body: str,
    python: str | Path = sys.executable,
    extra_args: list[str] | None = None,
    runs: int = 7,
    warmup: int = 1,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 300.0,
) -> Measurement:
    """Run one configuration ``runs`` times after ``warmup`` discarded runs."""
    extra_args = extra_args or []
    result = Measurement(label=label)

    for _ in range(max(0, warmup)):
        outcome = _run_once(python, body, extra_args, cwd, env, timeout)
        if isinstance(outcome, str):
            result.error = outcome
            return result

    for _ in range(max(1, runs)):
        outcome = _run_once(python, body, extra_args, cwd, env, timeout)
        if isinstance(outcome, str):
            result.error = outcome
            return result
        result.samples.append(outcome)

    return result


def run_benchmark(
    body: str,
    python: str | Path = sys.executable,
    runs: int = 7,
    warmup: int = 1,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> Comparison:
    """Measure ``body`` with lazy imports disabled, then enabled.

    ``body`` is the statement whose startup cost matters, usually
    ``import myapp`` or ``import myapp.cli``.

    Raises :exc:`LazyImportsUnsupported` when ``python`` predates PEP 810: both
    sides would run the same code and the difference reported would be noise.
    """
    if not supports_pep810(python):
        raise LazyImportsUnsupported(
            f"{python} does not implement PEP 810, so eager and lazy runs would be "
            f"identical; point --python at a Python 3.15+ interpreter"
        )
    baseline = measure(
        "eager (-X lazy_imports=none)",
        body,
        python=python,
        extra_args=["-X", "lazy_imports=none"],
        runs=runs,
        warmup=warmup,
        cwd=cwd,
        env=env,
    )
    lazy = measure(
        "lazy (as written)",
        body,
        python=python,
        extra_args=[],
        runs=runs,
        warmup=warmup,
        cwd=cwd,
        env=env,
    )
    return Comparison(baseline=baseline, lazy=lazy)
