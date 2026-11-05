"""Mutation engine: turn a Python function into "deliberately broken" variants.

Design note: we mutate *source text*, not the AST, and we only ever replace the
exact token span that the AST points at. ``ast.unparse`` would re-print the whole
file — losing comments, reformatting code, and shifting line numbers. A mutation
tool that reformats your repository is a mutation tool nobody will trust.

Every mutant records enough information to be shown to a human and to be applied
deterministically: where it lives, what it was, what it became, and why that
particular break is worth checking.
"""

from __future__ import annotations

import ast
import textwrap
from dataclasses import dataclass, field
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------

# How much we care when a mutant survives.
#   critical — a surviving mutant here means money, security or data can be lost
#   normal   — ordinary logic that is simply untested
#   low      — cosmetic; surviving is expected and not worth alarming anyone
SEVERITY_CRITICAL = "critical"
SEVERITY_NORMAL = "normal"
SEVERITY_LOW = "low"


@dataclass(frozen=True)
class Mutant:
    """One deliberate defect in one specific place."""

    id: str
    operator: str
    lineno: int
    col: int
    end_lineno: int
    end_col: int
    original: str
    replacement: str
    description: str
    severity: str = SEVERITY_NORMAL
    # Name of the function this mutant lives in, for grouping in reports.
    function: str = field(default="<module>")

    def apply(self, source: str) -> str:
        """Return ``source`` with this mutant's span replaced.

        Raises ValueError if the span no longer matches what was recorded, which
        protects against applying a mutant to a file that changed underneath us.
        """
        lines = source.splitlines(keepends=True)
        if self.end_lineno > len(lines) or self.lineno < 1:
            raise ValueError(f"mutant {self.id}: span outside file")
        if self.lineno == self.end_lineno:
            line = lines[self.lineno - 1]
            head, tail = line[: self.col], line[self.end_col :]
            if line[self.col : self.end_col] != self.original:
                raise ValueError(f"mutant {self.id}: source drifted")
            lines[self.lineno - 1] = head + self.replacement + tail
            return "".join(lines)

        # Multi-line span: splice across the boundary lines.
        first = lines[self.lineno - 1]
        last = lines[self.end_lineno - 1]
        rebuilt = first[: self.col] + self.replacement + last[self.end_col :]
        lines[self.lineno - 1] = rebuilt
        del lines[self.lineno : self.end_lineno]
        return "".join(lines)


def _seg(source_lines: list[str], lineno: int, col: int, end_lineno: int, end_col: int) -> str:
    """Extract the exact source text covered by an AST span."""
    if lineno == end_lineno:
        return source_lines[lineno - 1][col:end_col]
    parts = [source_lines[lineno - 1][col:]]
    parts.extend(source_lines[lineno : end_lineno - 1])
    parts.append(source_lines[end_lineno - 1][:end_col])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

_COMPARE_FLIP = {
    ast.Lt: (">=", "less-than became greater-or-equal"),
    ast.LtE: (">", "less-or-equal became greater-than"),
    ast.Gt: ("<=", "greater-than became less-or-equal"),
    ast.GtE: ("<", "greater-or-equal became less-than"),
    ast.Eq: ("!=", "equality inverted"),
    ast.NotEq: ("==", "inequality inverted"),
    ast.In: ("not in", "membership inverted"),
    ast.NotIn: ("in", "membership inverted"),
    ast.Is: ("is not", "identity check inverted"),
    ast.IsNot: ("is", "identity check inverted"),
}

_BINOP_FLIP = {
    ast.Add: (ast.Sub, "-", "add became subtract"),
    ast.Sub: (ast.Add, "+", "subtract became add"),
    ast.Mult: (ast.Div, "/", "multiply became divide"),
    ast.Div: (ast.Mult, "*", "divide became multiply"),
    ast.FloorDiv: (ast.Mult, "*", "floor-divide became multiply"),
    ast.Mod: (ast.Mult, "*", "modulo became multiply"),
}

_BOOLOP_FLIP = {ast.And: (ast.Or, "or", "and became or"), ast.Or: (ast.And, "and", "or became and")}

# Comparisons whose failure usually costs money rather than style points.
_MONEY_HINTS = ("price", "amount", "total", "cost", "balance", "salary", "tax", "discount",
                "fee", "payment", "refund", "credit", "debit", "invoice", "rate", "interest")
_SECURITY_HINTS = ("auth", "token", "password", "permission", "role", "admin", "secret",
                   "signature", "verify", "allow", "deny", "access", "credential", "session")


def _severity_for(function: str, operator: str) -> str:
    name = function.lower()
    if any(h in name for h in _MONEY_HINTS) or any(h in name for h in _SECURITY_HINTS):
        if operator in ("compare", "boundary", "boolop", "return_none"):
            return SEVERITY_CRITICAL
    if operator in ("return_none", "boundary"):
        return SEVERITY_NORMAL
    return SEVERITY_NORMAL


class _Mutator:
    """Walks a function body and emits candidate mutants."""

    def __init__(self, source: str, function: str):
        self.source_lines = source.splitlines(keepends=True)
        self.function = function
        self.mutants: list[Mutant] = []

    def _add(self, operator: str, node: ast.AST, original: str, replacement: str, description: str) -> None:
        if original == replacement:
            return
        self.mutants.append(
            Mutant(
                id="",
                operator=operator,
                lineno=node.lineno,  # type: ignore[attr-defined]
                col=node.col_offset,  # type: ignore[attr-defined]
                end_lineno=node.end_lineno,  # type: ignore[attr-defined]
                end_col=node.end_col_offset,  # type: ignore[attr-defined]
                original=original,
                replacement=replacement,
                description=description,
                severity=_severity_for(self.function, operator),
                function=self.function,
            )
        )

    # -- individual operators ------------------------------------------------

    def visit(self, node: ast.AST, depth: int = 0) -> None:
        if depth > 60:  # pathological nesting guard
            return

        if isinstance(node, ast.Compare):
            self._compare(node)

        elif isinstance(node, ast.BinOp):
            self._binop(node)

        elif isinstance(node, ast.BoolOp):
            self._boolop(node)

        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            self._not(node)

        elif isinstance(node, ast.Return) and node.value is not None:
            self._return_none(node)

        elif isinstance(node, ast.Constant):
            self._constant(node)

        for child in ast.iter_child_nodes(node):
            self.visit(child, depth + 1)

    def _compare(self, node: ast.Compare) -> None:
        # Only single-comparison expressions; chained ones are rare and the
        # token span is ambiguous, so we skip rather than guess.
        if len(node.ops) != 1:
            return
        op = node.ops[0]
        flip = _COMPARE_FLIP.get(type(op))
        if not flip:
            return
        symbol, what = flip
        left, right = node.left, node.comparators[0]
        # The operator token sits between left.end and right.start on one line.
        if left.end_lineno != right.lineno:
            return
        col, end_col = left.end_col_offset, right.col_offset
        original = self.source_lines[left.end_lineno - 1][col:end_col].strip()
        if not original:
            return
        pad = self.source_lines[left.end_lineno - 1][col:end_col]
        lead = pad[: len(pad) - len(pad.lstrip())]
        trail = pad[len(pad.rstrip()) :]
        self._add(
            "compare",
            _Span(node.lineno, col, left.end_lineno, end_col),
            pad,  # type: ignore[arg-type]
            lead + symbol + trail,
            f"comparison {original} → {symbol} ({what})",
        )
        # A dedicated off-by-one mutant: >= becomes >. This is the single most
        # common real-world defect and the one AI-written tests miss most.
        if isinstance(op, (ast.GtE, ast.LtE)):
            tightened = ">" if isinstance(op, ast.GtE) else "<"
            self._add(
                "boundary",
                _Span(node.lineno, col, left.end_lineno, end_col),
                pad,  # type: ignore[arg-type]
                lead + tightened + trail,
                f"off-by-one: {original} → {tightened} (behaviour changes exactly at the threshold)",
            )

    def _binop(self, node: ast.BinOp) -> None:
        flip = _BINOP_FLIP.get(type(node.op))
        if not flip:
            return
        _, symbol, what = flip
        left, right = node.left, node.right
        if left.end_lineno != right.lineno:
            return
        col, end_col = left.end_col_offset, right.col_offset
        pad = self.source_lines[left.end_lineno - 1][col:end_col]
        if not pad.strip():
            return
        lead = pad[: len(pad) - len(pad.lstrip())]
        trail = pad[len(pad.rstrip()) :]
        self._add("binop", _Span(node.lineno, col, left.end_lineno, end_col),
                  pad, lead + symbol + trail, f"arithmetic swap ({what})")

    def _boolop(self, node: ast.BoolOp) -> None:
        flip = _BOOLOP_FLIP.get(type(node.op))
        if not flip:
            return
        _, symbol, what = flip
        # Replace every "and"/"or" keyword inside this span.
        original = _seg(self.source_lines, node.lineno, node.col_offset, node.end_lineno,
                        node.end_col_offset)  # type: ignore[arg-type]
        keyword = "and" if isinstance(node.op, ast.And) else "or"
        replacement = original.replace(f" {keyword} ", f" {symbol} ")
        if replacement == original:
            return
        self._add("boolop", node, original, replacement, f"boolean connective swap ({what})")

    def _not(self, node: ast.UnaryOp) -> None:
        inner = node.operand
        original = _seg(self.source_lines, node.lineno, node.col_offset, node.end_lineno,
                        node.end_col_offset)  # type: ignore[arg-type]
        inner_src = _seg(self.source_lines, inner.lineno, inner.col_offset, inner.end_lineno,
                         inner.end_col_offset)  # type: ignore[attr-defined]
        if not original.startswith("not "):
            return
        self._add("not", node, original, inner_src, "negation dropped: the polarity of the condition inverts")

    def _return_none(self, node: ast.Return) -> None:
        original = _seg(self.source_lines, node.lineno, node.col_offset, node.end_lineno,
                        node.end_col_offset)  # type: ignore[arg-type]
        if original.strip() == "return None":
            return
        indent = self.source_lines[node.lineno - 1][: node.col_offset]
        self._add("return_none", node, original, f"{indent}return None",
                  "return value discarded (the function returns nothing)")

    def _constant(self, node: ast.Constant) -> None:
        value = node.value
        if isinstance(value, bool):
            self._add("constant", node, _seg(self.source_lines, node.lineno, node.col_offset,
                                             node.end_lineno, node.end_col_offset),  # type: ignore[arg-type]
                      "False" if value else "True", "boolean constant flipped")
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            text = _seg(self.source_lines, node.lineno, node.col_offset,
                        node.end_lineno, node.end_col_offset)  # type: ignore[arg-type]
            if text != str(value):
                return  # written as an expression (e.g. 1e3, 0x10); skip
            self._add("constant", node, text, repr(value + 1), "numeric constant +1")


@dataclass(frozen=True)
class _Span:
    """A source span, shaped like an AST node so _add can use it."""

    lineno: int
    col_offset: int
    end_lineno: int
    end_col_offset: int


def find_functions(tree: ast.AST) -> dict[str, ast.AST]:
    """Map ``qualname`` (e.g. ``Order.total``) to its FunctionDef node."""
    found: dict[str, ast.AST] = {}

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = f"{prefix}{child.name}"
                found[qual] = child
                walk(child, qual + ".")
            elif isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            else:
                walk(child, prefix)

    walk(tree, "")
    return found


def mutants_for_source(
    source: str,
    function: Optional[str] = None,
    operators: Optional[Iterable[str]] = None,
) -> list[Mutant]:
    """Generate mutants for one file, optionally limited to one function."""
    tree = ast.parse(source)
    functions = find_functions(tree)
    if function is not None:
        if function not in functions:
            raise KeyError(f"function {function!r} not found in file")
        targets = {function: functions[function]}
    else:
        targets = functions

    wanted = set(operators) if operators else None
    out: list[Mutant] = []
    for qual, node in targets.items():
        m = _Mutator(source, qual)
        for stmt in node.body:  # type: ignore[attr-defined]
            m.visit(stmt)
        for mutant in m.mutants:
            if wanted and mutant.operator not in wanted:
                continue
            out.append(mutant)

    # Stable ids: M001, M002, ... ordered by position so two runs on the same
    # source always agree.
    ordered = sorted(out, key=lambda x: (x.lineno, x.col, x.operator))
    return [
        Mutant(
            id=f"M{i:03d}",
            operator=x.operator,
            lineno=x.lineno,
            col=x.col,
            end_lineno=x.end_lineno,
            end_col=x.end_col,
            original=x.original,
            replacement=x.replacement,
            description=x.description,
            severity=x.severity,
            function=x.function,
        )
        for i, x in enumerate(ordered, start=1)
    ]


# ---------------------------------------------------------------------------
# Safety: make sure a "mutant" really is different code
# ---------------------------------------------------------------------------

def renumber(mutants: Iterable[Mutant]) -> list[Mutant]:
    """Reassign ids in source order.

    Ids are generated per function, so concatenating mutants from several
    functions would otherwise produce two different "M001"s in one report —
    which is exactly the kind of quiet ambiguity this tool is supposed to catch,
    not create.
    """
    import dataclasses

    ordered = sorted(mutants, key=lambda m: (m.lineno, m.col, m.operator))
    return [
        dataclasses.replace(m, id=f"M{i:03d}")
        for i, m in enumerate(ordered, start=1)
    ]


def is_equivalent(source: str, mutant: Mutant) -> bool:
    """True when applying the mutant produces byte-identical source.

    A mutation tool that silently does nothing is exactly the failure mode it
    exists to catch, so every candidate is checked before it is trusted.
    """
    try:
        mutated = mutant.apply(source)
    except ValueError:
        return False
    if mutated == source:
        return True
    try:
        ast.parse(mutated)
    except SyntaxError:
        return True  # broken syntax is not a useful mutant
    return False
