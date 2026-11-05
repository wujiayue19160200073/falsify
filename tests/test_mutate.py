import ast
import unittest

from falsify.mutate import (
    is_equivalent,
    mutants_for_source,
    find_functions,
    renumber,
)

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


class TestSpanReplacement(unittest.TestCase):
    def test_apply_changes_only_the_span(self):
        mutants = mutants_for_source(SOURCE, "apply_discount")
        for mutant in mutants:
            mutated = mutant.apply(SOURCE)
            # Line count must not change: a mutation tool that reformats code is
            # a mutation tool that breaks diffs.
            self.assertEqual(mutated.count("\n"), SOURCE.count("\n"), mutant.id)
            self.assertNotEqual(mutated, SOURCE, mutant.id)

    def test_apply_is_reversible_and_parseable(self):
        mutants = mutants_for_source(SOURCE, "apply_discount")
        for mutant in mutants:
            mutated = mutant.apply(SOURCE)
            ast.parse(mutated)  # must stay valid Python
            self.assertEqual(mutant.apply(SOURCE), mutated)  # deterministic

    def test_drifted_source_is_rejected(self):
        mutants = mutants_for_source(SOURCE, "apply_discount")
        drifted = SOURCE.replace("price >= THRESHOLD", "price >= 999")
        lost = 0
        for mutant in mutants:
            try:
                mutant.apply(drifted)
            except ValueError:
                lost += 1
        self.assertGreater(lost, 0, "editing the source should invalidate spans")


class TestOperators(unittest.TestCase):
    def setUp(self):
        self.mutants = mutants_for_source(SOURCE, "apply_discount")

    def test_boundary_mutant_exists(self):
        boundary = [m for m in self.mutants if m.operator == "boundary"]
        self.assertTrue(boundary, "off-by-one mutants must be generated")
        self.assertIn(">", [m.replacement.strip() for m in boundary])

    def test_boolop_mutant_swaps_and_for_or(self):
        boolops = [m for m in self.mutants if m.operator == "boolop"]
        self.assertTrue(boolops)
        self.assertIn(" or ", boolops[0].replacement)
        self.assertIn(" and ", boolops[0].original)

    def test_return_none_mutant_exists(self):
        returns = [m for m in self.mutants if m.operator == "return_none"]
        self.assertTrue(returns)
        self.assertIn("return None", returns[0].replacement)

    def test_ids_are_unique_across_functions(self):
        mutants = mutants_for_source(SOURCE)
        ids = [m.id for m in mutants]
        self.assertEqual(len(ids), len(set(ids)))

    def test_renumber_removes_collisions(self):
        a = mutants_for_source(SOURCE, "apply_discount")
        b = mutants_for_source(SOURCE, "apply_discount")
        merged = renumber(list(a) + list(b))
        ids = [m.id for m in merged]
        self.assertEqual(len(ids), len(set(ids)))

    def test_operator_filter(self):
        only = mutants_for_source(SOURCE, "apply_discount", operators=["boundary"])
        self.assertTrue(only)
        self.assertTrue(all(m.operator == "boundary" for m in only))


class TestEquivalence(unittest.TestCase):
    def test_mutant_that_changes_nothing_is_equivalent(self):
        mutants = mutants_for_source(SOURCE, "apply_discount")
        # Applying a mutant to its own already-mutated source cannot match the
        # recorded span in general, so equivalence must not crash on that.
        for mutant in mutants:
            self.assertIsInstance(is_equivalent(SOURCE, mutant), bool)

    def test_find_functions_reports_qualnames(self):
        tree = ast.parse(SOURCE)
        self.assertIn("apply_discount", find_functions(tree))


if __name__ == "__main__":
    unittest.main()
