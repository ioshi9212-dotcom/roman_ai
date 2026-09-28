"""Roman AI package bootstrap.

Gameplay uses one explicit turn pipeline. Setup keeps its existing isolated setup
adapters for compatibility; no gameplay module is allowed to wrap prepare/commit.
"""

from .foundation_coverage_runtime import install as _install_foundation_coverage
from .draft_intake_runtime import install as _install_draft_intake
from .setup_draft_v3_runtime import install as _install_setup_draft_v3
from .simple_setup_runtime import install as _install_simple_setup
from .turn_pipeline import install as _install_turn_pipeline


# Setup-only compatibility. These functions patch draft construction, not gameplay turns.
_install_foundation_coverage()
_install_draft_intake()
_install_setup_draft_v3()
_install_simple_setup()

# The only gameplay installation.
_install_turn_pipeline()


del _install_foundation_coverage
del _install_draft_intake
del _install_setup_draft_v3
del _install_simple_setup
del _install_turn_pipeline
