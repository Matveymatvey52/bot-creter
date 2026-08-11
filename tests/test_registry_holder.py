"""runtime/registry_holder.py's RegistryHandle — the shared boilerplate that
replaced the byte-identical `_registry = None` / `global _registry` block
that used to be copy-pasted into handlers/create_bot.py, handlers/manage_bots.py
and handlers/custom_features.py. Each of those three handler modules gets its
OWN RegistryHandle instance; this file only tests the class itself, the
per-module integration is already covered by each handler's own test file
(tests/test_factory_registry_citizen.py, tests/test_manage_bots_features.py,
tests/test_custom_features_handler.py all call set_registry() and assert on
its effect).
"""
from __future__ import annotations

import unittest

from runtime.registry_holder import RegistryHandle


class RegistryHandleTests(unittest.TestCase):
    def test_starts_empty(self):
        handle = RegistryHandle()
        self.assertIsNone(handle.value)

    def test_set_updates_value(self):
        handle = RegistryHandle()
        sentinel = object()
        handle.set(sentinel)
        self.assertIs(handle.value, sentinel)

    def test_set_none_clears_value(self):
        handle = RegistryHandle()
        handle.set(object())
        handle.set(None)
        self.assertIsNone(handle.value)

    def test_two_instances_are_independent(self):
        a = RegistryHandle()
        b = RegistryHandle()
        a.set(object())
        self.assertIsNone(b.value)


if __name__ == "__main__":
    unittest.main()
