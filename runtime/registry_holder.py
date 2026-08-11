"""Shared boilerplate for handler modules that need late-bound access to the
live webhook Registry.

Registry construction happens in runtime/combined_app.py's bootstrap, AFTER
the handler modules (handlers/create_bot.py, handlers/manage_bots.py,
handlers/custom_features.py) are already imported and their routers wired
into the Dispatcher — so each of those modules can't just import a Registry
instance at module load time, it needs a slot that starts empty and gets
filled in later via a setter.

Each handler module owns its OWN RegistryHandle instance (not one shared
global) — combined_app.py calls each module's set_registry() individually
after constructing the Registry (see its own bootstrap for the three calls).
This class only collapses the `_registry = None` / `global _registry` /
`_registry = registry` triplication that used to be copy-pasted identically
into all three modules; it does not change that per-module ownership model.
Still None (get() returns None) when running under main.py's separate
long-polling process, since no Registry exists there at all."""
from __future__ import annotations


class RegistryHandle:
    def __init__(self) -> None:
        self._registry = None

    def set(self, registry) -> None:
        self._registry = registry

    @property
    def value(self):
        return self._registry
