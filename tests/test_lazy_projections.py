"""Ordinary state writes do not regenerate optional display projections."""
import unittest
from unittest.mock import patch

from gsp.runtime import Project
from tests import test_project_write_recovery as fixtures


class LazyProjectionTests(unittest.TestCase):
    def test_ordinary_write_does_not_render_dashboard(self):
        fixture = fixtures.ProjectWriteRecoveryTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        state, _, event_file = fixture.seed_state()
        with patch.object(Project, 'dashboard', side_effect=AssertionError('optional projection rendered')):
            fixture.project.write(state, 'local-change', 'authoritative facts only')
        fixture.assert_complete_projection(event_file, 'local-change', 'authoritative facts only')
