"""A small, realistic pricing module.

This file is deliberately ordinary: it looks like the kind of code that ships
every day, written quickly, reviewed quickly, tested with a green tick.
"""

VIP_THRESHOLD = 200.0
MAX_REFUND_DAYS = 30
HIGH_VALUE = 500.0


def apply_discount(price: float, coupon: float, vip: bool) -> float:
    """VIP orders over the threshold get 10% off, then the coupon comes off."""
    if vip and price >= VIP_THRESHOLD:
        price = price * 0.9
    price = price - coupon
    if price < 0:
        price = 0.0
    return round(price, 2)


def can_refund(order_total: float, days_since: int, has_receipt: bool) -> bool:
    """Refunds within 30 days; high-value orders only within 14."""
    if days_since > MAX_REFUND_DAYS or not has_receipt:
        return False
    if order_total > HIGH_VALUE and days_since > 14:
        return False
    return True


def shipping_fee(weight: float, express: bool = False) -> float:
    """Flat rate up to 1kg, then per-kilo bands."""
    if weight <= 1:
        fee = 8.0
    elif weight <= 5:
        fee = 8.0 + (weight - 1) * 2.0
    else:
        fee = 16.0 + (weight - 5) * 1.5
    if express:
        fee = fee * 1.5
    return round(fee, 2)


def loyalty_tier(spend: float, orders: int) -> str:
    """Simple tiering used by the checkout page."""
    if spend >= 1000 and orders >= 10:
        return "gold"
    if spend >= 500 or orders >= 5:
        return "silver"
    return "basic"
