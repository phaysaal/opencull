"""Test-wide isolation from whatever is on the machine running the tests.

Presets are the photographer's own and live outside every project, which
means the ordinary place to look for them is the home directory of
whoever runs the suite. A developer with three saved presets would see
different treatment lists than one with none, and the tests would pass or
fail depending on that. They are pointed somewhere empty instead.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from opencull_gui.presets import PRESETS_ENVIRONMENT


@pytest.fixture(autouse=True, scope="session")
def _presets_of_nobody():
    with tempfile.TemporaryDirectory() as empty:
        before = os.environ.get(PRESETS_ENVIRONMENT)
        os.environ[PRESETS_ENVIRONMENT] = empty
        try:
            yield empty
        finally:
            if before is None:
                os.environ.pop(PRESETS_ENVIRONMENT, None)
            else:
                os.environ[PRESETS_ENVIRONMENT] = before
