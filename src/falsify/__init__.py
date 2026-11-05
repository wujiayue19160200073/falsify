"""falsify — mutation testing for the code you just changed.

A test that cannot fail is not a test. falsify proves which of your tests can
fail, by breaking your code on purpose and checking whether anything notices.
"""

__version__ = "0.1.1"

from .mutate import Mutant, mutants_for_source  # noqa: F401
from .report import Report  # noqa: F401
from .witness import Witness  # noqa: F401
