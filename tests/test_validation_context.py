"""Per-command validation cache boundaries and fail-closed phase checks."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from gsp.runtime import WorkflowError, atomic, digest
from gsp.validation_context import (ValidationContext, activate, current_context,
                                    deactivate)


class ValidationContextUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='gsp-validation-context-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.meta = self.root / '.gsp'
        self.state_file = self.meta / 'runs' / 'work' / 'state.json'
        self.state_file.parent.mkdir(parents=True)
        self.state_file.write_text('{"status":"execute"}', encoding='utf-8')
        self.project = SimpleNamespace(meta=self.meta)

    def context(self):
        return ValidationContext(self.project, run_id='work')

    def test_get_reuses_value_until_phase_boundary(self):
        context = self.context()
        calls = []

        def load():
            calls.append(1)
            return 'value'

        self.assertEqual(context.get('namespace', ('key',), load), 'value')
        self.assertEqual(context.get('namespace', ('key',), load), 'value')
        self.assertEqual(len(calls), 1)
        context.begin_phase('final')
        self.assertEqual(context.get('namespace', ('key',), load), 'value')
        self.assertEqual(len(calls), 2)
        self.assertEqual(context.summary()['phase'], 'final')

    def test_invalidate_drops_every_cached_value(self):
        context = self.context()
        calls = []

        def load():
            calls.append(1)
            return len(calls)

        self.assertEqual(context.get('namespace', ('key',), load), 1)
        context.invalidate('write')
        self.assertEqual(context.get('namespace', ('key',), load), 2)
        self.assertEqual(context.invalidations[-1]['reason'], 'write')

    def test_state_change_between_phases_is_rejected(self):
        context = self.context()
        context.begin_phase('initial')
        self.state_file.write_text('{"status":"ready","extra":true}', encoding='utf-8')
        with self.assertRaisesRegex(WorkflowError, 'VALIDATION_STATE_CHANGED'):
            context.assert_unchanged()
        context.begin_phase('final')
        context.assert_unchanged()

    def test_atomic_write_invalidates_active_context(self):
        context = self.context()
        previous = activate(context)
        try:
            target = self.root / 'value.txt'
            target.write_text('one', encoding='utf-8')
            first = digest(target)
            atomic(target, 'two')
            second = digest(target)
        finally:
            deactivate(previous)
        self.assertNotEqual(first, second)
        self.assertIsNone(current_context())

    def test_digest_cache_does_not_hide_changes_after_phase_restart(self):
        context = self.context()
        previous = activate(context)
        try:
            target = self.root / 'value.txt'
            target.write_text('one', encoding='utf-8')
            first = digest(target)
            target.write_text('two', encoding='utf-8')
            self.assertEqual(digest(target), first)
            context.begin_phase('final')
            self.assertNotEqual(digest(target), first)
        finally:
            deactivate(previous)

    def test_deactivate_restores_previous_context(self):
        outer = self.context()
        inner = self.context()
        self.assertIsNone(current_context())
        previous = activate(outer)
        nested = activate(inner)
        self.assertIs(current_context(), inner)
        deactivate(nested)
        self.assertIs(current_context(), outer)
        deactivate(previous)
        self.assertIsNone(current_context())


if __name__ == '__main__':
    unittest.main()
