"""Command line interface.

Five verbs, in the order a migration uses them::

    pep810 analyze src/          # what could be lazy, and what must not be
    pep810 hotspots -e "import myapp"   # what is actually expensive
    pep810 apply src/ --write    # rewrite the safe ones
    pep810 bench -e "import myapp"      # prove it worked
    pep810 filter src/ -o sitecustomize.py   # or skip the codemod entirely

Plus ``pep810 check`` for CI, which reports the same findings and exits
non-zero when something regressed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .api import analyze_paths, build_filter_plan
from .bench import LazyImportsUnsupported, run_benchmark, supports_lazy_imports
from .codemod import apply_edits, render_lazy_modules, rewrite
from .filters import render_filter_module
from .importtime import measure_importtime
from .knowledge import Confidence
from .project import DEFAULT_EXCLUDES, load_config
from .report import (
    Style,
    render_benchmark,
    render_hotspots,
    render_json,
    render_markdown,
    render_text,
)
from .verdict import Decision, Policy

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pep810",
        description="Find, apply and measure PEP 810 lazy imports.",
    )
    parser.add_argument("--version", action="version", version=f"pep810 {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_analysis_options(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("paths", nargs="*", default=["."], type=Path,
                         help="files or directories to analyse (default: .)")
        sub.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                         help="directory name pattern to skip; repeatable")
        sub.add_argument("--ignore", action="append", default=[], metavar="CODE",
                         help="reason code to suppress, e.g. W311; repeatable")
        sub.add_argument("--confidence", choices=["low", "medium", "high"],
                         default="medium",
                         help="minimum confidence for a side effect to block "
                              "an import (default: medium)")
        sub.add_argument("--depth", type=int, default=3, metavar="N",
                         help="how far to follow eager imports when looking for "
                              "side effects (default: 3)")
        sub.add_argument("--include-stdlib-effects", action="store_true",
                         help="also scan standard library modules for side effects")
        sub.add_argument("--no-conditional", action="store_true",
                         help="do not touch imports nested in a module-level if")
        sub.add_argument("--include-low-benefit", action="store_true",
                         help="also report imports whose names are used at module level")

    analyze = subparsers.add_parser("analyze", help="report which imports can be lazy")
    add_analysis_options(analyze)
    analyze.add_argument("--format", choices=["text", "json", "markdown"], default="text")
    analyze.add_argument("--show", action="append", default=[],
                         choices=[decision.value for decision in Decision],
                         help="decisions to list; repeatable (default: safe, risky, unsafe)")
    analyze.add_argument("-v", "--verbose", action="store_true",
                         help="show every reason, not just the first")
    analyze.add_argument("-e", "--entry", metavar="CODE",
                         help="measure import cost of CODE and rank findings by it")

    apply_cmd = subparsers.add_parser("apply", help="rewrite safe imports to lazy imports")
    add_analysis_options(apply_cmd)
    apply_cmd.add_argument("--write", action="store_true",
                           help="edit files in place (default: print the diff)")
    apply_cmd.add_argument("--include-risky", action="store_true",
                           help="also rewrite imports whose targets have side effects")
    apply_cmd.add_argument("--style", choices=["keyword", "lazy-modules"], default="keyword",
                           help="emit `lazy import x` (default) or a __lazy_modules__ list")

    check = subparsers.add_parser("check", help="fail when eligible imports are still eager")
    add_analysis_options(check)
    check.add_argument("--max-eager", type=int, default=0, metavar="N",
                       help="allow up to N safe-but-eager imports (default: 0)")

    filter_cmd = subparsers.add_parser(
        "filter", help="generate a sys.set_lazy_imports_filter module"
    )
    add_analysis_options(filter_cmd)
    filter_cmd.add_argument("-o", "--output", type=Path, metavar="FILE",
                            help="write the module here (default: stdout)")
    filter_cmd.add_argument("--lazy-only", action="append", default=[], metavar="PREFIX",
                            help="only allow modules under PREFIX to be lazy; repeatable")
    filter_cmd.add_argument("--mode", choices=["all", "normal"], default="all",
                            help="lazy-imports mode the generated module installs")

    hotspots = subparsers.add_parser(
        "hotspots", help="rank measured import costs against the analysis"
    )
    add_analysis_options(hotspots)
    hotspots.add_argument("-e", "--entry", required=True, metavar="CODE",
                          help="code to measure, e.g. 'import myapp'")
    hotspots.add_argument("--limit", type=int, default=15)
    hotspots.add_argument("--python", default=sys.executable, metavar="PATH")

    bench = subparsers.add_parser("bench", help="measure startup with and without lazy imports")
    bench.add_argument("-e", "--entry", required=True, metavar="CODE",
                       help="code to measure, e.g. 'import myapp'")
    bench.add_argument("--runs", type=int, default=7)
    bench.add_argument("--warmup", type=int, default=1)
    bench.add_argument("--python", default=sys.executable, metavar="PATH",
                       help="interpreter to benchmark (must implement PEP 810)")
    bench.add_argument("--cwd", type=Path, default=None)

    return parser


def _policy(args: argparse.Namespace, config: dict) -> Policy:
    confidence = {"low": Confidence.LOW, "medium": Confidence.MEDIUM,
                  "high": Confidence.HIGH}[args.confidence]
    ignore = set(args.ignore) | set(config.get("ignore", []))
    return Policy(
        block_at=confidence,
        allow_conditional=not args.no_conditional,
        include_low_benefit=args.include_low_benefit,
        ignore=frozenset(ignore),
    )


def _run_analysis(args: argparse.Namespace):
    paths = [Path(path) for path in (args.paths or ["."])]
    root = paths[0] if paths[0].is_dir() else paths[0].parent
    config = load_config(root)
    excludes = DEFAULT_EXCLUDES + tuple(args.exclude) + tuple(config.get("exclude", []))
    return analyze_paths(
        paths,
        policy=_policy(args, config),
        excludes=excludes,
        max_depth=args.depth,
        skip_stdlib_effects=not args.include_stdlib_effects,
    )


def _cmd_analyze(args: argparse.Namespace) -> int:
    result = _run_analysis(args)

    if args.entry:
        result.importtime = measure_importtime(args.entry)

    if args.format == "json":
        print(render_json(result))
    elif args.format == "markdown":
        print(render_markdown(result))
    else:
        show = (
            frozenset(Decision(value) for value in args.show)
            if args.show
            else frozenset({Decision.SAFE, Decision.RISKY, Decision.UNSAFE})
        )
        print(
            render_text(
                result,
                show=show,
                style=Style.for_stream(sys.stdout),
                verbose=args.verbose,
            )
        )

    # `analyze` is a report, not a gate: finding opportunities is the point, so
    # it exits 0.  `check` is the command that fails a build.
    return EXIT_ERROR if result.errors else EXIT_OK


def _cmd_apply(args: argparse.Namespace) -> int:
    result = _run_analysis(args)
    accept = frozenset(
        {Decision.SAFE, Decision.RISKY} if args.include_risky else {Decision.SAFE}
    )

    if args.style == "lazy-modules":
        for file in result.files:
            declaration = render_lazy_modules(file.analysis, file.verdicts, accept)
            if declaration:
                print(f"# {file.path}")
                print(declaration)
        return EXIT_OK

    edits = [rewrite(file.analysis, file.verdicts, accept) for file in result.files]
    changed, failures = apply_edits(edits, write=args.write)

    if not args.write:
        for edit in edits:
            if edit.is_change:
                sys.stdout.write(edit.diff(root=result.project.root))

    for failure in failures:
        print(f"error: {failure.path}: {failure.error}", file=sys.stderr)

    verb = "rewrote" if args.write else "would rewrite"
    print(f"{verb} {changed} file(s)", file=sys.stderr)
    return EXIT_ERROR if failures else EXIT_OK


def _cmd_check(args: argparse.Namespace) -> int:
    result = _run_analysis(args)
    safe = result.counts.get(Decision.SAFE, 0)
    print(
        render_text(
            result,
            show=frozenset({Decision.SAFE}),
            style=Style.for_stream(sys.stdout),
        )
    )
    if safe > args.max_eager:
        print(
            f"\n{safe} import(s) could be lazy but are still eager "
            f"(allowed: {args.max_eager})",
            file=sys.stderr,
        )
        return EXIT_FINDINGS
    return EXIT_OK


def _cmd_filter(args: argparse.Namespace) -> int:
    result = _run_analysis(args)
    plan = build_filter_plan(result, lazy_only_prefixes=args.lazy_only, mode=args.mode)
    name = args.output.stem if args.output else "lazy_filter"
    module = render_filter_module(plan, module_name=name)

    if args.output:
        args.output.write_text(module, encoding="utf-8")
        print(
            f"wrote {args.output} forcing {len(plan.eager_modules)} module(s) eager",
            file=sys.stderr,
        )
    else:
        print(module)
    return EXIT_OK


def _cmd_hotspots(args: argparse.Namespace) -> int:
    result = _run_analysis(args)
    tree = measure_importtime(args.entry, python=args.python)
    if not tree:
        print(f"error: could not measure import time: {tree.stderr}", file=sys.stderr)
        return EXIT_ERROR
    result.importtime = tree
    style = Style.for_stream(sys.stdout)
    print(render_hotspots(result, tree, limit=args.limit, style=style))
    print()
    saved = result.estimated_saving_us() / 1000
    total = tree.total_us / 1000
    print(
        style.bold(
            f"{total:.1f} ms of import cost; {saved:.1f} ms sits behind "
            f"imports the analysis found safe to defer"
        )
    )
    return EXIT_OK


def _cmd_bench(args: argparse.Namespace) -> int:
    if not supports_lazy_imports(args.python):
        print(
            f"error: {args.python} does not implement PEP 810.\n"
            f"       Use --python to point at a Python 3.15+ build.",
            file=sys.stderr,
        )
        return EXIT_ERROR
    try:
        comparison = run_benchmark(
            args.entry,
            python=args.python,
            runs=args.runs,
            warmup=args.warmup,
            cwd=args.cwd,
        )
    except LazyImportsUnsupported as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(render_benchmark(comparison, style=Style.for_stream(sys.stdout)))
    return EXIT_OK


_COMMANDS = {
    "analyze": _cmd_analyze,
    "apply": _cmd_apply,
    "check": _cmd_check,
    "filter": _cmd_filter,
    "hotspots": _cmd_hotspots,
    "bench": _cmd_bench,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = _COMMANDS[args.command]
    try:
        return handler(args)
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    except BrokenPipeError:  # pragma: no cover - `| head`
        return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
