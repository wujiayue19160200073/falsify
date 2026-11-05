"""Sandboxed worker: find an input that separates the original from the mutant.

Runs as its own process. Reads a JSON payload, writes a JSON result next to it.
Results go to a file rather than stdout because the target module may print on
import, and a tool that reports garbage because of someone else's ``print`` is
not a tool anyone keeps.
"""

from __future__ import annotations

import ast
import itertools
import json
import math
import random
import sys
import traceback
import types

MAX_RANDOM_TRIES = 300
MAX_REPR = 120


def _load(source: str, name: str, path: str) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__file__ = path
    module.__dict__["__name__"] = name
    exec(compile(source, path, "exec"), module.__dict__)
    return module


def _resolve(module: types.ModuleType, qualname: str):
    """Find a callable by qualname, instantiating a class if needed."""
    parts = qualname.split(".")
    obj = module
    for part in parts[:-1]:
        obj = getattr(obj, part)
    attr = getattr(obj, parts[-1])
    if isinstance(obj, type) and callable(attr):
        # Unbound method: try a zero-argument instance.
        try:
            return attr(obj()), 1  # drop the bound `self`
        except Exception:
            return None, 0
    return attr, 0


def _numeric_consts(node: ast.AST) -> list[float]:
    seen: list[float] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, (int, float)) \
                and not isinstance(child.value, bool):
            if child.value not in seen:
                seen.append(child.value)
    return seen


def _str_consts(node: ast.AST) -> list[str]:
    seen: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            if child.value not in seen:
                seen.append(child.value)
    return seen


def _pool_for(annotation: str | None, nums: list[float], strs: list[str]) -> list:
    """Candidate values for one parameter, biased toward real boundaries.

    Negative numbers come last on purpose. ``apply_discount(2, -1, True)`` is a
    technically valid counterexample and a completely useless one: a negative
    coupon cannot happen in the business, so the reader dismisses the finding.
    We would rather search a little longer than hand someone an input they will
    rightly ignore.
    """
    if annotation in ("bool",):
        return [True, False]
    if annotation in ("str", "Optional[str]"):
        base = ["", "a", "0", "x"]
        base.extend(strs)
        return base
    if annotation in ("int",):
        base = [0, 1, 2, 10, 100]
        for n in nums:
            if float(n).is_integer():
                base.extend([int(n) - 1, int(n), int(n) + 1])
        base.append(-1)
        return _dedupe(base)
    if annotation in ("float",):
        base = [0.0, 1.0, 0.5, 100.0]
        for n in nums:
            base.extend([n - 1, n, n + 1, n - 0.01, n + 0.01])
        base.extend([-1.0, -0.5])
        return _dedupe(base)

    # Unknown type: cover every shape the body hints at.
    base: list = [0, 1, 0.0, 1.0, "", "a", [], {}, True, False]
    for n in nums:
        base.extend([n - 1, n, n + 1])
    base.extend(strs)
    base.extend([None, -1])
    return _dedupe(base)


def _dedupe(values: list) -> list:
    out, seen = [], set()
    for v in values:
        key = (type(v).__name__, repr(v))
        if key not in seen:
            seen.add(key)
            out.append(v)
    return out


def _signature(func, node: ast.AST):
    """Return (param_names, annotations, defaults) for the callable."""
    import inspect

    try:
        sig = inspect.signature(func)
    except (ValueError, TypeError):
        return None
    names, anns, defaults = [], [], {}
    for name, p in sig.parameters.items():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            return None
        if p.kind == p.KEYWORD_ONLY:
            return None  # keep it simple: positional only
        names.append(name)
        ann = None
        if p.annotation is not inspect.Parameter.empty and isinstance(p.annotation, str):
            ann = p.annotation
        elif p.annotation is not inspect.Parameter.empty:
            ann = getattr(p.annotation, "__name__", None)
        anns.append(ann)
        if p.default is not inspect.Parameter.empty:
            defaults[name] = p.default
    return names, anns, defaults


def _same(a, b) -> bool:
    if type(a) is not type(b):
        return False
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) and math.isnan(b):
            return True
        return math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)
    try:
        return bool(a == b)
    except Exception:
        return repr(a) == repr(b)


def _call(func, args):
    try:
        return ("value", func(*args))
    except Exception as exc:  # noqa: BLE001 - any failure is an observable outcome
        return ("raise", f"{type(exc).__name__}: {exc}"[:MAX_REPR])


def _differs(orig, mut) -> bool:
    (okind, oval), (mkind, mval) = orig, mut
    if okind != mkind:
        return True
    if okind == "raise":
        return oval.split(":", 1)[0] != mval.split(":", 1)[0] or oval != mval
    return not _same(oval, mval)


def _render(kind, value) -> str:
    if kind == "raise":
        return f"raised {value}"
    return repr(value)[:MAX_REPR]


def run(payload_path: str) -> dict:
    with open(payload_path, encoding="utf-8") as fh:
        payload = json.load(fh)

    orig_source = payload["orig"]
    mut_source = payload["mut"]
    qualname = payload["func"]
    path = payload["path"]
    budget = int(payload.get("max_calls", 400))

    try:
        orig_mod = _load(orig_source, "__falsify_orig__", path)
        mut_mod = _load(mut_source, "__falsify_mut__", path)
    except Exception as exc:  # noqa: BLE001
        return {"kind": "unsupported", "note": f"module could not be loaded twice: {type(exc).__name__}"}

    orig_func, drop = _resolve(orig_mod, qualname)
    mut_func, _ = _resolve(mut_mod, qualname)
    if not callable(orig_func) or not callable(mut_func):
        return {"kind": "unsupported", "note": "target is not a plain callable"}

    tree = ast.parse(orig_source)

    def find(node, name):
        for child in ast.walk(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == name:
                return child
        return None

    node = find(tree, qualname.rsplit(".", 1)[-1])
    # Scan the whole module, not just the function body: thresholds usually live
    # in module-level constants (VIP_THRESHOLD = 200.0), and a counterexample
    # search that cannot see the threshold can never hit the boundary.
    nums = _numeric_consts(tree)
    strs = _str_consts(tree)

    sig = _signature(orig_func, node) if node else None
    if sig is None:
        return {"kind": "unsupported", "note": "unsupported signature"}
    names, anns, defaults = sig

    # A bound method consumed its `self` already, but the signature still lists it.
    if drop and names and names[0] in ("self", "cls"):
        names, anns = names[1:], anns[1:]

    if not names:
        o, m = _call(orig_func, []), _call(mut_func, [])
        if _differs(o, m):
            return {"kind": "value" if o[0] == "value" else "exception", "args": [], "kwargs": {},
                    "orig_result": _render(*o), "mutant_result": _render(*m)}
        return {"kind": "no_witness", "note": "no argument to vary"}

    pools = [_pool_for(anns[i], nums, strs) for i in range(len(names))]
    pools = [p if p else [None] for p in pools]

    def zeroish(pool):
        for v in pool:
            if v in (0, 0.0, "", False, [], {}):
                return v
        return pool[0]

    baseline = [defaults.get(n, zeroish(p)) for n, p in zip(names, pools)]

    # Layered probing. The naive "vary one parameter, leave the rest at default"
    # sweep fails on any guard clause: with vip=False, `vip and price >= 200`
    # short-circuits and no value of price ever reaches the code under test. So
    # every numeric sweep is repeated once per boolean combination.
    bool_idx = [i for i, a in enumerate(anns) if a == "bool"]
    other_idx = [i for i in range(len(names)) if i not in bool_idx]
    if 0 < len(bool_idx) <= 6:
        bool_combos = list(itertools.product([True, False], repeat=len(bool_idx)))
    else:
        bool_combos = [tuple(baseline[i] for i in bool_idx)]

    probes: list[list] = [list(baseline)]
    seen: set[str] = {repr(baseline)}
    budget = max(budget, 50)

    def add(probe: list) -> None:
        key = repr(probe)
        if key not in seen:
            seen.add(key)
            probes.append(probe)

    for combo in bool_combos:
        base = list(baseline)
        for idx, val in zip(bool_idx, combo):
            base[idx] = val
        add(list(base))

        # Sweep each non-boolean parameter through its boundary candidates.
        for i in other_idx:
            for value in pools[i]:
                probe = list(base)
                probe[i] = value
                add(probe)

        # Every numeric parameter at its "largest" candidate at once, so compound
        # conditions like `vip and price >= 200` can actually become true.
        probe = list(base)
        for i in other_idx:
            probe[i] = pools[i][-1]
        add(probe)

    # Randomized fill, to escape the corners the sweeps missed.
    rng = random.Random(20261105)
    attempts = 0
    while len(probes) < budget and attempts < MAX_RANDOM_TRIES * 4:
        attempts += 1
        add([rng.choice(p) for p in pools])

    for probe in probes[:budget]:
        o = _call(orig_func, probe)
        m = _call(mut_func, probe)
        if _differs(o, m):
            return {
                "kind": "value" if o[0] == "value" else "exception",
                "args": [_safe_json(v) for v in probe],
                "kwargs": {},
                "orig_result": _render(*o),
                "mutant_result": _render(*m),
            }

    return {"kind": "no_witness", "note": f"tried {len(probes[:budget])} inputs, no divergence"}


def _safe_json(value):
    try:
        json.dumps(value)
        return value
    except Exception:  # noqa: BLE001
        return f"<{type(value).__name__}>"


def main() -> int:
    if len(sys.argv) < 2:
        return 2
    payload_path = sys.argv[1]
    try:
        result = run(payload_path)
    except Exception:  # noqa: BLE001
        result = {"kind": "unsupported", "note": "worker crashed: " +
                  traceback.format_exc(limit=1).strip().splitlines()[-1]}
    with open(payload_path + ".out", "w", encoding="utf-8") as fh:
        json.dump(result, fh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
