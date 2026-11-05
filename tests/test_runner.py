import os
import sys
import tempfile
import unittest

from falsify.mutate import mutants_for_source
from falsify.runner import (
    ERROR,
    KILLED,
    SURVIVED,
    TIMEOUT,
    back_up,
    check_baseline,
    evaluate,
    explain_no_tests,
    restore_from_backup,
    run_command,
)

SAMPLE = '''
def apply_discount(price: float, coupon: float, vip: bool) -> float:
    if vip and price >= 200.0:
        price = price * 0.9
    price = price - coupon
    if price < 0:
        price = 0.0
    return round(price, 2)
'''

PASS_CMD = [sys.executable, "-c", "pass"]
FAIL_CMD = [sys.executable, "-c", "raise SystemExit(1)"]


class RunnerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "shop.py")
        with open(self.path, "w", encoding="utf-8", newline="") as fh:
            fh.write(SAMPLE)
        self.original = SAMPLE
        # evaluate() keeps its backup relative to the cwd.
        self._old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, self._old_cwd)

    def mutants(self, limit=3):
        return mutants_for_source(self.original, "apply_discount")[:limit]

    def read(self):
        with open(self.path, encoding="utf-8", newline="") as fh:
            return fh.read()


class TestRestoration(RunnerTestCase):
    def test_file_is_restored_after_a_run(self):
        evaluate(self.tmp.name, self.path, self.mutants(), PASS_CMD, timeout=20)
        self.assertEqual(self.read(), self.original)

    def test_file_is_restored_when_a_callback_explodes(self):
        def boom(outcome, index, total):
            raise RuntimeError("callback failed")

        with self.assertRaises(RuntimeError):
            evaluate(self.tmp.name, self.path, self.mutants(), PASS_CMD,
                     timeout=20, on_progress=boom)
        self.assertEqual(self.read(), self.original)

    def test_file_is_restored_on_keyboard_interrupt(self):
        def interrupt(outcome, index, total):
            if index == 2:
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            evaluate(self.tmp.name, self.path, self.mutants(), PASS_CMD,
                     timeout=20, on_progress=interrupt)
        self.assertEqual(self.read(), self.original)

    def test_backup_can_be_restored_manually(self):
        back_up(self.path)
        with open(self.path, "w", encoding="utf-8", newline="") as fh:
            fh.write("# corrupted\n")
        self.assertTrue(restore_from_backup(self.path, "shop.py"))
        self.assertEqual(self.read(), self.original)


class TestVerdicts(RunnerTestCase):
    def test_a_green_suite_kills_nothing(self):
        outcomes = evaluate(self.tmp.name, self.path, self.mutants(), PASS_CMD, timeout=20)
        self.assertTrue(all(o.status == SURVIVED for o in outcomes))

    def test_a_red_suite_kills_everything(self):
        outcomes = evaluate(self.tmp.name, self.path, self.mutants(), FAIL_CMD, timeout=20)
        self.assertTrue(all(o.status == KILLED for o in outcomes))

    def test_timeout_is_not_counted_as_killed(self):
        slow = [sys.executable, "-c", "import time; time.sleep(5)"]
        outcomes = evaluate(self.tmp.name, self.path, self.mutants(1), slow, timeout=0.5)
        self.assertEqual(outcomes[0].status, TIMEOUT)


class TestBaseline(RunnerTestCase):
    def test_green_baseline_passes(self):
        result = check_baseline(self.tmp.name, PASS_CMD, 20)
        self.assertTrue(result.ok, result.detail)
        self.assertFalse(result.no_tests)

    def test_red_baseline_is_refused(self):
        result = check_baseline(self.tmp.name, FAIL_CMD, 20)
        self.assertFalse(result.ok)
        self.assertTrue(result.detail)
        self.assertFalse(result.no_tests)

    def test_zero_tests_is_not_a_pass(self):
        """A suite that collects nothing must never look green.

        Python 3.12+ makes unittest exit 5 here, but 3.9 and 3.10 exit 0 while
        still printing "Ran 0 tests". Trusting the exit code alone therefore
        scores every mutant "survived" and reports 0% for a suite that never ran.
        """
        empty = os.path.join(self.tmp.name, "empty_suite")
        os.makedirs(empty, exist_ok=True)
        cmd = [sys.executable, "-m", "unittest", "discover", "-s", empty, "-t", empty]
        result = check_baseline(self.tmp.name, cmd, 20)
        self.assertFalse(result.ok)
        self.assertTrue(result.no_tests, result.detail)

    def test_zero_tests_is_reported_as_missing_tests_not_failing_tests(self):
        """The two failures must not be collapsed into one message.

        Telling someone "your tests fail" when no runner could find them sends
        them to debug the wrong thing, in a tool whose entire job is judging tests.
        """
        cmd = [sys.executable, "-c", "print('Ran 0 tests in 0.000s')"]
        result = check_baseline(self.tmp.name, cmd, 20)
        self.assertTrue(result.no_tests)

    def test_pytest_marker_is_recognised(self):
        cmd = [sys.executable, "-c", "print('no tests ran in 0.01s')"]
        self.assertTrue(check_baseline(self.tmp.name, cmd, 20).no_tests)

    def test_explanation_names_the_missing_runner(self):
        path = os.path.join(self.tmp.name, "test_pytest_style.py")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write("def test_something():\n    assert True\n")
        text = explain_no_tests([path])
        self.assertTrue(text)
        self.assertIn("--test-cmd", text)


if __name__ == "__main__":
    unittest.main()
