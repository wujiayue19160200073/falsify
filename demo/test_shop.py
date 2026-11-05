"""Tests for shop.py.

Every test here passes. Coverage is close to 100%. Nothing in this file would
notice if half of shop.py were deleted and replaced with something wrong.

That is the point of the demo: this is what AI-generated tests look like when
nobody reads them.
"""

import unittest

from shop import apply_discount, can_refund, loyalty_tier, shipping_fee


class TestDiscount(unittest.TestCase):
    def test_vip_discount_returns_something(self):
        result = apply_discount(300.0, 10.0, True)
        # Asserts the shape, not the value. Passes for 300.0, 0.0, or -999.
        self.assertIsNotNone(result)
        self.assertIsInstance(result, float)

    def test_plain_order(self):
        self.assertEqual(apply_discount(100.0, 5.0, False), 95.0)

    def test_never_negative(self):
        self.assertEqual(apply_discount(10.0, 50.0, False), 0.0)


class TestRefund(unittest.TestCase):
    def test_recent_order_can_refund(self):
        self.assertTrue(can_refund(100.0, 5, True))

    def test_old_order_cannot_refund(self):
        self.assertFalse(can_refund(100.0, 40, True))


class TestShipping(unittest.TestCase):
    def test_light_parcel(self):
        self.assertEqual(shipping_fee(0.5), 8.0)

    def test_medium_parcel(self):
        self.assertEqual(shipping_fee(3.0), 12.0)


class TestLoyalty(unittest.TestCase):
    def test_some_tier_is_returned(self):
        tier = loyalty_tier(1200.0, 12)
        self.assertIn(tier, ["gold", "silver", "basic"])


if __name__ == "__main__":
    unittest.main()
