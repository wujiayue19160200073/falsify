"""Command line entry point."""

from __future__ import annotations

import argparse
import os
import sys
import textwrap
from typing import Optional, Sequence

from . import __version__
from . import runner as runner_mod
from . import witness as witness_mod
from .diff import changed_functions, git_available
from .discover import find_tests, guess_module_name
from .mutate import find_functions, is_equivalent, mutants_for_source, renumber
from .report import Report, merge, render_badge, render_json, render_markdown, render_terminal
from .runner import BACKUP_DIR, build_command, check_baseline, evaluate, restore_from_backup


OPERATOR_HELP = {
    "compare": (
        "flip a comparison (>= → <, == → != …)",
        "The condition means something else now, but the code still runs and the "
        "types still check. Tests written against the happy path never look at the "
        "difference.",
    ),
    "boundary": (
        "off-by-one at a threshold (>= → >)",
        "It only changes behaviour when the input sits exactly on the boundary. "
        "Hand-written tests rarely try that value, and generated ones almost never do.",
    ),
    "binop": (
        "swap arithmetic operators (+ → -, * → / …)",
        "Money, rates and counters go wrong in ways that still look plausible, so "
        "review cannot catch it. Only an assertion on the number can.",
    ),
    "boolop": (
        "swap boolean connectives (and → or)",
        "One `and` becoming `or` turns 'all conditions' into 'any condition'. In an "
        "authorisation check that is a privilege escalation, not a bug.",
    ),
    "not": (
        "drop a negation (not x → x)",
        "The polarity of the condition inverts. A test that only asserts the happy "
        "path still passes.",
    ),
    "return_none": (
        "discard the return value (return x → return None)",
        "Callers get None instead of data. Only a test that asserts on the value "
        "will notice.",
    ),
    "constant": (
        "tamper with constants (0 → 1, True → False)",
        "Defaults, flags and thresholds change. A test that never asserts the "
        "specific value cannot see it.",
    ),
}


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        enc = (getattr(stream, "encoding", "") or "").lower()
        if enc not in ("utf-8", "utf8") and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="falsify",
        description="Mutation testing for the code you just changed. Finds the tests that only pretend.",
    )
    p.add_argument("--version", action="version", version=f"falsify {__version__}")
    sub = p.add_subparsers(dest="command")

    run = sub.add_parser("run", help="mutate the functions a change touched")
    run.add_argument("--base", default="main", help="baseline branch to diff against (default: main)")
    run.add_argument("--staged", action="store_true", help="only look at staged changes")
    run.add_argument("--file", action="append", metavar="PATH", help="target a file (repeatable)")
    run.add_argument("--function", action="append", metavar="NAME",
                     help="only mutate this function, e.g. Order.total (repeatable)")
    run.add_argument("--operators", metavar="LIST",
                     help="comma-separated operators: " + ",".join(OPERATOR_HELP))
    run.add_argument("--max-mutants", type=int, default=40, help="mutants per file (default: 40)")
    run.add_argument("--timeout", type=float, default=120.0, help="per-mutant test timeout in seconds (default: 120)")
    run.add_argument("--test-cmd", help="your own test command, e.g. 'pytest tests/test_x.py'")
    run.add_argument("--json", action="store_true", help="emit JSON")
    run.add_argument("--md", action="store_true", help="emit Markdown for a PR comment")
    run.add_argument("--badge", metavar="PATH", help="write an SVG badge to PATH")
    run.add_argument("--ci", action="store_true", help="exit non-zero when any mutant survives")
    run.add_argument("--fail-under", type=float, metavar="N", help="fail when the mutation score is below N")
    run.add_argument("--no-witness", action="store_true",
                     help="skip the counterexample search (faster, but only reports the problem)")
    run.add_argument("--witness-limit", type=int, default=8,
                     help="search counterexamples for at most N survivors (default: 8)")
    run.add_argument("--force", action="store_true", help="run even with uncommitted changes in target files")
    run.add_argument("--detail-limit", type=int, default=8, help="expand N survivors in the terminal (default: 8)")
    run.add_argument("--color", choices=["auto", "always", "never"], default="auto")
    run.add_argument("--no-color", action="store_true", help="same as --color never")

    exp = sub.add_parser("explain", help="explain why an operator matters")
    exp.add_argument("operator", nargs="?", choices=sorted(OPERATOR_HELP))

    sub.add_parser("restore", help="restore files from .falsify/backup after an interrupted run")

    return p


def _resolve_targets(args, root: str) -> dict[str, list[str]]:
    """Work out which (file, functions) pairs to mutate."""
    if args.file:
        targets: dict[str, list[str]] = {}
        for path in args.file:
            full = path if os.path.isabs(path) else os.path.join(root, path)
            if not os.path.exists(full):
                raise SystemExit(f"falsify: file not found: {path}")
            with open(full, encoding="utf-8", errors="replace") as fh:
                source = fh.read()
            names = sorted(find_functions(_parse(source)).keys())
            targets[os.path.relpath(full, root).replace(os.sep, "/")] = names
        return targets

    if not git_available(root):
        raise SystemExit("falsify: not a git repository. Use --file to name the files to test.")

    found = changed_functions(root, args.base, staged=args.staged)
    if not found:
        raise SystemExit(
            f"falsify: no Python functions changed against {args.base}. "
            "Use --file to name a file, or --base to diff against a different branch."
        )
    return found


def _parse(source: str):
    import ast

    return ast.parse(source)


def _check_clean(root: str, files: Sequence[str], force: bool) -> None:
    if force:
        return
    try:
        import subprocess

        proc = subprocess.run(["git", "status", "--porcelain", "--", *files], cwd=root,
                              capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return
    dirty = [ln[3:].strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    if dirty:
        listing = "\n  ".join(dirty)
        raise SystemExit(
            "falsify: these files have uncommitted changes:\n  " + listing + "\n"
            "Mutation testing rewrites source files on purpose, so an uncommitted "
            "file can lose work.\n"
            "Commit or stash first, or pass --force if you accept that."
        )


def _cmd_run(args) -> int:
    root = os.getcwd()
    targets = _resolve_targets(args, root)
    files = sorted(targets)
    _check_clean(root, files, args.force)

    operators = [o.strip() for o in args.operators.split(",")] if args.operators else None
    reports: list[Report] = []
    had_survivors = False

    for rel in files:
        path = os.path.join(root, rel)
        with open(path, encoding="utf-8", errors="replace") as fh:
            source = fh.read()

        functions = targets[rel]
        if args.function:
            wanted = set(args.function)
            functions = [f for f in functions if f in wanted or f.split(".")[-1] in wanted]
            if not functions:
                print(f"falsify: {rel}: no function matched --function, skipped", file=sys.stderr)
                continue

        candidates = []
        for func in functions:
            try:
                candidates.extend(mutants_for_source(source, func, operators))
            except (KeyError, SyntaxError) as exc:
                print(f"falsify: skipping {rel}:{func} — {exc}", file=sys.stderr)
        candidates = [m for m in candidates if not is_equivalent(source, m)]
        candidates = renumber(candidates)[: args.max_mutants]

        if not candidates:
            print(f"falsify: {rel}: no usable mutants (bodies too simple, or operators do not apply)",
                  file=sys.stderr)
            continue

        module = guess_module_name(root, path)
        test_target = find_tests(root, module)
        if test_target.is_empty:
            print(f"falsify: {rel}: no test file found, skipped", file=sys.stderr)
            continue

        if args.test_cmd:
            cmd = args.test_cmd.split()
        else:
            cmd = build_command(root, test_target.files)

        baseline = check_baseline(root, cmd, args.timeout)
        if not baseline.ok:
            if baseline.no_tests:
                print(
                    f"falsify: {rel}: no tests were collected, so there is nothing to judge.\n"
                    f"{runner_mod.explain_no_tests(test_target.files)}",
                    file=sys.stderr,
                )
            else:
                print(
                    f"falsify: {rel}: the suite fails before any mutation, so the score would "
                    f"be meaningless. Fix the suite first:\n{baseline.detail}",
                    file=sys.stderr,
                )
            continue

        try:
            outcomes = evaluate(root, path, candidates, cmd, timeout=args.timeout)
        except RuntimeError as exc:
            print(f"falsify: {exc}", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            print("\nfalsify: interrupted, source files restored. If you suspect damage, "
                  "run `falsify restore`.", file=sys.stderr)
            return 130

        report = Report(file=rel, outcomes=outcomes)

        survivors = [o for o in outcomes if o.survived]
        if survivors:
            had_survivors = True
        if not args.no_witness and survivors:
            for outcome in survivors[: args.witness_limit]:
                mutated = outcome.mutant.apply(source)
                w = witness_mod.search(source, mutated, outcome.mutant.function, path)
                report.witnesses[outcome.mutant.id] = w
                if w.kind in ("value", "exception"):
                    suggestion = witness_mod.suggest_assertion(w, outcome.mutant.function, outcome.mutant.id)
                    if suggestion:
                        report.suggestions[outcome.mutant.id] = suggestion

        reports.append(report)

    if not reports:
        print("falsify: nothing to evaluate.", file=sys.stderr)
        return 2

    total = merge(reports)

    if args.json:
        print(render_json(reports))
    elif args.md:
        print(render_markdown(reports))
    else:
        color = {"auto": None, "always": True, "never": False}["never" if args.no_color else args.color]
        print(render_terminal(reports, color=color, detail_limit=args.detail_limit))

    if args.badge:
        if total.score is None:
            print("falsify: no score to put on a badge, skipped", file=sys.stderr)
        else:
            written = render_badge(total.score, args.badge)
            if not args.json and not args.md:
                print(f"  badge written to {written}")

    if args.ci or args.fail_under is not None:
        if args.fail_under is not None:
            if total.score is None or total.score < args.fail_under:
                return 1
        elif had_survivors:
            return 1
    return 0


def _cmd_explain(args) -> int:
    keys = [args.operator] if args.operator else list(OPERATOR_HELP)
    for key in keys:
        what, why = OPERATOR_HELP[key]
        print(f"\n  {key}  —  {what}\n")
        for line in textwrap.wrap(why, width=74):
            print(f"    {line}")
    print()
    return 0


def _cmd_restore(args) -> int:
    if not os.path.isdir(BACKUP_DIR):
        print("falsify: no backup directory, nothing to restore.")
        return 0
    restored = []
    for name in sorted(os.listdir(BACKUP_DIR)):
        if restore_from_backup(name, name):
            restored.append(name)
    if restored:
        print("falsify: restored from backup: " + ", ".join(restored))
    else:
        print("falsify: the backup directory is empty.")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    _force_utf8()
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "explain":
        return _cmd_explain(args)
    if args.command == "restore":
        return _cmd_restore(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
