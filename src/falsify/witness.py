"""Counterexample search — the part every mutation tool leaves out.

Everyone tells you "mutant survived". Nobody tells you *which input* your test
suite failed to try. That input is the whole point: it is the test you forgot to
write, and once you have it, the fix is a two-line assertion.

We find it by running the original function and the mutated function side by
side over a generated input space, and returning the first input where their
behaviour diverges.

Safety note: this executes the target module. That is unavoidable — the module
has to run for us to observe behaviour — and it is also why the search always
happens in a *separate process* with a timeout, never in the caller's process.
Modules with import-time side effects (network, database) should be excluded
with ``--no-witness``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Any, Optional

_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_witness_worker.py")


@dataclass
class Witness:
    """An input that separates the original function from the mutant."""

    args: list[Any] = field(default_factory=list)
    kwargs: dict[str, Any] = field(default_factory=dict)
    orig_result: str = ""
    mutant_result: str = ""
    kind: str = "value"  # value | exception | no_witness | unsupported
    note: str = ""

    def call_source(self, func_name: str) -> str:
        """Render the call as Python source, e.g. ``apply_discount(200.0, 0.0, True)``."""
        parts = [repr(a) for a in self.args]
        parts.extend(f"{k}={v!r}" for k, v in self.kwargs.items())
        short = func_name.rsplit(".", 1)[-1]
        return f"{short}({', '.join(parts)})"


def search(
    orig_source: str,
    mutant_source: str,
    func_name: str,
    module_path: str,
    timeout: float = 20.0,
    max_calls: int = 400,
) -> Witness:
    """Search for a distinguishing input in a sandboxed subprocess."""
    payload = {
        "orig": orig_source,
        "mut": mutant_source,
        "func": func_name,
        "path": module_path,
        "max_calls": max_calls,
    }
    fd, path = tempfile.mkstemp(suffix=".json", prefix="falsify-witness-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        out_path = path + ".out"
        proc = subprocess.run(
            [sys.executable, _WORKER, path],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        # The result is read from a side file, not stdout: the target module may
        # print on import and we refuse to be confused by someone else's output.
        if not os.path.exists(out_path):
            err = (proc.stderr or "").strip().splitlines()
            return Witness(kind="unsupported", note=err[-1] if err else "worker produced no result")
        with open(out_path, encoding="utf-8") as fh:
            data = json.load(fh)
        return Witness(**data)
    except subprocess.TimeoutExpired:
        return Witness(kind="unsupported", note=f"counterexample search timed out after {timeout:.0f}s")
    except (json.JSONDecodeError, TypeError):
        return Witness(kind="unsupported", note="could not parse counterexample result")
    finally:
        for p in (path, path + ".out"):
            try:
                os.unlink(p)
            except OSError:
                pass


def suggest_assertion(witness: Witness, func_name: str, tag: str = "") -> Optional[str]:
    """Turn a witness into a test body someone can paste into their suite.

    Deliberately boring output: given-when-then, a literal expected value, no
    cleverness. A test you cannot read is a test you will delete.
    """
    if witness.kind == "no_witness" or not witness.orig_result:
        return None
    call = witness.call_source(func_name)
    short = func_name.rsplit(".", 1)[-1]
    suffix = f"_{tag.lower()}" if tag else ""
    lines = [
        f"def test_{short}{suffix}_boundary_case():",
        '    """Kills a mutant that your current suite lets through."""',
    ]
    if witness.kind == "exception":
        lines.append(f"    with pytest.raises(...):  # original raised {witness.orig_result}")
        lines.append(f"        {call}")
    else:
        lines.append(f"    assert {call} == {witness.orig_result}")
        lines.append(f"    # the mutated version would have returned {witness.mutant_result}")
    return "\n".join(lines)
