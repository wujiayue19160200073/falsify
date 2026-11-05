"""Work out which functions a change actually touched.

Mutation testing the whole repository is a 40-minute job that nobody schedules.
Mutation testing the three functions this pull request changed is a two-minute
job that fits in CI. The difference is entirely about scoping, so this module
does the scoping honestly: real ``git diff`` line numbers, mapped onto real AST
function bodies, with no guessing about "related" code.
"""

from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import dataclass
from typing import Optional

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Changed:
    path: str          # path as it exists in the working tree
    lines: set[int]    # 1-based line numbers added or modified


def git_available(root: str) -> bool:
    try:
        proc = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=root,
                              capture_output=True, text=True, timeout=10)
        return proc.returncode == 0 and proc.stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def diff_against(root: str, base: str, staged: bool = False) -> list[Changed]:
    """Return the added/modified line numbers per Python file."""
    args = ["git", "diff", "--unified=0", "--no-color"]
    if staged:
        args.append("--cached")
    else:
        args.extend([f"{base}...HEAD"])
    args.append("--")

    try:
        proc = subprocess.run(args, cwd=root, capture_output=True, text=True,
                              timeout=60, encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []

    return parse_diff(proc.stdout)


def parse_diff(text: str) -> list[Changed]:
    """Parse unified diff text into per-file changed line sets."""
    changes: list[Changed] = []
    current: Optional[Changed] = None

    for line in text.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            if target == "/dev/null":
                current = None
                continue
            path = target[2:] if target.startswith("b/") else target
            if not path.endswith(".py"):
                current = None
                continue
            current = Changed(path=path, lines=set())
            changes.append(current)
        elif line.startswith("@@") and current is not None:
            match = _HUNK.match(line)
            if not match:
                continue
            start = int(match.group(1))
            count = int(match.group(2)) if match.group(2) is not None else 1
            for offset in range(count):
                current.lines.add(start + offset)

    return [c for c in changes if c.lines]


def functions_at(source: str, lines: set[int]) -> list[str]:
    """Which function qualnames contain any of ``lines``?"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    hits: list[str] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = f"{prefix}{child.name}"
                start = min([child.lineno] + [d.lineno for d in child.decorator_list])
                end = getattr(child, "end_lineno", child.lineno)
                if any(start <= ln <= end for ln in lines):
                    hits.append(qual)
                walk(child, qual + ".")
            elif isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            else:
                walk(child, prefix)

    walk(tree, "")
    return hits


def changed_functions(root: str, base: str, staged: bool = False) -> dict[str, list[str]]:
    """Map file path → qualnames touched by the change."""
    import os

    out: dict[str, list[str]] = {}
    for change in diff_against(root, base, staged):
        full = os.path.join(root, change.path)
        if not os.path.exists(full):
            continue
        try:
            with open(full, encoding="utf-8", errors="replace") as fh:
                source = fh.read()
        except OSError:
            continue
        names = functions_at(source, change.lines)
        if names:
            out[change.path] = names
    return out
