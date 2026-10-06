"""Lab indicators. Importing this package registers the starter set.

To add one: write a pure function in a module here, decorate it with
``@indicator("name", setting=(default, min, max))``, import the module below,
and add a unit test of its math. The generic lookahead test in
``tests/test_lab_indicators.py`` covers every registered indicator automatically.
"""

from quant_system.lab.indicators import core  # noqa: F401  (registers the starter set)
from quant_system.lab.indicators import volume  # noqa: F401  (volume studies)
from quant_system.lab.indicators.registry import (
    BAR_COLUMNS,
    REGISTRY,
    IndicatorDef,
    get_indicator,
    indicator,
    lookahead_violations,
)

__all__ = [
    "BAR_COLUMNS",
    "REGISTRY",
    "IndicatorDef",
    "get_indicator",
    "indicator",
    "lookahead_violations",
]
