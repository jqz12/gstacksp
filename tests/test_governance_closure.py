from pathlib import Path
import json
import sys
import tempfile
import unittest

from gsp.runtime import Project, WorkflowError, dispatch, parser, read
from gsp.document_health import audit


class GovernanceClosureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)/'project'
        self.root.mkdir()
        self.project = Project(self.root)
        self.project.initialize()
        self.cmd('new', 'governance', '--title', 'Document governance fixture')
        state = read(self.root/'.gsp/runs/governance/state.json')
        self.doc = self.root/state['doc_dir']
        self.project.document(state, 'design').write_text('# Govern the guide and verify the current document inputs.', encoding='utf-8')
        self.cmd('scope', 'governance', '--off', '--quote', 'synthetic fixture')
        self.cmd('approve', 'governance', '--quote', 'synthetic fixture approval')
        self.project.document(state, 'plan').write_text('# Maintain guide, verify inputs, review and prepare delivery.', encoding='utf-8')
        self.cmd('task-add', 'governance', 'T1', '--title', 'Maintain guide', '--acceptance', 'Guide is current', '--no-tests-reason', 'Document fixture uses explicit checks')
        self.cmd('seal-plan', 'governance')
        self.cmd('advance', 'governance', '--to', 'execute')
        self.cmd('task-start', 'governance', 'T1')

    def cmd(self, *argv):
        values = list(argv)
        cut = values.index('--') if '--' in values else len(values)
        args = parser().parse_args(values[:cut])
        if cut < len(values): args.argv = values[cut+1:]
        result, ok = dispatch(self.project, args)
        self.assertTrue(ok, result)
        return result

    def check(self, task):
        self.cmd('run', 'governance', '--task', task, '--kind', 'check', '--', sys.executable, '-c', 'print("document fixture checked")')

    def finish(self):
        self.check('T1')
        self.cmd('task-done', 'governance', 'T1')
        self.check('all')
        (self.doc/'review.md').write_text('# Current document fixture review: no blocking issues.', encoding='utf-8')
        self.cmd('advance', 'governance', '--to', 'review')
        self.cmd('review', 'governance', '--reviewer', 'self', '--blocking', '0')
        self.cmd('advance', 'governance', '--to', 'checking')
        self.cmd('advance', 'governance', '--to', 'ready')
        self.cmd('ready', 'governance')

    def test_old_delivery_append_and_health_do_not_revalidate_changed_inputs(self):
        guide = self.root/'guide.md'
        guide.write_text('# Before', encoding='utf-8')
        self.finish()
        guide.write_text('# After governance', encoding='utf-8')
        with (self.doc/'delivery.md').open('a', encoding='utf-8') as f: f.write('\nGovernance complete\n')
        audit(self.root, Path(self.tmp.name)/'reports')
        self.assertEqual(self.project.state('governance')[0]['status'], 'needs-verification')
        with self.assertRaises(WorkflowError): self.cmd('ready', 'governance')
        self.cmd('task-reopen', 'governance', 'T1', '--reason', 'New document inputs need current evidence')
        self.cmd('task-start', 'governance', 'T1')
        self.finish()
        self.assertEqual(self.project.state('governance')[0]['status'], 'ready')
        events = [json.loads(s)['event'] for s in (self.root/'.gsp/runs/governance/events.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertIn('task-reopen', events)
        self.assertEqual(events[-1], 'ready')

    def test_read_only_health_does_not_close_or_change_parent_task(self):
        before = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        audit(self.root, Path(self.tmp.name)/'reports')
        after = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(read(self.root/'.gsp/runs/governance/state.json')['status'], 'execute')
