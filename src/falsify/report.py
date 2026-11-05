"""Present results so they are impossible to misread.

The report has one job: make a surviving mutant feel like a bug you can fix in
thirty seconds, not a number you scroll past. So every survivor gets its exact
location, what the break did, the input your suite never tried, and the assertion
that closes the hole.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional

from .runner import ERROR, KILLED, SURVIVED, TIMEOUT, Outcome
from .witness import Witness

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"

_SEVERITY_ORDER = {"critical": 0, "normal": 1, "low": 2}


@dataclass
class Report:
    file: str
    outcomes: list[Outcome] = field(default_factory=list)
    witnesses: dict[str, Witness] = field(default_factory=dict)
    suggestions: dict[str, str] = field(default_factory=dict)

    @property
    def killed(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.status == KILLED]

    @property
    def survived(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.status == SURVIVED]

    @property
    def scored(self) -> int:
        return len([o for o in self.outcomes if o.status in (KILLED, SURVIVED)])

    @property
    def score(self) -> Optional[float]:
        if not self.scored:
            return None
        return 100.0 * len(self.killed) / self.scored

    @property
    def blind_spot(self) -> float:
        """Share of deliberate defects no test noticed."""
        if not self.scored:
            return 0.0
        return 100.0 * len(self.survived) / self.scored


def merge(reports: list[Report]) -> Report:
    merged = Report(file="*")
    for r in reports:
        merged.outcomes.extend(r.outcomes)
        merged.witnesses.update(r.witnesses)
        merged.suggestions.update(r.suggestions)
    return merged


class _Paint:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def __call__(self, text: str, *codes: str) -> str:
        if not self.enabled or not codes:
            return text
        return "".join(codes) + text + RESET


def _assert_key(suggestion: Optional[str]) -> str:
    """The part of a suggestion that actually asserts something."""
    if not suggestion:
        return ""
    keep = [
        line.strip()
        for line in suggestion.splitlines()
        if line.strip().startswith(("assert", "with "))
    ]
    return "\n".join(keep)


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FALSIFY_FORCE_COLOR"):
        return True
    return bool(getattr(os.sys.stdout, "isatty", lambda: False)())


def render_terminal(reports: list[Report], color: Optional[bool] = None, width: int = 78,
                    detail_limit: int = 8) -> str:
    paint = _Paint(_supports_color() if color is None else color)
    lines: list[str] = []
    # One assertion often kills several mutants. Repeating the same snippet three
    # times makes the report feel padded, so the second mention points back to
    # the first instead of printing it again.
    seen_suggestions: dict[str, str] = {}
    detailed = 0

    for report in reports:
        if not report.outcomes:
            continue
        lines.append("")
        lines.append(paint(f"  {report.file}", BOLD))

        survivors = sorted(
            report.survived,
            key=lambda o: (_SEVERITY_ORDER.get(o.mutant.severity, 1), o.mutant.lineno),
        )
        for outcome in survivors:
            m = outcome.mutant
            if detailed >= detail_limit:
                lines.append(
                    f"  {paint('SURVIVED', RED)}  {paint(m.id, BOLD)}  line {m.lineno}  "
                    f"{paint(m.description.split(' (')[0], DIM)}"
                )
                continue
            detailed += 1
            tag = paint("SURVIVED", RED, BOLD)
            sev = "" if m.severity == "normal" else paint(f" [{m.severity}]", DIM)
            lines.append("")
            lines.append(f"  {tag}{sev}  {paint(m.id, BOLD)}  {m.operator}  line {m.lineno}")
            lines.append(f"    {paint(m.function, CYAN)}  {m.description}")
            before = m.original.strip().replace("\n", " ")
            after = m.replacement.strip().replace("\n", " ")
            lines.append(f"    {paint(before, DIM)}  →  {paint(after, DIM)}")

            witness = report.witnesses.get(m.id)
            if witness and witness.kind in ("value", "exception"):
                suggestion = report.suggestions.get(m.id)
                call = witness.call_source(m.function)
                # Key on the assertion itself, not the whole snippet: the trailing
                # "mutant would have returned X" comment differs per mutant, so
                # keying on the full text would never dedupe anything.
                key = _assert_key(suggestion)
                lines.append("")
                if key and key in seen_suggestions:
                    lines.append(
                        paint(f"    the same input ({call}) kills this one too.", DIM)
                    )
                else:
                    lines.append(paint("    your tests never tried this input:", YELLOW))
                    lines.append(f"      {call}")
                    lines.append(f"      original  →  {witness.orig_result}")
                    lines.append(f"      mutant    →  {witness.mutant_result}")
                    if suggestion:
                        if key:
                            seen_suggestions[key] = m.id
                        lines.append("")
                        lines.append(paint("    one line closes it:", GREEN))
                        for sline in suggestion.splitlines():
                            if sline.strip().startswith(("assert", "with ")):
                                lines.append(f"      {paint(sline.strip(), GREEN)}")
            elif witness and witness.note:
                lines.append(f"    {paint('counterexample search: ' + witness.note, DIM)}")

        if report.killed:
            ids = ", ".join(o.mutant.id for o in sorted(report.killed, key=lambda o: o.mutant.lineno))
            lines.append("")
            lines.append(
                f"  {paint('KILLED', GREEN)}    {len(report.killed)} caught by your tests  {paint(ids, DIM)}"
            )

        others = [o for o in report.outcomes if o.status in (TIMEOUT, ERROR)]
        for o in others:
            lines.append(f"  {paint('SKIPPED', YELLOW)}   {o.mutant.id}  {o.status}: {o.detail[:80]}")

    total = merge(reports)
    lines.append("")
    lines.append(paint("  " + "─" * (width - 2), DIM))
    if total.score is None:
        lines.append(paint("  no mutants were evaluated.", YELLOW))
    else:
        score = total.score
        if score >= 90:
            tone, word = GREEN, "solid"
        elif score >= 70:
            tone, word = YELLOW, "thin"
        else:
            tone, word = RED, "blind"
        lines.append(
            f"  {paint(f'{score:.0f}%', tone, BOLD)} mutation score  "
            f"{paint(f'({len(total.killed)}/{total.scored} killed)', DIM)}  ·  {word}"
        )
        if total.survived:
            lines.append(
                paint(
                    f"  {total.blind_spot:.0f}% of behaviour changes would pass your tests unnoticed.",
                    DIM,
                )
            )
            keys = {_assert_key(s) for s in total.suggestions.values() if s}
            keys.discard("")
            if keys:
                lines.append("")
                lines.append(
                    paint(
                        f"  {len(total.survived)} survivors, "
                        f"{len(keys)} assertions close every one of them.",
                        BOLD,
                    )
                )
    lines.append("")
    return "\n".join(lines)


def render_json(reports: list[Report]) -> str:
    total = merge(reports)
    payload = {
        "schema": "falsify/1",
        "score": None if total.score is None else round(total.score, 1),
        "killed": len(total.killed),
        "survived": len(total.survived),
        "scored": total.scored,
        "files": [
            {
                "file": r.file,
                "score": None if r.score is None else round(r.score, 1),
                "mutants": [
                    {
                        "id": o.mutant.id,
                        "status": o.status,
                        "operator": o.mutant.operator,
                        "function": o.mutant.function,
                        "line": o.mutant.lineno,
                        "original": o.mutant.original.strip(),
                        "replacement": o.mutant.replacement.strip(),
                        "severity": o.mutant.severity,
                        "witness": None if o.mutant.id not in r.witnesses else {
                            "kind": r.witnesses[o.mutant.id].kind,
                            "call": r.witnesses[o.mutant.id].call_source(o.mutant.function),
                            "original_result": r.witnesses[o.mutant.id].orig_result,
                            "mutant_result": r.witnesses[o.mutant.id].mutant_result,
                        },
                        "suggestion": r.suggestions.get(o.mutant.id),
                    }
                    for o in r.outcomes
                ],
            }
            for r in reports
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def render_markdown(reports: list[Report]) -> str:
    total = merge(reports)
    out: list[str] = []
    if total.score is None:
        return "falsify: no mutants were evaluated.\n"

    verdict = "clean" if not total.survived else f"{len(total.survived)} survived"
    out.append(f"### falsify — mutation score {total.score:.0f}% · {verdict}")
    out.append("")
    out.append(
        f"{len(total.killed)}/{total.scored} injected defects were caught by the suite. "
        f"The rest is logic that can be broken while CI stays green."
    )

    if total.survived:
        out.append("")
        out.append("| mutant | location | what changed | input your suite missed |")
        out.append("| --- | --- | --- | --- |")
        for r in reports:
            for o in sorted(r.survived, key=lambda x: (x.mutant.function, x.mutant.lineno)):
                m = o.mutant
                w = r.witnesses.get(m.id)
                if w is None:
                    call = "_not searched_"
                elif w.kind in ("value", "exception"):
                    call = f"`{w.call_source(m.function)}`"
                elif w.kind == "no_witness":
                    call = "_searched, none found_"
                else:
                    call = "_signature not searchable_"
                before = m.original.strip().replace("|", "\\|")
                after = m.replacement.strip().replace("|", "\\|")
                out.append(
                    f"| `{m.id}` | `{r.file}:{m.lineno}` | `{before}` → `{after}` | {call} |"
                )

    suggestions: list[str] = []
    seen_keys: set[str] = set()
    for suggestion in total.suggestions.values():
        key = _assert_key(suggestion)
        if not suggestion or (key and key in seen_keys):
            continue
        if key:
            seen_keys.add(key)
        suggestions.append(suggestion)
    if suggestions:
        out.append("")
        out.append("<details><summary>Tests that would close these gaps</summary>")
        out.append("")
        out.append("```python")
        out.append("\n\n".join(suggestions))
        out.append("```")
        out.append("")
        out.append("</details>")
    out.append("")
    return "\n".join(out)


_BADGE_KEY = None


def render_badge(score: float, path: str) -> str:
    """Write a self-contained SVG badge you can commit to your README."""
    color = "#2ea043" if score >= 90 else "#d29922" if score >= 70 else "#cf222e"
    label = "falsify"
    value = f"{score:.0f}%"
    label_w, value_w = 62, 46
    total_w = label_w + value_w
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{total_w}" height="20" role="img" aria-label="{label}: {value}">
  <title>{label}: {value}</title>
  <linearGradient id="s" x2="0" y2="100%">
    <stop offset="0" stop-color="#bbb" stop-opacity=".1"/>
    <stop offset="1" stop-opacity=".1"/>
  </linearGradient>
  <clipPath id="r"><rect width="{total_w}" height="20" rx="3" fill="#fff"/></clipPath>
  <g clip-path="url(#r)">
    <rect width="{label_w}" height="20" fill="#555"/>
    <rect x="{label_w}" width="{value_w}" height="20" fill="{color}"/>
    <rect width="{total_w}" height="20" fill="url(#s)"/>
  </g>
  <g fill="#fff" text-anchor="middle" font-family="Verdana,Geneva,DejaVu Sans,sans-serif" font-size="11">
    <text x="{label_w / 2:.0f}" y="14">{label}</text>
    <text x="{label_w + value_w / 2:.0f}" y="14">{value}</text>
  </g>
</svg>
"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(svg)
    return path
