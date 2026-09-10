"""Parse ``python -X importtime`` output into a tree of import costs.

The analyser can say an import is *safe* to defer; only measurement can say it
is *worth* deferring.  ``-X importtime`` is the measurement, and this module
turns its output into something a report can rank.

The format is one line per module, emitted as the import *finishes*, so children
appear before their parents and indentation encodes depth::

    import time:      1234 |       1234 |     encodings.aliases
    import time:       567 |       1801 |   encodings
    import time:        89 |       1890 | codecs

``self`` is the module's own cost and ``cumulative`` includes everything it
imported.  For "what would deferring this import save?" the cumulative figure of
the top-level module is the number that matters, so that is what
:meth:`ImportTimeTree.top_level` reports.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["ImportRecord", "ImportTimeTree", "parse_importtime", "measure_importtime"]

_LINE = re.compile(r"^import time:\s*([\d.]+)\s*\|\s*([\d.]+)\s*\|(\s*)(\S.*)$")


@dataclass
class ImportRecord:
    """One module's entry in the import-time log."""

    module: str
    self_us: float
    cumulative_us: float
    depth: int

    @property
    def self_ms(self) -> float:
        return self.self_us / 1000.0

    @property
    def cumulative_ms(self) -> float:
        return self.cumulative_us / 1000.0


@dataclass
class ImportTimeTree:
    """All records from one ``-X importtime`` run."""

    records: list[ImportRecord] = field(default_factory=list)
    stderr: str = ""

    def __bool__(self) -> bool:
        return bool(self.records)

    @property
    def total_us(self) -> float:
        """Total import cost: the cumulative time of every root-level import."""
        return sum(record.cumulative_us for record in self.records if record.depth == 0)

    def by_module(self) -> dict[str, ImportRecord]:
        """Deepest record per module name, keeping the largest cumulative cost."""
        best: dict[str, ImportRecord] = {}
        for record in self.records:
            existing = best.get(record.module)
            if existing is None or record.cumulative_us > existing.cumulative_us:
                best[record.module] = record
        return best

    def top_level(self, limit: int = 20) -> list[ImportRecord]:
        """The most expensive imports, by cumulative cost, most expensive first."""
        ranked = sorted(self.by_module().values(), key=lambda r: -r.cumulative_us)
        return ranked[:limit]

    def cost_of(self, module: str) -> float:
        """Cumulative microseconds attributable to importing ``module``.

        Falls back to the nearest ancestor that was recorded, because
        ``import a.b.c`` shows up as records for ``a``, ``a.b`` and ``a.b.c``
        while a caller may only know it wrote ``import a.b.c``.
        """
        table = self.by_module()
        parts = module.split(".")
        for stop in range(len(parts), 0, -1):
            record = table.get(".".join(parts[:stop]))
            if record is not None:
                return record.cumulative_us
        return 0.0


def parse_importtime(text: str) -> ImportTimeTree:
    """Parse the stderr of a ``-X importtime`` run.

    Depth comes from indentation width.  The set of widths is only known once
    the whole log has been read, so parsing collects raw rows first and ranks
    the widths afterwards rather than guessing that a level is two spaces.

    Lines that are not import-time records -- warnings, program output that
    landed on stderr -- are collected separately rather than discarded, so a run
    that failed for an unrelated reason can still be explained.
    """
    rows: list[tuple[float, float, int, str]] = []
    other: list[str] = []

    for line in text.splitlines():
        match = _LINE.match(line)
        if match is None:
            if line.strip():
                other.append(line)
            continue
        self_us, cumulative_us, indent, module = match.groups()
        rows.append((float(self_us), float(cumulative_us), len(indent), module.strip()))

    widths = sorted({row[2] for row in rows})
    records = [
        ImportRecord(
            module=module,
            self_us=self_us,
            cumulative_us=cumulative_us,
            depth=widths.index(width),
        )
        for self_us, cumulative_us, width, module in rows
    ]
    return ImportTimeTree(records=records, stderr="\n".join(other))


def measure_importtime(
    code: str,
    python: str | Path = sys.executable,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    timeout: float = 120.0,
) -> ImportTimeTree:
    """Run ``code`` under ``-X importtime`` and parse the result."""
    try:
        completed = subprocess.run(
            [str(python), "-X", "importtime", "-c", code],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(cwd) if cwd else None,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return ImportTimeTree(stderr=f"could not run {python}: {exc}")

    tree = parse_importtime(completed.stderr)
    if not tree.records and completed.returncode != 0:
        tree.stderr = (completed.stderr or completed.stdout).strip()
    return tree
