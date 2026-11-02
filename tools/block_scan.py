#!/usr/bin/env python3
"""Find blocking calls made directly inside `async def` functions.

Usage:  python3 tools/block_scan.py moviebot/bot.py [more.py ...] [-v]
Exit:   0 clean, 1 findings (one `file:line:function: ...` line each), 2 usage or parse error.
        -v also lists every function the scanner considers blocking, and why.

Why: an `async def` runs on the event loop, so one blocking call inside it (an HTTP request, a
sleep, a subprocess) freezes every other handler until it returns. For the bot it is worse than
slow: Radarr and Sonarr deliver webhooks synchronously from inside their own API calls, so a
blocking call to Radarr made on the event loop waits for Radarr while Radarr waits for the bot's
webhook. Blocking work belongs in `await asyncio.to_thread(fn, ...)` or `loop.run_in_executor(...)`.

Heuristics (standard library `ast` only, each file on its own):
1. Seeds, i.e. calls that block by nature, resolved through the file's imports (so
   `import requests as rq` and `from time import sleep` are followed):
     requests.<anything>()       except helpers that do no I/O: Session, Request, exceptions, ...
     urllib.request.urlopen()
     time.sleep()
     subprocess.<anything>(), os.system(), os.popen()
     socket.<anything>()         except pure helpers (htons, inet_aton, gethostname, ...)
     shutil.disk_usage(), os.statvfs(), os.path.ismount()   (they hang on a dead network mount)
   plus any method call on a name, or an attribute, that was assigned from requests.Session(),
   urllib.request.build_opener(), http.client.HTTP(S)Connection(), socket.socket(),
   socket.create_connection() or subprocess.Popen() anywhere in the file (with `as` targets of
   `with` statements too): `_QS = requests.Session()` makes `_QS.get(...)` a seed.
2. Wrappers: a plain `def` (module level, nested or a method) that calls a seed or another
   blocking `def` is blocking too, repeated until nothing changes. Calls are matched by name only:
   `foo()` to the functions named foo, `self.foo()` / `cls.foo()` to the enclosing class's method
   foo, `Klass.foo()` to Klass's method foo. Calls on any other object are not followed (no type
   inference), and a lambda inside a def counts as part of that def.
3. Check: in each `async def` body, every call of a seed or a blocking function is reported unless
   it sits inside the arguments of asyncio.to_thread(...) or <anything>.run_in_executor(...), where
   it runs in a worker thread (`await asyncio.to_thread(lambda: fetch(x))` is fine). Nested `def`s
   are skipped (they run when called, and are checked as functions); lambdas, comprehensions and
   generator expressions are checked, because they usually run right there (a sort key, say).
Not covered: blocking code in other modules (only the given files are read), dynamic calls
(getattr, callbacks kept in containers), and plain file I/O (fast on a local disk; send slow
paths through a thread by hand).
"""
from __future__ import annotations

import ast
import sys

SEED_EXACT = {
    "urllib.request.urlopen", "time.sleep", "os.system", "os.popen",
    "shutil.disk_usage", "os.statvfs", "os.path.ismount",
}
SEED_MODULES = ("requests", "subprocess", "socket")
NOT_BLOCKING = {
    "requests.Session", "requests.session", "requests.Request", "requests.PreparedRequest",
    "requests.Response", "requests.codes", "requests.exceptions", "requests.utils", "requests.adapters",
    "requests.auth", "requests.structures", "requests.cookies", "requests.models", "requests.status_codes",
    "requests.compat", "requests.RequestException", "requests.Timeout", "requests.ConnectionError",
    "requests.HTTPError",
    "subprocess.CalledProcessError", "subprocess.TimeoutExpired", "subprocess.CompletedProcess",
    "socket.htons", "socket.ntohs", "socket.htonl", "socket.ntohl", "socket.inet_aton", "socket.inet_ntoa",
    "socket.inet_pton", "socket.inet_ntop", "socket.gethostname", "socket.timeout", "socket.error",
    "socket.gaierror", "socket.herror",
}
CONNECTION_FACTORIES = {
    "requests.Session", "requests.session", "urllib.request.build_opener", "http.client.HTTPConnection",
    "http.client.HTTPSConnection", "socket.socket", "socket.create_connection", "subprocess.Popen",
}
THREAD_WRAPPERS = {"asyncio.to_thread"}
EXECUTOR_METHODS = {"run_in_executor"}


def _dotted(expr):
    """'a.b.c' for a Name/Attribute chain, else None."""
    parts = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if isinstance(expr, ast.Name):
        parts.append(expr.id)
        return ".".join(reversed(parts))
    return None


class Module:
    """One parsed file: imports, connection objects, functions, and the blocking set."""

    def __init__(self, path, source):
        self.path = path
        self.tree = ast.parse(source, filename=path)
        self.aliases = {}          # local name -> qualified module path
        self.conn_names = set()    # names assigned from a connection factory
        self.conn_attrs = set()    # attribute names assigned from one (self.session = ...)
        self.classes = set()
        self.sync_defs = []        # (key, node, class name or None)
        self.async_defs = []       # (node, class name or None)
        self.blocking = {}         # key -> (shown callee, next key or None)
        self._collect_imports()
        self._collect_defs(self.tree, None)
        self._collect_connections()
        self._propagate()

    # ---- imports and connection objects
    def _collect_imports(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        self.aliases[a.asname] = a.name
                    else:
                        top = a.name.split(".")[0]
                        self.aliases[top] = top
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                for a in node.names:
                    self.aliases[a.asname or a.name] = f"{node.module}.{a.name}"

    def qualify(self, func):
        """Fully qualified name of a called expression when its root is an imported name."""
        dotted = _dotted(func)
        if not dotted:
            return None
        root, _, rest = dotted.partition(".")
        if root not in self.aliases:
            return None
        base = self.aliases[root]
        return f"{base}.{rest}" if rest else base

    def _collect_connections(self):
        for node in ast.walk(self.tree):
            pairs = []
            if isinstance(node, ast.Assign):
                pairs = [(t, node.value) for t in node.targets]
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                pairs = [(node.target, node.value)]
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                pairs = [(i.optional_vars, i.context_expr) for i in node.items if i.optional_vars is not None]
            for target, value in pairs:
                if isinstance(value, ast.Call) and self.qualify(value.func) in CONNECTION_FACTORIES:
                    if isinstance(target, ast.Name):
                        self.conn_names.add(target.id)
                    elif isinstance(target, ast.Attribute):
                        self.conn_attrs.add(target.attr)

    # ---- functions
    def _collect_defs(self, node, cls):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                self.classes.add(child.name)
                self._collect_defs(child, child.name)
            elif isinstance(child, ast.FunctionDef):
                key = ("method", cls, child.name) if cls else ("func", child.name)
                self.sync_defs.append((key, child, cls))
                self._collect_defs(child, None)        # nested defs are plain functions
            elif isinstance(child, ast.AsyncFunctionDef):
                self.async_defs.append((child, cls))
                self._collect_defs(child, None)
            else:
                self._collect_defs(child, cls)

    def seed(self, call):
        """The blocking primitive a call is, or None."""
        q = self.qualify(call.func)
        if q:
            if any(q == n or q.startswith(n + ".") for n in NOT_BLOCKING):
                return None
            if q in SEED_EXACT or q.split(".")[0] in SEED_MODULES:
                return q
        f = call.func
        if isinstance(f, ast.Attribute):
            recv = f.value
            if isinstance(recv, ast.Name) and recv.id in self.conn_names:
                return f"{recv.id}.{f.attr}"
            if isinstance(recv, ast.Attribute) and recv.attr in self.conn_attrs:
                return f"{recv.attr}.{f.attr}"
        return None

    def callee_key(self, call, cls):
        """Key of the module function or method this call goes to, when it can be told by name."""
        f = call.func
        if isinstance(f, ast.Name):
            return ("func", f.id)
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            owner = f.value.id
            if owner in ("self", "cls") and cls:
                return ("method", cls, f.attr)
            if owner in self.classes:
                return ("method", owner, f.attr)
        return None

    def _propagate(self):
        changed = True
        while changed:
            changed = False
            for key, node, cls in self.sync_defs:
                if key in self.blocking:
                    continue
                for call in calls_in(node, skip_wrapped=False):
                    s = self.seed(call)
                    if s:
                        self.blocking[key] = (s, None)
                        changed = True
                        break
                    ck = self.callee_key(call, cls)
                    if ck and ck in self.blocking and ck != key:
                        self.blocking[key] = (_show(ck), ck)
                        changed = True
                        break

    def chain(self, key):
        out, seen = [_show(key)], {key}
        while key in self.blocking:
            shown, nxt = self.blocking[key]
            if nxt is None:
                out.append(shown)
                break
            if nxt in seen:
                break
            seen.add(nxt)
            out.append(_show(nxt))
            key = nxt
        return " -> ".join(out)

    def findings(self):
        out = []
        for node, cls in self.async_defs:
            for call in calls_in(node, skip_wrapped=True, module=self):
                s = self.seed(call)
                if s:
                    out.append((call.lineno, node.name, f"{s}() blocks"))
                    continue
                ck = self.callee_key(call, cls)
                if ck and ck in self.blocking:
                    out.append((call.lineno, node.name, f"{_show(ck)}() blocks ({self.chain(ck)})"))
        return sorted(out)


def _show(key):
    return key[1] if key[0] == "func" else f"{key[1]}.{key[2]}"


def _is_thread_wrapper(call, module):
    f = call.func
    if isinstance(f, ast.Attribute) and f.attr in EXECUTOR_METHODS:
        return True
    return module is not None and module.qualify(f) in THREAD_WRAPPERS


def calls_in(func_node, skip_wrapped, module=None):
    """Every Call in a function body, skipping nested defs and classes; with skip_wrapped, the
    arguments of to_thread / run_in_executor calls are skipped as well."""
    found = []

    def visit(node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return
        if isinstance(node, ast.Call):
            found.append(node)
            if skip_wrapped and _is_thread_wrapper(node, module):
                visit(node.func)
                return
        for child in ast.iter_child_nodes(node):
            visit(child)

    for stmt in func_node.body:
        visit(stmt)
    return found


def main(argv):
    verbose = "-v" in argv or "--verbose" in argv
    paths = [a for a in argv if not a.startswith("-")]
    if not paths or "-h" in argv or "--help" in argv:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    total = 0
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                mod = Module(path, fh.read())
        except (OSError, SyntaxError, ValueError) as e:
            print(f"{path}: cannot scan: {e}", file=sys.stderr)
            return 2
        if verbose:
            for key in sorted(mod.blocking, key=_show):
                print(f"{path}: blocking {mod.chain(key)}")
        for line, fname, what in mod.findings():
            print(f"{path}:{line}:{fname}: {what}; call it through await asyncio.to_thread(...)")
            total += 1
    if total:
        print(f"{total} blocking call(s) inside async functions", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
