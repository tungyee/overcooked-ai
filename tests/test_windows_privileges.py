import sys
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows token ABI")

from ai.output.windows_privileges import integrity_report, process_integrity


def test_reads_current_process_integrity():
    import os
    assert process_integrity(os.getpid()) in (0x1000, 0x2000, 0x2100, 0x3000, 0x4000)


def test_elevated_game_requires_elevation():
    with patch("ai.output.windows_privileges.process_integrity", side_effect=[0x2000, 0x3000]):
        assert integrity_report(123)["requires_elevation"]


def test_same_integrity_does_not_require_elevation():
    with patch("ai.output.windows_privileges.process_integrity", side_effect=[0x3000, 0x3000]):
        assert not integrity_report(123)["requires_elevation"]
