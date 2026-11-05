"""Command line entry point."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional, Sequence

from . import __version__
from .diff import changed_functions, git_available
from .discover import find_tests, guess_module_name
from .mutate import find_functions, is_equivalent, mutants_for_source, renumber
from .report import Report, merge, render_badge, render_json, render_markdown, render_terminal
from .runner import BACKUP_DIR, build_command, check_baseline, evaluate, restore_from_backup
from . import witness as witness_mod


OPERATOR_HELP = {
    "compare": (
        "比较运算符互换（>= → >、== → != …）",
        "条件判断的语义完全变了，但代码依然能跑、类型依然对。"
        "AI 写的测试通常只覆盖 happy path，这类改动在测试里完全隐形。",
    ),
    "boundary": (
        "边界值差一（>= → >）",
        "编程史上最贵的 bug 类型之一。它只在输入正好等于阈值时改变行为，"
        "而手写测试和 AI 生成的测试都极少主动去试那个精确的边界值。",
    ),
    "binop": (
        "算术运算符替换（+ → -、* → / …）",
        "金额、分数、计数在这些地方出错时，结果往往看起来仍然『合理』，"
        "所以靠肉眼 review 很难发现，只能靠测试。",
    ),
    "boolop": (
        "布尔连接词替换（and → or）",
        "一个 and 变 or，权限判断和准入校验就会从『全部满足』变成『满足其一』。"
        "这类改动最典型的后果是越权。",
    ),
    "not": (
        "移除取反（not x → x）",
        "条件极性反转。测试若只断言『正常情况能通过』，反转后依然通过。",
    ),
    "return_none": (
        "丢弃返回值（return x → return None）",
        "调用方拿到 None 而不是数据。只有当测试真的断言了返回内容时才会暴露。",
    ),
    "constant": (
        "常量篡改（0 → 1、True → False）",
        "默认值、开关、阈值被改。测试如果没有断言具体数值，就抓不到。",
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
        description="变异测试，只测你这次改的代码。告诉你测试哪里在假装。",
    )
    p.add_argument("--version", action="version", version=f"falsify {__version__}")
    sub = p.add_subparsers(dest="command")

    run = sub.add_parser("run", help="对改动的函数做变异测试")
    run.add_argument("--base", default="main", help="对比的基线分支（默认 main）")
    run.add_argument("--staged", action="store_true", help="只看已暂存的改动")
    run.add_argument("--file", action="append", metavar="PATH", help="指定文件（可重复）")
    run.add_argument("--function", action="append", metavar="NAME",
                     help="只测指定函数，如 Order.total（可重复）")
    run.add_argument("--operators", metavar="LIST",
                     help="逗号分隔的算子：" + ",".join(OPERATOR_HELP))
    run.add_argument("--max-mutants", type=int, default=40, help="每个文件的变异体上限（默认 40）")
    run.add_argument("--timeout", type=float, default=120.0, help="单个变异体的测试超时秒数（默认 120）")
    run.add_argument("--test-cmd", help="自定义测试命令，如 'pytest tests/test_x.py'")
    run.add_argument("--json", action="store_true", help="输出 JSON")
    run.add_argument("--md", action="store_true", help="输出 Markdown（可直接贴 PR 评论）")
    run.add_argument("--badge", metavar="PATH", help="写一个 SVG 徽章到指定路径")
    run.add_argument("--ci", action="store_true", help="有存活变异体时以非零码退出")
    run.add_argument("--fail-under", type=float, metavar="N", help="变异分数低于 N 时失败")
    run.add_argument("--no-witness", action="store_true", help="跳过反例搜索（更快，但只说问题不给答案）")
    run.add_argument("--witness-limit", type=int, default=8, help="最多为几个存活变异体找反例（默认 8）")
    run.add_argument("--force", action="store_true", help="允许在工作区有未提交改动时运行")
    run.add_argument("--detail-limit", type=int, default=8, help="终端里详细展开几个存活变异体（默认 8）")
    run.add_argument("--color", choices=["auto", "always", "never"], default="auto")
    run.add_argument("--no-color", action="store_true", help="关闭颜色，等价于 --color never")

    exp = sub.add_parser("explain", help="解释某类变异为什么危险")
    exp.add_argument("operator", nargs="?", choices=sorted(OPERATOR_HELP))

    sub.add_parser("restore", help="从 .falsify/backup 恢复被中断的运行")

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
        raise SystemExit("falsify: 不是 git 仓库，请用 --file 指定要测的文件")

    found = changed_functions(root, args.base, staged=args.staged)
    if not found:
        raise SystemExit(
            f"falsify: 与 {args.base} 相比没有 Python 函数改动。"
            "用 --file 指定文件，或用 --base 换一个基线。"
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
            "falsify: 这些文件有未提交的改动：\n  " + listing + "\n"
            "变异测试会临时改写源文件，在有未提交改动时跑可能丢东西。\n"
            "先 git stash / commit，或者确认风险后加 --force。"
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
                print(f"falsify: {rel} 里没有找到指定函数，跳过", file=sys.stderr)
                continue

        candidates = []
        for func in functions:
            try:
                candidates.extend(mutants_for_source(source, func, operators))
            except (KeyError, SyntaxError) as exc:
                print(f"falsify: 跳过 {rel}:{func} — {exc}", file=sys.stderr)
        candidates = [m for m in candidates if not is_equivalent(source, m)]
        candidates = renumber(candidates)[: args.max_mutants]

        if not candidates:
            print(f"falsify: {rel} 没有可用的变异体（函数体太简单或算子不适用）", file=sys.stderr)
            continue

        module = guess_module_name(root, path)
        test_target = find_tests(root, module)
        if test_target.is_empty:
            print(f"falsify: {rel} 找不到任何测试文件，跳过", file=sys.stderr)
            continue

        if args.test_cmd:
            cmd = args.test_cmd.split()
        else:
            cmd = build_command(root, test_target.files)

        ok, why = check_baseline(root, cmd, args.timeout)
        if not ok:
            print(f"falsify: {rel} 的测试在未变异时就没通过，先修测试：\n{why}", file=sys.stderr)
            continue

        try:
            outcomes = evaluate(root, path, candidates, cmd, timeout=args.timeout)
        except RuntimeError as exc:
            print(f"falsify: {exc}", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            print("\nfalsify: 已中断，源文件已恢复。若仍怀疑受损，运行 falsify restore", file=sys.stderr)
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
        print("falsify: 没有可评估的目标。", file=sys.stderr)
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
            print("falsify: 没有分数，跳过徽章", file=sys.stderr)
        else:
            written = render_badge(total.score, args.badge)
            if not args.json and not args.md:
                print(f"  徽章已写入 {written}")

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
        for line in why.split("。"):
            if line.strip():
                print(f"    {line.strip()}。")
    print()
    return 0


def _cmd_restore(args) -> int:
    if not os.path.isdir(BACKUP_DIR):
        print("falsify: 没有找到备份目录，无需恢复。")
        return 0
    restored = []
    for name in sorted(os.listdir(BACKUP_DIR)):
        if restore_from_backup(name, name):
            restored.append(name)
    if restored:
        print("falsify: 已从备份恢复：" + ", ".join(restored))
    else:
        print("falsify: 备份目录是空的。")
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
