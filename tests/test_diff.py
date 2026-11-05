import unittest

from falsify.diff import functions_at, parse_diff

DIFF = """diff --git a/shop.py b/shop.py
--- a/shop.py
+++ b/shop.py
@@ -10,0 +11,2 @@
+    price = price * 0.9
+    price = price - coupon
diff --git a/other.txt b/other.txt
--- a/other.txt
+++ b/other.txt
@@ -1 +1 @@
+not python
"""

SOURCE = '''class Order:
    def total(self):
        return 0

    def apply_discount(self, price):
        if price >= 200.0:
            price = price * 0.9
        return price


def standalone(x):
    return x + 1
'''


class TestParseDiff(unittest.TestCase):
    def test_python_files_only(self):
        changes = parse_diff(DIFF)
        self.assertEqual([c.path for c in changes], ["shop.py"])

    def test_added_line_numbers(self):
        changes = parse_diff(DIFF)
        self.assertEqual(changes[0].lines, {11, 12})

    def test_deleted_file_is_skipped(self):
        diff = "--- a/x.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x = 1\n"
        self.assertEqual(parse_diff(diff), [])


class TestFunctionsAt(unittest.TestCase):
    def test_finds_method_containing_line(self):
        names = functions_at(SOURCE, {6})
        self.assertIn("Order.apply_discount", names)

    def test_finds_standalone_function(self):
        names = functions_at(SOURCE, {11})
        self.assertIn("standalone", names)

    def test_returns_nothing_for_blank_line(self):
        # Line 4 is the blank line between the two methods.
        self.assertEqual(functions_at(SOURCE, {4}), [])

    def test_broken_source_does_not_crash(self):
        self.assertEqual(functions_at("def (:", {1}), [])


if __name__ == "__main__":
    unittest.main()
