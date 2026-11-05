"""Find the tests that actually exercise a given module.

Running the whole suite once per mutant is why mutation testing has a reputation
for being too slow to use. Narrowing to the files that import the module under
test usually cuts the cost by an order of magnitude, and it is a heuristic we can
explain in one sentence instead of a dependency-graph engine we cannot.

When the heuristic finds nothing we say so and fall back to the full suite rather
than silently reporting a perfect score on a suite that never ran.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Iterable, Optional

_SKIP_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", "__pycache__",
    ".tox", ".nox", "build", "dist", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "site-packages", ".eggs",
}

_TEST_FILE = re.compile(r"^(test_.*|.*_test)\.py$")


@dataclass
class TestTarget:
    files: list[str]
    reason: str

    @property
    def is_empty(self) -> bool:
        return not self.files


def _walk(root: str) -> Iterable[str]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if _TEST_FILE.match(name):
                yield os.path.join(dirpath, name)


def _imports_module(text: str, module: str) -> bool:
    short = module.rsplit(".", 1)[-1]
    patterns = (
        rf"^\s*import\s+{re.escape(module)}\b",
        rf"^\s*import\s+{re.escape(short)}\b",
        rf"^\s*from\s+{re.escape(module)}\b",
        rf"^\s*from\s+\.*{re.escape(short)}\s+import\b",
        rf"^\s*from\s+\.+.*\b{re.escape(short)}\b",
    )
    return any(re.search(p, text, re.MULTILINE) for p in patterns)


def find_tests(root: str, module: Optional[str], limit: Optional[int] = None) -> TestTarget:
    """Locate test files related to ``module`` (dotted name, may be None)."""
    all_tests = sorted(_walk(root))
    if limit is not None:
        all_tests = all_tests[:limit]

    if not all_tests:
        return TestTarget(files=[], reason="no test files found")

    if not module:
        return TestTarget(files=all_tests, reason="no module filter given; using every test file")

    matched: list[str] = []
    for path in all_tests:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                head = fh.read(200_000)
        except OSError:
            continue
        if _imports_module(head, module):
            matched.append(path)

    if matched:
        return TestTarget(
            files=matched,
            reason=f"{len(matched)} of {len(all_tests)} test file(s) import {module}",
        )
    return TestTarget(
        files=all_tests,
        reason=f"no test file imports {module}; falling back to the full suite",
    )


def guess_module_name(root: str, target_file: str) -> Optional[str]:
    """Best-effort dotted module name for a file, e.g. ``src/shop/pricing.py`` → ``shop.pricing``."""
    rel = os.path.relpath(target_file, root).replace(os.sep, "/")
    if rel.endswith(".py"):
        rel = rel[:-3]
    parts = [p for p in rel.split("/") if p]
    # Drop a leading src/ or lib/ directory, which is not part of the import path.
    while parts and parts[0] in ("src", "lib", "source"):
        parts.pop(0)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) if parts else None
