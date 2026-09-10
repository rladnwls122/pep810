"""Import-time side-effect detection, including the transitive walk."""

import pytest

from lazyimp.effects import EffectAnalyzer, scan_source
from lazyimp.knowledge import Confidence, EffectKind
from lazyimp.resolver import ModuleResolver


def kinds(source):
    return {effect.kind for effect in scan_source(source, "m").effects}


@pytest.mark.parametrize(
    "source, kind",
    [
        ("import atexit\natexit.register(f)\n", EffectKind.REGISTRATION),
        ("import os\nos.environ['X'] = '1'\n", EffectKind.GLOBAL_CONFIG),
        ("import sys\nsys.path.insert(0, '.')\n", EffectKind.GLOBAL_CONFIG),
        ("import warnings\nwarnings.warn('x')\n", EffectKind.OUTPUT),
        ("print('hello')\n", EffectKind.OUTPUT),
        ("import subprocess\nsubprocess.run(['x'])\n", EffectKind.IO),
        ("open('f').read()\n", EffectKind.IO),
        ("import threading\nthreading.Thread(target=f).start()\n", EffectKind.CONCURRENCY),
        ("import sys\nsys.exit(1)\n", EffectKind.EXIT),
        ("import importlib\nimportlib.import_module('x')\n", EffectKind.DYNAMIC_IMPORT),
        ("import other\nother.attr = 1\n", EffectKind.MONKEYPATCH),
        ("raise RuntimeError('no')\n", EffectKind.EXIT),
        ("@app.route('/')\ndef view(): pass\n", EffectKind.DECORATOR),
    ],
)
def test_detects_effects(source, kind):
    assert kind in kinds(source)


@pytest.mark.parametrize(
    "source",
    [
        '"""Docstring."""\n',
        "CONST = 42\nTABLE = {'a': 1}\n",
        "import re\nPATTERN = re.compile('x')\n",
        "import logging\nLOG = logging.getLogger(__name__)\n",
        "import os.path\nHERE = os.path.dirname('x')\n",
        "from dataclasses import dataclass\n@dataclass\nclass C:\n    x: int = 0\n",
        "from contextlib import contextmanager\n@contextmanager\ndef f(): yield\n",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import expensive\n",
        "if __name__ == '__main__':\n    print('never on import')\n",
        "def f():\n    print('not at import time')\n",
        "class C:\n    def method(self):\n        open('f')\n",
    ],
)
def test_pure_code_produces_no_findings(source):
    findings = [
        effect
        for effect in scan_source(source, "m").effects
        if effect.confidence >= Confidence.MEDIUM
    ]
    assert findings == []


def test_platform_guarded_raise_is_low_confidence():
    # Nearly every stdlib module has one of these; treating it as a hard signal
    # would make the whole standard library look dangerous.
    source = "import sys\nif sys.platform == 'win32':\n    raise ImportError('nope')\n"
    effects = scan_source(source, "m").effects
    assert all(effect.confidence is Confidence.LOW for effect in effects)


def test_alias_resolution_reaches_the_call_table():
    source = "import atexit as cleanup\ncleanup.register(f)\n"
    assert EffectKind.REGISTRATION in kinds(source)


def test_lazy_imports_do_not_propagate(tmp_path):
    (tmp_path / "root.py").write_text("lazy import child\n")
    (tmp_path / "child.py").write_text("import atexit\natexit.register(f)\n")
    scan = EffectAnalyzer(resolver=ModuleResolver(project_roots=[tmp_path])).scan_module("root")
    assert scan.lazy_imports == ["child"] and scan.eager_imports == []


def test_effects_are_transitive(tmp_path):
    (tmp_path / "root.py").write_text("import middle\n")
    (tmp_path / "middle.py").write_text("import leaf\n")
    (tmp_path / "leaf.py").write_text("import atexit\natexit.register(f)\n")
    analyzer = EffectAnalyzer(resolver=ModuleResolver(project_roots=[tmp_path]))
    effects = analyzer.effects_of("root")
    registration = [e for e in effects if e.kind is EffectKind.REGISTRATION]
    assert registration
    assert registration[0].via == ("root", "middle")
    assert registration[0].module == "leaf"


def test_transitive_walk_terminates_on_cycles(tmp_path):
    (tmp_path / "a.py").write_text("import b\n")
    (tmp_path / "b.py").write_text("import a\n")
    analyzer = EffectAnalyzer(resolver=ModuleResolver(project_roots=[tmp_path]))
    assert analyzer.effects_of("a") == []


def test_depth_limit_is_respected(tmp_path):
    (tmp_path / "a.py").write_text("import b\n")
    (tmp_path / "b.py").write_text("import c\n")
    (tmp_path / "c.py").write_text("print('deep')\n")
    resolver = ModuleResolver(project_roots=[tmp_path])
    assert EffectAnalyzer(resolver=resolver, max_depth=0).effects_of("a") == []
    assert EffectAnalyzer(resolver=resolver, max_depth=2).effects_of("a")


def test_known_side_effect_module_is_flagged():
    analyzer = EffectAnalyzer(resolver=ModuleResolver())
    effects = analyzer.effects_of("gevent.monkey")
    assert any("patches the standard library" in effect.detail for effect in effects)


def test_compiled_extension_is_opaque(tmp_path):
    (tmp_path / "native.so").write_bytes(b"\x7fELF not really")
    analyzer = EffectAnalyzer(resolver=ModuleResolver(project_roots=[tmp_path]))
    effects = analyzer.scan_module("native").effects
    assert effects and effects[0].kind is EffectKind.OPAQUE


def test_unparseable_module_is_reported():
    result = scan_source("def f(:\n", "broken")
    assert not result.analysable and result.effects[0].kind is EffectKind.OPAQUE
