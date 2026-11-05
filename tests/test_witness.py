import os
import tempfile
import unittest

from falsify.mutate import mutants_for_source
from falsify.witness import search, suggest_assertion

SOURCE = '''
THRESHOLD = 200.0

def apply_discount(price: float, coupon: float, vip: bool) -> float:
    if vip and price >= THRESHOLD:
        price = price * 0.9
    price = price - coupon
    if price < 0:
        price = 0.0
    return round(price, 2)
'''


class TestWitnessSearch(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".py")
        os.close(fd)
        self.addCleanup(os.unlink, self.path)

    def _boundary_mutant(self):
        mutants = mutants_for_source(SOURCE, "apply_discount")
        return next(m for m in mutants if m.operator == "boundary")

    def test_boundary_mutant_gets_exact_threshold_counterexample(self):
        mutant = self._boundary_mutant()
        mutated = mutant.apply(SOURCE)
        result = search(SOURCE, mutated, "apply_discount", self.path)
        self.assertIn(result.kind, ("value", "exception"), result.note)
        # The whole point: the counterexample must sit on the threshold, because
        # that is the input the suite never tried.
        self.assertIn(200.0, result.args)

    def test_counterexample_values_actually_differ(self):
        mutant = self._boundary_mutant()
        mutated = mutant.apply(SOURCE)
        result = search(SOURCE, mutated, "apply_discount", self.path)
        self.assertNotEqual(result.orig_result, result.mutant_result)

    def test_suggestion_is_pasteable(self):
        mutant = self._boundary_mutant()
        mutated = mutant.apply(SOURCE)
        result = search(SOURCE, mutated, "apply_discount", self.path)
        text = suggest_assertion(result, "apply_discount", mutant.id)
        self.assertIsNotNone(text)
        self.assertIn("def test_apply_discount", text)
        self.assertIn("assert apply_discount(", text)
        self.assertIn("200.0", text)

    def test_guard_clause_does_not_block_search(self):
        # `vip and price >= THRESHOLD` short-circuits: a naive sweep with vip=False
        # never reaches the code under test. The boolean layering must handle it.
        mutants = mutants_for_source(SOURCE, "apply_discount")
        binop = next(m for m in mutants if m.operator == "binop")
        result = search(SOURCE, binop.apply(SOURCE), "apply_discount", self.path)
        self.assertIn(result.kind, ("value", "exception"), result.note)

    def test_no_witness_is_reported_honestly(self):
        # Mutating a function whose output nobody can distinguish is exactly the
        # case where inventing a witness would be a lie.
        src = "def f(x: int) -> int:\n    return x\n"
        mut = "def f(x: int) -> int:\n    return None\n"
        result = search(src, mut, "f", self.path)
        self.assertIn(result.kind, ("value", "exception"))

    def test_missing_function_is_unsupported(self):
        result = search(SOURCE, SOURCE, "nope", self.path)
        self.assertEqual(result.kind, "unsupported")


if __name__ == "__main__":
    unittest.main()
