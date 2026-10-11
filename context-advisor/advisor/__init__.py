"""context-advisor — truthful context-pressure model for Hermes.

Pure, stdlib-only core. No Hermes imports here so it is unit-testable in isolation
(the Hermes-facing shim lives in the plugin's ``__init__.py`` and in
``session_db.py``).
"""

from .core import (
    Budgets,
    Snapshot,
    Pressure,
    build_pressure,
    level_for,
    LEVELS,
)

__all__ = ["Budgets", "Snapshot", "Pressure", "build_pressure", "level_for", "LEVELS"]
