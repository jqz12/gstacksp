import json
import io
from argparse import Namespace
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from process_helpers import run, document
from gsp.plan_quality import check_plan

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
**Interfaces:**
- Consumes: N/A — no program interface changes, read existing prose.
- Produces: N/A — only a corrected label, no exported API.
- [ ] Edit the label.
- [ ] Run: python scripts/check_links.py
  Expected: exit 0, all local links exist.
## Self-Review
Coverage, steps, type consistency, failure cases and proportion checked.
'''


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

    def test_cli_read_only_and_checked_seal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def cli(*args):
                return run([sys.executable, str(ROOT/'scripts/gsp.py'), '--project', directory, *args],
                           capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(cli('init').returncode, 0)
            self.assertEqual(cli('new', 'sample', '--title', 'Example').returncode, 0)
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
            with patch.object(Path, 'read_text', guarded), redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                code = main(['--project', directory, 'seal-plan', 'sample', '--check-structure'])
            self.assertEqual(code, 1)
            self.assertIn('PLAN_TOO_LARGE', stderr.getvalue())
            plan.write_text(complete(), encoding='utf-8')
            # Driver and other pre-existing internal callers omit argparse's new field.
            result, ok = dispatch(Project(directory), Namespace(command='seal-plan', id='sample'))
            self.assertTrue(ok)
            self.assertEqual(result['event'], 'seal-plan')


if __name__ == '__main__':
    unittest.main()
