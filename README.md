# falsify

[![ci](https://github.com/wujiayue19160200073/falsify/actions/workflows/ci.yml/badge.svg)](https://github.com/wujiayue19160200073/falsify/actions/workflows/ci.yml)

**Your tests are green. Are they real?**

`falsify` breaks your code on purpose — then checks whether anything notices.
When nothing does, it hands you the exact input your suite never tried, and the
one-line assertion that closes the hole.

Zero dependencies. Python 3.9+. Runs on the diff, not the repo.

```
  SURVIVED [critical]  M001  boundary  line 14
    apply_discount  off-by-one: >= → > (behaviour changes exactly at the threshold)
    >=  →  >

    your tests never tried this input:
      apply_discount(200.0, 0.0, True)
      original  →  180.0
      mutant    →  200.0

    one line closes it:
      assert apply_discount(200.0, 0.0, True) == 180.0
```

That output is from [`demo/`](demo/), a module with ~100% line coverage. Its test
suite passes. It would not notice if you inverted half the pricing logic.

---

## The problem nobody's dashboard shows

Line coverage counts the lines that **ran**. It says nothing about the lines
that would **fail if they were wrong**. As coding agents started writing both the
code and its tests, the two numbers came apart:

| | Human-written tests | Agent-written tests |
| --- | --- | --- |
| Line coverage | ~75% | **100%** |
| Mutation score (defects actually caught) | ~70% | **4%** *(Wang et al., 2025)* |

A test written from the implementation asserts what the implementation does. It
is a tautology with a green checkmark. Coverage goes up, the safety net gets
thinner, and nobody notices until production does.

Thoughtworks put mutation testing in their **Trial** ring in April 2026 for
exactly this reason: it is the only measurement that tells you whether a test
asserts anything.

So why does almost nobody run it?

**Because the tools are too slow to fit anywhere.** Mutating a real repository
means re-running the whole suite hundreds of times. It is a 40-minute nightly job
that gets deleted in the second sprint.

`falsify` fixes the only part that matters: it mutates **only the functions this
change touched**. One PR usually touches one to five functions. That is a
two-minute job that fits in CI, on every pull request, for everyone.

---

## What makes it different

Every mutation tool tells you a mutant survived. That is a number, and numbers
get scrolled past.

`falsify` tells you **which input** your suite failed to try, by running the
original function and the mutated function side by side over a generated input
space until their behaviour diverges. That input *is* the missing test.

It also means one assertion often closes several holes at once:

```
  6 survivors, 3 assertions close every one of them.
```

---

## Install

```bash
pip install git+https://github.com/wujiayue19160200073/falsify
```

Or with no install at all:

```bash
git clone https://github.com/wujiayue19160200073/falsify && cd falsify
PYTHONPATH=src python -m falsify.cli run --file demo/shop.py
```

## Use

```bash
# mutate only what this branch changed, against main
falsify run --base main

# mutate one file, all of it
falsify run --file src/shop/pricing.py

# one function only, faster
falsify run --file src/shop/pricing.py --function apply_discount

# CI: fail the build if fewer than 70% of mutants are caught
falsify run --base origin/main --ci --fail-under 70
```

### Output formats

```bash
falsify run --base main --md      # Markdown, paste straight into a PR comment
falsify run --base main --json    # machine-readable, schema "falsify/1"
falsify run --base main --badge docs/falsify.svg   # commit it to your README
```

### Understand the operators

```bash
falsify explain boundary
falsify explain compare
```

---

## CI

```yaml
name: falsify
on: pull_request

jobs:
  mutants:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0          # falsify needs history to diff against the base
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -e . pytest
      - name: Mutation testing
        run: falsify run --base "origin/${{ github.base_ref }}" --md --json > /tmp/falsify.json
      - name: Publish report
        if: always()
        run: |
          python -c "import json;d=json.load(open('/tmp/falsify.json'));print(f\"## falsify: {d['score']}% mutation score\")" >> $GITHUB_STEP_SUMMARY
```

Drop `--ci` and the job always passes while still publishing the report — a good
way to look at the numbers for a week before you gate on them.

---

## What it does not do

Being clear about this is more useful than a feature list.

- **It does not detect AI-written code.** There is no authorship classifier here,
  no "AI slop score". Text-based detectors are unreliable and produce confident
  nonsense; `falsify` measures behaviour instead, which is objectively checkable.
- **It does not replace your linter or your reviewer.** A surviving mutant means
  *your tests didn't catch this*, not *this code is wrong*.
- **It does not mutate Python you didn't write in this change.** That is the
  design, not a limitation.
- **It is not a sandbox.** It rewrites your source files, runs your test suite,
  and — during counterexample search — imports your module in a subprocess. It
  restores everything in a `finally` block and keeps an on-disk backup in
  `.falsify/backup`, but it expects a working tree you don't mind it touching.

## Safety

Mutating code means writing broken code into your source files. That is the one
genuinely dangerous thing this tool does, so:

1. It refuses to run when the target files have uncommitted changes (override
   with `--force`).
2. It copies each file to `.falsify/backup/` before touching it.
3. It restores the original in a `finally` block — including on `Ctrl-C` — and
   verifies the restore by hash before returning.
4. If a run is killed hard enough to skip `finally`, `falsify restore` puts the
   files back from the backup.

It will still execute your test suite and import your module. If your module has
import-time side effects (network calls, database connections, sending email),
run with `--no-witness` — the mutation testing itself only executes *your tests*,
but the counterexample search needs to import the module twice.

---

## How it works

```
git diff  ──▶  which functions changed?      (diff.py, AST line ranges)
           ──▶  break them, one token at a time (mutate.py, exact source spans)
           ──▶  run the matching tests         (runner.py, scoped discovery)
           ──▶  survivor? find a counterexample (witness.py, sandboxed worker)
           ──▶  emit the missing assertion     (report.py)
```

Two deliberate choices worth calling out:

**Mutations edit source text, not the AST.** `ast.unparse` would reprint the
whole file — dropping comments, reflowing code, and shifting line numbers. A tool
that reformats your repository is a tool you stop running. Every mutant replaces
one exact token span, so a mutated file differs from the original by a few
characters and nothing else.

**Test discovery is a heuristic, and it says so.** `falsify` looks for test files
that import the module under test. If it finds none, it says so and runs the full
suite rather than reporting a confident score from a suite that never ran.

---

## Relation to other tools

| | Scope | Speed | Tells you the fix |
| --- | --- | --- | --- |
| `mutmut`, `cosmic-ray` | whole repo | minutes–hours | no |
| Stryker, PIT | whole module | minutes | no |
| **`falsify`** | **changed functions** | **seconds–minutes** | **yes — the assertion** |

`falsify` is not trying to be a better `mutmut`. Full-repo mutation testing is the
right tool for a nightly job on a critical service. `falsify` is for the other
95% of pull requests, where "we'll run it tonight" means "we'll never run it".

---

## License

MIT. See [LICENSE](LICENSE).

*中文说明见 [README.zh-CN.md](README.zh-CN.md)。*
