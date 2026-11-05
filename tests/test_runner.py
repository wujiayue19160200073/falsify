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
    restore_from_backup,
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
        ok, why = check_baseline(self.tmp.name, PASS_CMD, 20)
        self.assertTrue(ok, why)

    def test_red_baseline_is_refused(self):
        ok, why = check_baseline(self.tmp.name, FAIL_CMD, 20)
        self.assertFalse(ok)
        self.assertTrue(why)


if __name__ == "__main__":
    unittest.main()
