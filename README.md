# lazyimp

Find, apply and measure [PEP 810](https://peps.python.org/pep-0810/) lazy imports.

Python 3.15 adds a `lazy` soft keyword that defers a module's loading until its
name is first used. The PEP cites 50–70% off startup and 30–40% off memory on
real workloads. What it does not give you is a way to tell *which* of your
imports are safe to defer — and getting that wrong is silent:

```python
import readline   # nothing in this file ever mentions `readline`
```

Make that lazy and the import never happens at all, because nothing ever touches
the name. Interactive line editing quietly stops working. No error, no warning.

PEP 810 anticipates the gap in its own text:

> Static analysis tools could detect modules with side effects and automatically
> configure filters.

`lazyimp` is that tool, plus the two things you need on either side of it: a
codemod that applies the answer, and a benchmark that checks the answer was
worth applying.

```console
$ lazyimp analyze src/
src/myapp/core.py
      4  safe                   import subprocess
         S500: no import-time side effects found; safe to defer
      8  risky                  from . import plugins
         W301: importing myapp.plugins calls atexit.register()

src/myapp/__init__.py
      3  unsafe                 import readline
         E201: `readline` is never used in this module, so the import exists for its
               side effect -- deferring it would cancel the effect, not delay it

3 files, 12 import statements
  safe                   6
  risky                  1
  unsafe                 2
  low-benefit            3
```

## Install

```console
$ pip install lazyimp
```

No dependencies. Runs on Python 3.9+ — you do not need 3.15 to analyse or rewrite
code for 3.15, only to benchmark it.

## The five commands

```console
$ lazyimp analyze src/                     # what could be lazy, and what must not be
$ lazyimp hotspots src/ -e "import myapp"  # what is actually expensive
$ lazyimp apply src/ --write               # rewrite the safe ones
$ lazyimp bench -e "import myapp"          # prove it worked
$ lazyimp filter src/ -o sitecustomize.py  # or skip the codemod entirely
```

Plus `lazyimp check` for CI, which fails the build when an import that could be
lazy still is not.

### analyze

Every import statement gets one of seven decisions:

| decision | meaning |
| --- | --- |
| `safe` | Deferring changes nothing observable. Convert it. |
| `risky` | Legal, but importing the target has side effects. Read the reason. |
| `unsafe` | Deferring would change behaviour. Do not convert. |
| `low-benefit` | Legal and safe, but the name is used at module level anyway. |
| `ineligible` | PEP 810 does not allow `lazy` here. |
| `skipped` | Inside `if TYPE_CHECKING:` or a `__main__` guard; never runs. |
| `already-lazy` | Nothing to do. |

Reasons carry stable codes you can silence like lint rules — `E` blocks the
rewrite, `W` marks it risky, `I` is informational:

```console
$ lazyimp analyze src/ --ignore W311 --ignore I403
```

or in `pyproject.toml`:

```toml
[tool.lazyimp]
ignore = ["W311"]
exclude = ["vendor"]
```

`--format json` and `--format markdown` are there for editors and PR comments.

### apply

The rewrite inserts five characters — `lazy ` — before the `import` or `from`
keyword, and touches nothing else:

```diff
-import json
-import subprocess  # for the shell-out
-from xml.etree import (
+lazy import json
+lazy import subprocess  # for the shell-out
+lazy from xml.etree import (
     ElementTree,
 )
```

Comments, quote styles and parenthesised import lists survive, because the
codemod edits bytes rather than round-tripping through an AST. Running it twice
is a no-op. Every rewritten file is compiled before it is written; a file that
would not compile is left alone and reported.

Without `--write` you get the diff. With `--style lazy-modules` you get PEP 810's
`__lazy_modules__` declaration instead, which is inert before 3.15 — one release
that is fast on new Pythons and unchanged on old ones.

### filter

The other half of PEP 810. Instead of editing sources, run the whole application
with `-X lazy_imports=all` and let a generated deny-list force back to eager
exactly the modules the analysis found unsafe:

```console
$ lazyimp filter src/ -o sitecustomize.py --lazy-only myapp
$ python -X lazy_imports=all -m myapp
```

The generated module installs `sys.set_lazy_imports_filter` and documents why
each module is on the list. Note that PEP 810 consults the filter when an import
statement *runs*, so it has to be installed before your entry point — from
`sitecustomize` or a wrapper, not from the top of `main.py`.

### hotspots and bench

`hotspots` ranks measured import cost against the analysis, so you know what to
do first:

```console
$ lazyimp hotspots src/ -e "import myapp"
      cost  verdict        module
   41.2 ms  safe           pandas
   18.7 ms  risky          myapp.plugins
    5.4 ms  low-benefit    myapp.config

312.8 ms of import cost; 214.1 ms sits behind imports the analysis found safe to defer
```

`bench` measures the real thing. It uses the interpreter's own switch rather than
two copies of your tree — `-X lazy_imports=none` forces every `lazy import` back
to eager — so both sides run the same files on the same interpreter and differ in
exactly one variable:

```console
$ lazyimp bench -e "import myapp" --runs 15
eager (-X lazy_imports=none)     412.6 ms process    331.0 ms import  (range 401.2-433.8)  1284 modules  84.2 MiB
lazy (as written)                168.3 ms process     92.4 ms import  (range 161.9-179.4)   412 modules  51.7 MiB

process -59.2%   import -72.1%   memory -38.6%   modules -872
  61 imports still unreified at exit
```

Startup benchmarks are noisy, so the report carries the spread and says so
outright when the two sample ranges overlap. Benchmarking needs a Python 3.15+
interpreter; point `--python` at one if it is not the interpreter running the
tool. `lazyimp` refuses to report a comparison it cannot actually make.

## How the analysis decides

Four questions, in this order.

**1. Is `lazy` legal here?** PEP 810 restricts it to module scope. Inside a
function, a class body, or a `try`/`except`/`finally` block it is a
`SyntaxError`, as are `lazy from x import *` and `lazy from __future__ import`.

**2. Would deferring change behaviour?** The important case is an import whose
bound name is never used: it exists for its side effect, and deferring it does
not delay the effect, it cancels it. A name that is deleted or rebound at module
level is treated the same way. A package `__init__` with no `__all__` is the
exception — there, an unused name is an implicit re-export.

**3. What does importing the target do?** The target module's top level is
scanned for registration, monkeypatching, I/O, global configuration, threads and
process exit. Because deferring an import defers everything it pulls in, the walk
is transitive, and each finding carries the chain that produced it:

```
W301: importing myapp.api -> myapp.db reaches myapp.registry, which calls
      atexit.register() (myapp/registry.py:41)
```

**4. Is it worth it?** A lazy import reifies the moment its name is touched, so
an import used at module level saves nothing. The win is names used only in
function bodies — and, since [PEP 649](https://peps.python.org/pep-0649/),
annotations.

Everything is a heuristic, and the tool says which ones it is confident about.
`--confidence low` widens what counts as risky; `--confidence high` narrows it.

## Working on 3.15 source from an older interpreter

`lazy import json` is a `SyntaxError` on every Python before 3.15, which would
otherwise stop the tool from running on the interpreter most projects are
migrating *from*. `lazyimp` reads and writes PEP 810 source on 3.9+ by finding
the soft keywords with `tokenize` — which knows about strings and comments, so
`lazy = 1` and `"lazy import x"` in a docstring are not mistaken for keywords —
stripping them for parsing, and mapping the AST's offsets back onto the original
bytes.

## Library use

```python
from pathlib import Path
from lazyimp import analyze_paths, Decision, Policy

result = analyze_paths([Path("src")], Policy(include_low_benefit=True))
for file, verdict in result.verdicts(Decision.SAFE):
    print(f"{file.path}:{verdict.site.lineno} {verdict.site.source_line.strip()}")
```

The layers underneath are independently useful: `lazyimp.analyzer` for import
sites and usage, `lazyimp.effects` for import-time side effects,
`lazyimp.importtime` for parsing `-X importtime`, `lazyimp.codemod` for the
minimal-diff rewrite.

## Limitations

- Import-time side effects are undecidable in general. The knowledge base covers
  the calls that show up at the top level of real code; anything unrecognised is
  reported at low confidence rather than guessed at.
- Compiled extension modules cannot be analysed. They are reported as opaque.
- Non-path importers — zipimport, frozen modules, packages that extend
  `__path__` at runtime — resolve as unknown rather than as safe.
- Star-imported names are invisible to the usage analysis, so a module using
  `from x import *` may show false `unsafe` verdicts. Those imports are
  ineligible anyway.
- `--include-risky` exists, but the name is the recommendation.

## Licence

MIT.
