"""Curated tables of what module-level code does at import time.

The effect scanner in :mod:`pep810.effects` classifies each module-level
statement, and most of that classification comes down to "what does this call
do?".  Answering that in general is undecidable, so this module encodes the
answer for the calls that actually show up at the top level of real code.

Three tables carry the weight:

``PURE_CALLS``
    Calls that are safe to defer: constructors, table builders and the typing
    machinery.  Matching one produces no finding at all.
``EFFECT_CALLS``
    Calls whose whole point is a side effect, mapped to the kind of effect and
    how sure we are.  Matching one is a strong signal.
``SIDE_EFFECT_MODULES``
    Modules that are imported *for* their import-time effect.  Deferring these
    does not delay the effect, it cancels it.

Anything matching none of the tables is reported as an unknown call at low
confidence, which keeps the analysis honest: an unrecognised call is a reason to
look, not a reason to claim danger.
"""

from __future__ import annotations

import enum

__all__ = [
    "EffectKind",
    "Confidence",
    "PURE_CALLS",
    "EFFECT_CALLS",
    "PURE_DECORATORS",
    "SIDE_EFFECT_MODULES",
    "STARTUP_MODULES",
    "PURE_MODULES",
    "lookup_call",
    "side_effect_reason",
]


class EffectKind(enum.Enum):
    """A category of import-time side effect."""

    REGISTRATION = "registration"  #: Adds itself to a registry someone else reads.
    MONKEYPATCH = "monkeypatch"  #: Mutates another module's attributes.
    GLOBAL_CONFIG = "global_config"  #: Reconfigures the interpreter or process.
    IO = "io"  #: Touches the filesystem, network or a subprocess.
    OUTPUT = "output"  #: Writes to stdout/stderr or emits a warning.
    CONCURRENCY = "concurrency"  #: Starts threads, processes or event loops.
    EXIT = "exit"  #: Can terminate the process.
    RANDOMNESS = "randomness"  #: Consumes entropy or reads the clock.
    DECORATOR = "decorator"  #: A module-level decorator that may register.
    DYNAMIC_IMPORT = "dynamic_import"  #: Imports by string; opaque to us.
    CONTROL_FLOW = "control_flow"  #: Loops or context managers at module level.
    MUTATION = "mutation"  #: Rebinds or deletes module-level state.
    UNKNOWN_CALL = "unknown_call"  #: A call we have no opinion about.
    OPAQUE = "opaque"  #: Compiled or unreadable; cannot be analysed at all.

    @property
    def label(self) -> str:
        return self.value.replace("_", " ")


class Confidence(enum.IntEnum):
    """How much weight a finding carries.

    Ordered so that ``max()`` over findings gives the strongest signal, and so
    that a threshold comparison reads naturally.
    """

    LOW = 1  #: A heuristic; often a false positive.
    MEDIUM = 2  #: Probably a real effect.
    HIGH = 3  #: The call exists to do this.


#: Dotted callee names whose evaluation at import time is harmless.  Matching is
#: by suffix on the attribute path, so ``re.compile`` also matches ``_re.compile``
#: only when the alias resolves there -- see :func:`lookup_call`.
PURE_CALLS: frozenset[str] = frozenset(
    {
        # Builtin constructors and table builders.
        "bool", "bytearray", "bytes", "complex", "dict", "float", "frozenset",
        "int", "list", "object", "range", "set", "slice", "str", "tuple",
        "abs", "divmod", "len", "max", "min", "ord", "chr", "round", "sorted",
        "sum", "repr", "hash", "format", "zip", "enumerate", "reversed",
        # Typing and data modelling: these define shapes, they do not act.
        "typing.NamedTuple", "typing.TypedDict", "typing.TypeVar",
        "typing.ParamSpec", "typing.NewType", "typing.TypeVarTuple",
        "typing.Generic", "typing.Protocol", "typing.cast", "typing.overload",
        "typing.get_type_hints", "typing.Annotated", "typing.Literal",
        "typing.Union", "typing.Optional", "typing.Final", "typing.ClassVar",
        "typing_extensions.TypedDict", "typing_extensions.TypeVar",
        "collections.namedtuple", "collections.OrderedDict",
        "collections.defaultdict", "collections.deque", "collections.Counter",
        "collections.ChainMap", "dataclasses.field", "dataclasses.dataclass",
        "enum.auto", "enum.unique",
        # Cheap value construction.
        "re.compile", "re.escape", "struct.Struct", "string.Template",
        "decimal.Decimal", "fractions.Fraction", "uuid.UUID",
        "datetime.date", "datetime.time", "datetime.datetime",
        "datetime.timedelta", "datetime.timezone",
        "functools.partial", "functools.partialmethod", "functools.reduce",
        "operator.itemgetter", "operator.attrgetter", "operator.methodcaller",
        "itertools.chain", "itertools.count", "itertools.cycle",
        # Path arithmetic: string manipulation, no filesystem access.
        "os.path.join", "os.path.dirname", "os.path.basename",
        "os.path.abspath", "os.path.normpath", "os.path.splitext",
        "os.path.split", "os.path.expanduser", "os.path.relpath",
        "pathlib.Path", "pathlib.PurePath", "pathlib.PurePosixPath",
        # Reading configuration without changing it.
        "os.environ.get", "os.getenv", "sys.getdefaultencoding",
        "sysconfig.get_paths", "sysconfig.get_config_var",
        "platform.system", "platform.machine", "platform.python_version",
        # A logger object is inert until something logs to it.
        "logging.getLogger",
        # Version lookups: cheap and read-only.
        "importlib.metadata.version",
    }
)

#: Dotted callee names mapped to the effect they cause and our confidence.
EFFECT_CALLS: dict[str, tuple[EffectKind, Confidence]] = {
    # --- Registration -------------------------------------------------------
    "atexit.register": (EffectKind.REGISTRATION, Confidence.HIGH),
    "signal.signal": (EffectKind.GLOBAL_CONFIG, Confidence.HIGH),
    "codecs.register": (EffectKind.REGISTRATION, Confidence.HIGH),
    "codecs.register_error": (EffectKind.REGISTRATION, Confidence.HIGH),
    "copyreg.pickle": (EffectKind.REGISTRATION, Confidence.HIGH),
    "abc.ABCMeta.register": (EffectKind.REGISTRATION, Confidence.HIGH),
    "faulthandler.enable": (EffectKind.GLOBAL_CONFIG, Confidence.HIGH),
    "multiprocessing.set_start_method": (EffectKind.GLOBAL_CONFIG, Confidence.HIGH),
    "multiprocessing.freeze_support": (EffectKind.GLOBAL_CONFIG, Confidence.HIGH),
    "mimetypes.add_type": (EffectKind.REGISTRATION, Confidence.HIGH),
    "urllib.request.install_opener": (EffectKind.GLOBAL_CONFIG, Confidence.HIGH),
    "socket.setdefaulttimeout": (EffectKind.GLOBAL_CONFIG, Confidence.HIGH),
    # --- Interpreter and process configuration ------------------------------
    "sys.setrecursionlimit": (EffectKind.GLOBAL_CONFIG, Confidence.HIGH),
    "sys.setswitchinterval": (EffectKind.GLOBAL_CONFIG, Confidence.HIGH),
    "sys.set_int_max_str_digits": (EffectKind.GLOBAL_CONFIG, Confidence.HIGH),
    "sys.settrace": (EffectKind.GLOBAL_CONFIG, Confidence.HIGH),
    "sys.setprofile": (EffectKind.GLOBAL_CONFIG, Confidence.HIGH),
    "sys.path.append": (EffectKind.GLOBAL_CONFIG, Confidence.HIGH),
    "sys.path.insert": (EffectKind.GLOBAL_CONFIG, Confidence.HIGH),
    "sys.path.extend": (EffectKind.GLOBAL_CONFIG, Confidence.HIGH),
    "locale.setlocale": (EffectKind.GLOBAL_CONFIG, Confidence.HIGH),
    "warnings.filterwarnings": (EffectKind.GLOBAL_CONFIG, Confidence.HIGH),
    "warnings.simplefilter": (EffectKind.GLOBAL_CONFIG, Confidence.HIGH),
    "warnings.resetwarnings": (EffectKind.GLOBAL_CONFIG, Confidence.HIGH),
    "logging.basicConfig": (EffectKind.GLOBAL_CONFIG, Confidence.HIGH),
    "logging.config.dictConfig": (EffectKind.GLOBAL_CONFIG, Confidence.HIGH),
    "logging.config.fileConfig": (EffectKind.GLOBAL_CONFIG, Confidence.HIGH),
    "logging.disable": (EffectKind.GLOBAL_CONFIG, Confidence.HIGH),
    "decimal.setcontext": (EffectKind.GLOBAL_CONFIG, Confidence.HIGH),
    "decimal.getcontext": (EffectKind.GLOBAL_CONFIG, Confidence.LOW),
    "gettext.install": (EffectKind.GLOBAL_CONFIG, Confidence.HIGH),
    "gc.disable": (EffectKind.GLOBAL_CONFIG, Confidence.HIGH),
    "gc.freeze": (EffectKind.GLOBAL_CONFIG, Confidence.HIGH),
    # --- Monkeypatching -----------------------------------------------------
    "gevent.monkey.patch_all": (EffectKind.MONKEYPATCH, Confidence.HIGH),
    "eventlet.monkey_patch": (EffectKind.MONKEYPATCH, Confidence.HIGH),
    "setattr": (EffectKind.MONKEYPATCH, Confidence.MEDIUM),
    "delattr": (EffectKind.MONKEYPATCH, Confidence.MEDIUM),
    # --- I/O ----------------------------------------------------------------
    "open": (EffectKind.IO, Confidence.HIGH),
    "os.makedirs": (EffectKind.IO, Confidence.HIGH),
    "os.mkdir": (EffectKind.IO, Confidence.HIGH),
    "os.remove": (EffectKind.IO, Confidence.HIGH),
    "os.unlink": (EffectKind.IO, Confidence.HIGH),
    "os.chdir": (EffectKind.GLOBAL_CONFIG, Confidence.HIGH),
    "os.system": (EffectKind.IO, Confidence.HIGH),
    "os.listdir": (EffectKind.IO, Confidence.MEDIUM),
    "os.walk": (EffectKind.IO, Confidence.MEDIUM),
    "os.stat": (EffectKind.IO, Confidence.MEDIUM),
    "shutil.rmtree": (EffectKind.IO, Confidence.HIGH),
    "shutil.copy": (EffectKind.IO, Confidence.HIGH),
    "shutil.which": (EffectKind.IO, Confidence.MEDIUM),
    "subprocess.run": (EffectKind.IO, Confidence.HIGH),
    "subprocess.check_output": (EffectKind.IO, Confidence.HIGH),
    "subprocess.Popen": (EffectKind.IO, Confidence.HIGH),
    "socket.socket": (EffectKind.IO, Confidence.HIGH),
    "socket.gethostname": (EffectKind.IO, Confidence.MEDIUM),
    "urllib.request.urlopen": (EffectKind.IO, Confidence.HIGH),
    "requests.get": (EffectKind.IO, Confidence.HIGH),
    "requests.post": (EffectKind.IO, Confidence.HIGH),
    "httpx.get": (EffectKind.IO, Confidence.HIGH),
    "tempfile.mkdtemp": (EffectKind.IO, Confidence.HIGH),
    "tempfile.NamedTemporaryFile": (EffectKind.IO, Confidence.HIGH),
    "pathlib.Path.read_text": (EffectKind.IO, Confidence.HIGH),
    "pathlib.Path.read_bytes": (EffectKind.IO, Confidence.HIGH),
    "pathlib.Path.write_text": (EffectKind.IO, Confidence.HIGH),
    "pathlib.Path.mkdir": (EffectKind.IO, Confidence.HIGH),
    "pkgutil.get_data": (EffectKind.IO, Confidence.HIGH),
    "importlib.resources.read_text": (EffectKind.IO, Confidence.HIGH),
    "importlib.metadata.entry_points": (EffectKind.IO, Confidence.HIGH),
    "importlib.metadata.distributions": (EffectKind.IO, Confidence.HIGH),
    # --- Output -------------------------------------------------------------
    "print": (EffectKind.OUTPUT, Confidence.HIGH),
    "warnings.warn": (EffectKind.OUTPUT, Confidence.HIGH),
    "warnings.warn_explicit": (EffectKind.OUTPUT, Confidence.HIGH),
    "sys.stdout.write": (EffectKind.OUTPUT, Confidence.HIGH),
    "sys.stderr.write": (EffectKind.OUTPUT, Confidence.HIGH),
    # --- Concurrency --------------------------------------------------------
    "threading.Thread": (EffectKind.CONCURRENCY, Confidence.HIGH),
    "threading.Timer": (EffectKind.CONCURRENCY, Confidence.HIGH),
    "asyncio.get_event_loop": (EffectKind.CONCURRENCY, Confidence.HIGH),
    "asyncio.new_event_loop": (EffectKind.CONCURRENCY, Confidence.HIGH),
    "asyncio.set_event_loop_policy": (EffectKind.GLOBAL_CONFIG, Confidence.HIGH),
    "concurrent.futures.ThreadPoolExecutor": (EffectKind.CONCURRENCY, Confidence.HIGH),
    "multiprocessing.Pool": (EffectKind.CONCURRENCY, Confidence.HIGH),
    # --- Exit ---------------------------------------------------------------
    "sys.exit": (EffectKind.EXIT, Confidence.HIGH),
    "os._exit": (EffectKind.EXIT, Confidence.HIGH),
    "exit": (EffectKind.EXIT, Confidence.HIGH),
    "quit": (EffectKind.EXIT, Confidence.HIGH),
    # --- Order-dependent values --------------------------------------------
    "time.time": (EffectKind.RANDOMNESS, Confidence.MEDIUM),
    "time.monotonic": (EffectKind.RANDOMNESS, Confidence.MEDIUM),
    "datetime.datetime.now": (EffectKind.RANDOMNESS, Confidence.MEDIUM),
    "datetime.datetime.utcnow": (EffectKind.RANDOMNESS, Confidence.MEDIUM),
    "random.seed": (EffectKind.GLOBAL_CONFIG, Confidence.HIGH),
    "random.random": (EffectKind.RANDOMNESS, Confidence.MEDIUM),
    "uuid.uuid4": (EffectKind.RANDOMNESS, Confidence.MEDIUM),
    "secrets.token_hex": (EffectKind.RANDOMNESS, Confidence.MEDIUM),
    # --- Dynamic imports ----------------------------------------------------
    "importlib.import_module": (EffectKind.DYNAMIC_IMPORT, Confidence.HIGH),
    "importlib.reload": (EffectKind.DYNAMIC_IMPORT, Confidence.HIGH),
    "__import__": (EffectKind.DYNAMIC_IMPORT, Confidence.HIGH),
    "pkgutil.iter_modules": (EffectKind.DYNAMIC_IMPORT, Confidence.MEDIUM),
    "pkgutil.walk_packages": (EffectKind.DYNAMIC_IMPORT, Confidence.HIGH),
}

#: Decorators that only wrap the function they decorate.  A module-level
#: decorator is otherwise treated as possible registration, and without this
#: table every ``@contextmanager`` in the standard library would read as a risk.
PURE_DECORATORS: frozenset[str] = frozenset(
    {
        "staticmethod", "classmethod", "property", "cached_property",
        "abc.abstractmethod", "abc.abstractproperty",
        "contextlib.contextmanager", "contextlib.asynccontextmanager",
        "dataclasses.dataclass", "enum.unique", "enum.member", "enum.nonmember",
        "functools.cache", "functools.lru_cache", "functools.wraps",
        "functools.singledispatch", "functools.singledispatchmethod",
        "functools.total_ordering", "functools.cached_property",
        "typing.overload", "typing.final", "typing.runtime_checkable",
        "typing.no_type_check", "typing.type_check_only",
        "typing_extensions.overload", "typing_extensions.final",
        "typing_extensions.runtime_checkable", "typing_extensions.deprecated",
        "warnings.deprecated",
    }
)

#: Modules CPython imports while starting up, before user code runs.  Deferring
#: an import of one of these cannot save anything: it is already in
#: ``sys.modules`` by the time the module doing the importing is executed.
STARTUP_MODULES: frozenset[str] = frozenset(
    {
        "sys", "builtins", "_imp", "_thread", "_warnings", "_weakref",
        "abc", "codecs", "encodings", "encodings.aliases", "encodings.utf_8",
        "genericpath", "io", "marshal", "os", "os.path", "posixpath",
        "ntpath", "site", "stat", "_collections_abc", "_frozen_importlib",
        "_frozen_importlib_external", "_io", "_sitebuiltins", "zipimport",
    }
)

#: Modules that exist to change global state when imported.  Importing one is
#: the effect, so a lazy import that is never reified simply loses it.
SIDE_EFFECT_MODULES: dict[str, str] = {
    "readline": "installs the interactive line editor as a side effect of import",
    "rlcompleter": "installs tab completion into readline",
    "gevent.monkey": "patches the standard library when imported",
    "eventlet.monkey_patch": "patches the standard library when imported",
    "sitecustomize": "runs site-wide startup configuration",
    "usercustomize": "runs per-user startup configuration",
    "encodings.idna": "registers the IDNA codec",
    "coverage": "hooks the trace function when started at import time",
    "faulthandler": "only useful for its enable() side effect",
    "pytest_asyncio": "registers a pytest plugin at import time",
    "_strptime": "imported for thread-safety pre-initialisation",
    "antigravity": "opens a web browser when imported",
    "this": "prints the Zen of Python when imported",
}

#: Modules known to be free of import-time effects.  Used to stop the transitive
#: walk early, which matters because ``typing`` and ``dataclasses`` sit under a
#: large share of all first-party imports.
PURE_MODULES: frozenset[str] = frozenset(
    {
        "abc", "array", "base64", "binascii", "bisect", "calendar", "cmath",
        "collections", "collections.abc", "colorsys", "contextlib", "copy",
        "dataclasses", "datetime", "difflib", "enum", "errno", "fnmatch",
        "functools", "graphlib", "hashlib", "heapq", "hmac", "html", "inspect",
        "io", "ipaddress", "itertools", "json", "keyword", "math", "numbers",
        "operator", "pathlib", "pprint", "queue", "reprlib", "shlex", "stat",
        "statistics", "string", "struct", "textwrap", "token", "types",
        "typing", "typing_extensions", "unicodedata", "urllib.parse", "uuid",
        "weakref", "zlib",
    }
)


def lookup_call(dotted: str) -> tuple[EffectKind, Confidence] | None:
    """Classify a dotted callee name.

    Returns ``None`` for a call known to be pure, a ``(kind, confidence)`` pair
    for a recognised effect, and ``(UNKNOWN_CALL, LOW)`` for anything else.
    Matching also tries the trailing segments of the name so that
    ``self.registry.register`` and ``foo.bar.warnings.warn`` still land on their
    table entries.
    """
    if dotted in PURE_CALLS:
        return None
    if dotted in EFFECT_CALLS:
        return EFFECT_CALLS[dotted]

    parts = dotted.split(".")
    for start in range(1, len(parts)):
        tail = ".".join(parts[start:])
        if tail in PURE_CALLS:
            return None
        if tail in EFFECT_CALLS:
            return EFFECT_CALLS[tail]

    # A bare `.register(...)` on anything is the single most common shape of
    # import-time registration in plugin-based codebases.
    if parts[-1] in ("register", "register_type", "add_plugin", "subscribe"):
        return EffectKind.REGISTRATION, Confidence.MEDIUM
    if parts[-1] in ("patch", "patch_all", "monkey_patch"):
        return EffectKind.MONKEYPATCH, Confidence.MEDIUM

    return EffectKind.UNKNOWN_CALL, Confidence.LOW


def is_startup_module(module: str) -> bool:
    """Whether ``module`` is already imported before user code runs."""
    return module in STARTUP_MODULES


def side_effect_reason(module: str) -> str | None:
    """Why ``module`` (or a parent of it) must be imported eagerly, if it must."""
    parts = module.split(".")
    for stop in range(len(parts), 0, -1):
        candidate = ".".join(parts[:stop])
        if candidate in SIDE_EFFECT_MODULES:
            return SIDE_EFFECT_MODULES[candidate]
    return None
