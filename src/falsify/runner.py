"""Run mutants against the real test suite, one at a time, without losing code.

The hard requirement here is not speed, it is *never damaging the working tree*.
A mutation tool writes broken code into your source files on purpose; if it can
lose that source file, nobody will run it twice. So every run keeps an on-disk
backup, restores in a ``finally`` block, and verifies the restore actually
succeeded before returning.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from .mutate import Mutant

KILLED = "killed"
SURVIVED = "survived"
TIMEOUT = "timeout"
ERROR = "error"

BACKUP_DIR = ".falsify/backup"


@dataclass
class Outcome:
    mutant: Mutant
    status: str
    duration: float
    detail: str = ""

    @property
    def survived(self) -> bool:
        return self.status == SURVIVED


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def build_command(root: str, test_files: Sequence[str], extra: Optional[Sequence[str]] = None) -> list[str]:
    """Pick a test runner and target only the relevant files."""
    if extra:
        return [sys.executable, *extra]

    has_pytest = _module_available("pytest")
    if has_pytest and test_files:
        return [
            sys.executable, "-m", "pytest",
            "-x", "-q", "--no-header",
            "-p", "no:cacheprovider",
            *test_files,
        ]
    if has_pytest:
        return [sys.executable, "-m", "pytest", "-x", "-q", "--no-header", "-p", "no:cacheprovider"]

    # No pytest: fall back to unittest discovery, scoped to the directories the
    # matching tests live in so we do not pull in unrelated, slower suites.
    dirs = {os.path.dirname(os.path.abspath(f)) for f in test_files} if test_files else {root}
    if len(dirs) == 1:
        start = dirs.pop()
        return [sys.executable, "-m", "unittest", "discover", "-s", start, "-t", start]
    return [sys.executable, "-m", "unittest", "discover", "-s", root, "-t", root]


def _module_available(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def run_command(cmd: Sequence[str], cwd: str, timeout: float) -> tuple[str, str, float]:
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            list(cmd), cwd=cwd, capture_output=True, text=True,
            timeout=timeout, encoding="utf-8", errors="replace",
        )
        elapsed = time.perf_counter() - started
        output = (proc.stdout or "") + (proc.stderr or "")
        return ("pass" if proc.returncode == 0 else "fail"), output, elapsed
    except subprocess.TimeoutExpired:
        return TIMEOUT, "", time.perf_counter() - started


def check_baseline(root: str, cmd: Sequence[str], timeout: float) -> tuple[bool, str]:
    """The suite must be green before we mutate anything.

    Mutating a red suite produces a perfect score and a useless report: every
    mutant "gets caught" by a test that was already failing.
    """
    status, output, _ = run_command(cmd, root, timeout)
    if status == "pass":
        return True, ""
    if status == TIMEOUT:
        return False, f"the test suite timed out after {timeout:.0f}s before any mutation"
    detail = _tail(output) or "the test suite exited non-zero without printing anything"
    return False, detail


def _tail(text: str, limit: int = 2500) -> str:
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]


def _write(path: str, text: str) -> None:
    # newline="" keeps the file's original line endings on Windows.
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _read(path: str) -> str:
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def back_up(path: str) -> str:
    """Copy the original file aside so a hard crash cannot destroy it."""
    rel = os.path.basename(path)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dest = os.path.join(BACKUP_DIR, rel)
    shutil.copy2(path, dest)
    return dest


def restore_from_backup(target: str, backup_name: str) -> bool:
    dest = os.path.join(BACKUP_DIR, backup_name)
    if not os.path.exists(dest):
        return False
    shutil.copy2(dest, target)
    return True


def evaluate(
    root: str,
    path: str,
    mutants: Sequence[Mutant],
    cmd: Sequence[str],
    timeout: float = 120.0,
    on_progress: Optional[Callable[[Outcome, int, int], None]] = None,
) -> list[Outcome]:
    """Apply each mutant, run the suite, restore, repeat."""
    original = _read(path)
    original_digest = _digest(original)
    back_up(path)

    results: list[Outcome] = []
    try:
        for index, mutant in enumerate(mutants, start=1):
            try:
                mutated = mutant.apply(original)
            except ValueError as exc:
                outcome = Outcome(mutant, ERROR, 0.0, str(exc))
                results.append(outcome)
                if on_progress:
                    on_progress(outcome, index, len(mutants))
                continue

            _write(path, mutated)
            status, output, elapsed = run_command(cmd, root, timeout)

            if status == "pass":
                outcome = Outcome(mutant, SURVIVED, elapsed, "")
            elif status == TIMEOUT:
                outcome = Outcome(mutant, TIMEOUT, elapsed, f"suite exceeded {timeout:.0f}s")
            elif status == "fail":
                outcome = Outcome(mutant, KILLED, elapsed, _first_failure(output))
            else:
                outcome = Outcome(mutant, ERROR, elapsed, _tail(output, 400))

            results.append(outcome)
            if on_progress:
                on_progress(outcome, index, len(mutants))
    finally:
        _write(path, original)
        if _digest(_read(path)) != original_digest:
            # Last line of defence. Should be unreachable; if it fires, we say so loudly.
            raise RuntimeError(
                f"failed to restore {path}. A backup is at {os.path.join(BACKUP_DIR, os.path.basename(path))}"
            )
    return results


def _first_failure(output: str) -> str:
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith(("FAILED", "ERROR")) or "AssertionError" in stripped:
            return stripped[:200]
    for line in reversed(output.splitlines()):
        if line.strip():
            return line.strip()[:200]
    return ""
