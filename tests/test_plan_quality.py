import json
import io
import os
from argparse import Namespace
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from process_helpers import run, document, session_binding, temporary_env
from gsp.plan_quality import check_plan, inspect_plan, write_plan_gate

ROOT = Path(__file__).resolve().parents[1]


def complete(task='T1'):
    return f'''# Example Implementation Plan
**Goal:** Fix a label.
**Architecture:** Keep the existing page.
**Tech Stack:** Markdown.
**Spec:** [spec](spec.md)
## Global Constraints
Windows, Python 3.10+.
## Review Focus
Preserve existing links; inspect their targets in {task}.
## Task {task}: Label
**Files:** Modify docs/README.md.
Convergence: {{"requires_slices":false,"single_outcome":"links intact"}}
**Interfaces:**
- Consumes: N/A — no program interface changes, read existing prose.
- Produces: N/A — only a corrected label, no exported API.
- [ ] Edit the label.
- [ ] Run: python scripts/check_links.py
  Expected: exit 0, all local links exist.
## Self-Review
Coverage, steps, type consistency, failure cases and proportion checked.
'''


def write_plan(first, second, depends=None):
    depends = depends or []
    first = json.dumps(first)
    second = json.dumps(second)
    return f'''# Two task plan
**Goal:** Exercise scheduling writes.
**Architecture:** Reuse plan quality.
**Tech Stack:** Python.
**Spec:** [spec](spec.md)
## Global Constraints
Conservative conflict checks.
## Review Focus
Review both tasks.
## Task T01: First
**Files:** Modify shared source.
**Writes:** {{"paths":[{first}]}}
**Interfaces:**
- Consumes: N/A.
- Produces: N/A.
- [ ] Implement T01.
- [ ] Run: python -m unittest tests.test_plan_quality
  Expected: exit 0.
## Task T02: Second
**Files:** Modify shared source.
**Writes:** {{"paths":[{second}]}}
**Interfaces:**
- Consumes: T01 output when ordered.
- Produces: N/A.
- [ ] Implement T02.
- [ ] Run: python -m unittest tests.test_plan_quality
  Expected: exit 0.
## Self-Review
Paths, dependencies and verification reviewed.
'''


class PlanProject:
    def __init__(self, root, state, plan):
        self.root = root
        self._state = state
        self._plan = plan

    def identity(self):
        return {'id': 'project'}

    def paths(self, task_id):
        run = self.root / '.gsp/runs' / task_id
        run.mkdir(parents=True, exist_ok=True)
        (run / 'state.json').write_text(
            json.dumps(self._state), encoding='utf-8',
        )
        return run, self._plan.parent

    def document(self, state, kind):
        return self._plan


class PlanQualityTests(unittest.TestCase):
    def test_complete_and_document_only_plans(self):
        result = check_plan(complete(), ['T1'])
        self.assertTrue(result['structural_ok'], result)
        self.assertEqual(result['semantic_review'], 'NOT_CHECKED')

    def test_missing_task_and_empty_sections(self):
        result = check_plan(complete().replace('Windows, Python 3.10+.', ''), ['T1', 'T2'])
        self.assertFalse(result['structural_ok'])
        self.assertIn('TASK_MISSING', {x['code'] for x in result['findings']})
        self.assertIn('GLOBAL_CONSTRAINTS_MISSING', {x['code'] for x in result['findings']})

    def test_code_and_comments_cannot_supply_structure(self):
        for text in ('```markdown\n' + complete() + '\n```', '<!--\n'+complete()+'\n-->'):
            with self.subTest(text=text[:10]):
                self.assertFalse(check_plan(text, ['T1'])['structural_ok'])

    def test_each_task_needs_own_contract_and_verification(self):
        for missing in ('**Files:** Modify docs/README.md.', '- Consumes: N/A — no program interface changes, read existing prose.',
                        '- Produces: N/A — only a corrected label, no exported API.',
                        '  Expected: exit 0, all local links exist.', '- [ ] Run: python scripts/check_links.py'):
            with self.subTest(missing=missing):
                self.assertFalse(check_plan(complete().replace(missing, ''), ['T1'])['structural_ok'])

    def test_duplicate_tasks_and_placeholders(self):
        self.assertFalse(check_plan(complete() + complete(), ['T1'])['structural_ok'])
        self.assertFalse(check_plan(complete().replace('Fix a label.', 'TBD'), ['T1'])['structural_ok'])

    def test_task_heading_without_colon_and_header_not_borrowed(self):
        text = complete().replace('Task T1: Label', 'T1 Label')
        self.assertTrue(check_plan(text, ['T1'])['structural_ok'])
        text = text.replace('**Goal:** Fix a label.\n', '').replace('**Files:**', '**Goal:** borrowed from task\n**Files:**')
        self.assertFalse(check_plan(text, ['T1'])['structural_ok'])

    def test_chinese_labels_and_numbered_task(self):
        text = complete().replace('Task T1:', '任务 T1：')
        for a, b in [('Goal', '目标'), ('Architecture', '架构'), ('Tech Stack', '技术栈'), ('Spec', '规格'),
                     ('Global Constraints', '全局约束'), ('Review Focus', '审查重点'), ('Self-Review', '自查'),
                     ('Files', '文件'), ('Interfaces', '接口'), ('Consumes', '消费'), ('Produces', '产出'),
                     ('Run', '运行'), ('Expected', '预期')]:
            text = text.replace(a, b)
        self.assertTrue(check_plan(text, ['T1'])['structural_ok'])

    def test_structure_does_not_claim_semantic_approval(self):
        text = complete().replace('no exported API.', 'export f(x: str) -> int; later caller expects bool.')
        result = check_plan(text, ['T1'])
        self.assertTrue(result['structural_ok'])
        self.assertEqual(result['semantic_review'], 'NOT_CHECKED')

    def test_nested_task_cannot_supply_empty_parent_task(self):
        text = complete('T2').replace('## Task T2: Label', '## Task T1: Empty\n### Task T2: Label')
        result = check_plan(text, ['T1', 'T2'])
        self.assertFalse(result['structural_ok'])
        self.assertIn('T1', {row['task'] for row in result['findings']})
        self.assertNotIn('T2', {row['task'] for row in result['findings']})

    def test_required_section_needs_prose_not_just_subheadings(self):
        for content in ('### Placeholder', '### Placeholder\n---\n<!-- comment -->\n```\nexample\n```'):
            result = check_plan(complete().replace('Windows, Python 3.10+.', content), ['T1'])
            self.assertFalse(result['structural_ok'])
            self.assertIn('GLOBAL_CONSTRAINTS_MISSING', {row['code'] for row in result['findings']})

    def _inspect(self, plan, depends=()):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        path = root / 'plan.md'
        path.write_text(plan, encoding='utf-8')
        state = {
            'id': 'sample',
            'project_id': 'project',
            'work_uuid': 'work',
            'tasks': [
                {'id': 'T01', 'title': 'First', 'acceptance': 'works',
                 'depends': []},
                {'id': 'T02', 'title': 'Second', 'acceptance': 'works',
                 'depends': list(depends)},
            ],
        }
        return inspect_plan(PlanProject(root, state, path), 'sample')

    def test_inspect_plan_rejects_unordered_parent_child_write_conflict(self):
        result = self._inspect(write_plan('src/shared', 'SRC\\shared\\file.py'))
        self.assertFalse(result['structural_ok'])
        self.assertEqual(result['write_coverage']['status'], 'COMPLETE')
        self.assertIn('WRITE_CONFLICT', {row['code'] for row in result['findings']})
        self.assertFalse(result['write_coverage']['edit_allowlist'])
        self.assertFalse(result['write_coverage']['parallel_verified'])

    def test_inspect_plan_allows_declared_ordered_shared_write(self):
        result = self._inspect(
            write_plan('src/shared', 'src/shared/file.py'), depends=['T01'],
        )
        self.assertTrue(result['structural_ok'], result)
        self.assertEqual(result['write_conflicts'], [])
        self.assertEqual(result['write_coverage']['status'], 'COMPLETE')
        self.assertTrue(result['write_coverage']['parallel_verified'])

    def test_inspect_plan_rejects_invalid_windows_drive_path(self):
        result = self._inspect(write_plan('C:\\work\\one.py', 'src/two.py'))
        self.assertFalse(result['structural_ok'])
        self.assertIn(
            'WRITE_PATH_INVALID',
            {row['code'] for row in result['findings']},
        )
        self.assertEqual(result['write_coverage']['status'], 'INCOMPLETE')
        self.assertFalse(result['write_coverage']['parallel_verified'])

    def test_unknown_legacy_coverage_is_not_parallel_verified(self):
        result = self._inspect('# Legacy plan\nKeep compatibility.')
        self.assertEqual(result['write_coverage']['status'], 'NOT_CHECKED')
        self.assertEqual(
            result['write_coverage']['reason'],
            'TASK_CONTRACT_CONTEXT_REQUIRED',
        )
        self.assertFalse(result['write_coverage']['parallel_verified'])

    def test_fenced_writes_example_is_ignored_by_the_shared_parser(self):
        text = write_plan('src/real-one', 'src/real-two')
        text = text.replace(
            '**Writes:** {"paths":["src/real-one"]}',
            '```markdown\n'
            '**Writes:** {"paths":["src/fenced-example"]}\n'
            '```\n'
            '**Writes:** {"paths":["src/real-one"]}',
        )
        state = {
            'id': 'sample',
            'project_id': 'project',
            'work_uuid': 'work',
            'tasks': [
                {'id': 'T01', 'title': 'First', 'acceptance': 'works',
                 'depends': []},
                {'id': 'T02', 'title': 'Second', 'acceptance': 'works',
                 'depends': []},
            ],
        }
        gate = write_plan_gate(state, text, parallel=True)
        self.assertEqual(gate['blockers'], [])
        self.assertTrue(gate['coverage']['parallel_verified'])
        self.assertEqual(gate['conflicts'], [])

    def test_cli_read_only_and_checked_seal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = os.environ.copy()
            env.pop('GSP_SESSION_CONTEXT', None)
            env.update({
                'HOME': directory,
                'USERPROFILE': directory,
                'CODEX_HOME': str(root / '.codex'),
            })
            def cli(*args):
                return run([sys.executable, str(ROOT/'scripts/gsp.py'), '--project', directory, *args],
                           capture_output=True, text=True, encoding='utf-8',
                           env=env)
            self.assertEqual(cli('init').returncode, 0)
            self.assertEqual(cli('new', 'sample', '--title', 'Example').returncode, 0)
            state = json.loads(
                (root/'.gsp/runs/sample/state.json').read_text(
                    encoding='utf-8',
                )
            )
            binding = session_binding(
                root, state['work_uuid'], 'main', 'codex', 'plan-main',
            )
            env['GSP_SESSION_CONTEXT'] = str(root / binding.handle_path)
            document(root, 'sample', 'design').write_text('# Design\nChange the label, preserve links.', encoding='utf-8')
            self.assertEqual(cli('approve', 'sample', '--quote', 'SYNTHETIC approval').returncode, 0)
            self.assertEqual(cli('task-add', 'sample', 'T1', '--title', 'Label', '--acceptance', 'links intact').returncode, 0)
            plan = document(root, 'sample', 'plan')
            plan.write_text('# A vague plan\nChange a label.', encoding='utf-8')
            def snapshot():
                return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}
            before = snapshot()
            result = cli('plan-check', 'sample')
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertFalse(json.loads(result.stdout)['structural_ok'])
            self.assertEqual(snapshot(), before)
            result = cli('seal-plan', 'sample', '--check-structure')
            self.assertEqual(result.returncode, 1)
            self.assertIn('PLAN_STRUCTURE_INVALID', result.stderr)
            self.assertIsNone(json.loads((root/'.gsp/runs/sample/state.json').read_text(encoding='utf-8'))['plan_seal'])
            self.assertEqual(cli('seal-plan', 'sample').returncode, 0)  # legacy compatibility
            plan.write_text(complete(), encoding='utf-8')
            before = snapshot()
            result = cli('plan-check', 'sample')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(snapshot(), before)
            self.assertEqual(cli('seal-plan', 'sample', '--check-structure').returncode, 0)
            plan.write_bytes(b'a' * (1024 * 1024 + 1))
            result = cli('plan-check', 'sample')
            self.assertEqual(result.returncode, 1)
            self.assertIn('PLAN_TOO_LARGE', result.stderr)

            # The checked seal must reject oversized input before an unbounded read_text.
            from gsp.runtime import main, Project, dispatch
            original = Path.read_text
            def guarded(path, *args, **kwargs):
                if path == plan:
                    raise AssertionError('unbounded plan read before size validation')
                return original(path, *args, **kwargs)
            stderr = io.StringIO()
            direct_env = {
                key: env[key]
                for key in (
                    'GSP_SESSION_CONTEXT', 'HOME', 'USERPROFILE', 'CODEX_HOME',
                )
            }
            with (
                temporary_env(direct_env),
                patch.object(Path, 'read_text', guarded),
                redirect_stdout(io.StringIO()),
                redirect_stderr(stderr),
            ):
                code = main(['--project', directory, 'seal-plan', 'sample', '--check-structure'])
            self.assertEqual(code, 1)
            self.assertIn('PLAN_TOO_LARGE', stderr.getvalue())
            plan.write_text(complete(), encoding='utf-8')
            # Driver and other pre-existing internal callers omit argparse's new field.
            with temporary_env(direct_env):
                result, ok = dispatch(
                    Project(directory),
                    Namespace(command='seal-plan', id='sample'),
                )
            self.assertTrue(ok)
            self.assertEqual(result['event'], 'seal-plan')


if __name__ == '__main__':
    unittest.main()
