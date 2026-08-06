"""Regression for the "# COMPATIBLE_WITH:" header in features/*.py lagging
behind templates/*.py — see features/sheets.py and features/payments.py history:
both lists were written before templates/orders_tracker.py, (payments.py only)
templates/booking_fitness.py, templates/event_manager.py, and
templates/shop_catalog.py existed, so bots built from those templates had NO
features available in the "🧩 Фичи" panel (runtime/registry.py's
discover_features() has no "all" sentinel by design — an unlisted template_id
is simply incompatible with everything).

Driven through the REAL features/*.py files (not a fixture dir) — this is
exactly what handlers/manage_bots.py's _compatible_features() consults.
"""
from __future__ import annotations

import unittest

from runtime.registry import discover_features


class OrdersTrackerAndBookingFitnessAreFeatureCompatible(unittest.TestCase):
    def setUp(self):
        self.features = {f["name"]: f["compatible_with"] for f in discover_features()}

    def test_sheets_lists_orders_tracker(self):
        self.assertIn("orders_tracker", self.features["sheets"])

    def test_payments_lists_orders_tracker(self):
        self.assertIn("orders_tracker", self.features["payments"])

    def test_payments_lists_booking_fitness(self):
        self.assertIn("booking_fitness", self.features["payments"])

    def test_sheets_lists_event_manager(self):
        self.assertIn("event_manager", self.features["sheets"])

    def test_payments_lists_event_manager(self):
        self.assertIn("event_manager", self.features["payments"])

    def test_sheets_lists_shop_catalog(self):
        self.assertIn("shop_catalog", self.features["sheets"])

    def test_payments_lists_shop_catalog(self):
        self.assertIn("shop_catalog", self.features["payments"])


if __name__ == "__main__":
    unittest.main()
