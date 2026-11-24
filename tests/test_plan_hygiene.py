"""Plans that create build or temporary output must declare its lifecycle."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gsp.plan_quality import check_plan, hygiene_needed, hygiene_section


def plan(extra_body='', hygiene=None):
    section = ''
    if hygiene is not None:
        section = '\n## 产物与临时文件生命周期\n\n' + hygiene + '\n'
    return f'''# Example Plan
**Goal:** Exercise temporary output.
**Architecture:** Keep the existing layout.
**Tech Stack:** Python unittest.
**Spec:** [spec](spec.md)
## Global Constraints
Windows, Python 3.10+.
## Review Focus
Inspect cleanup boundaries in T01.
## Task T01: Temporary output
**Files:** Modify tests/helper.py.
**Interfaces:**
- Consumes: unittest.
- Produces: a temporary folder helper.
- [ ] Run: python -m pytest {extra_body}
  Expected: tests pass and no folder remains.
{section}
## Self-Review
Coverage, steps, limits and failure modes checked.
'''


COMPLETE_SECTION = '''- 产物位置：`target/tmp` only during the run.
- 回收责任：`gsp run` reclaims entries created by the execution.
- 保留与上限：keep at most one failed entry, 50 MiB.
- 验收方式：post-run assertion that no new entry remains.
'''


class PlanHygieneTests(unittest.TestCase):
    def test_trigger_keywords(self):
        self.assertFalse(hygiene_needed('# Plan\nChange a label in docs/README.md.'))
        for word in ('pytest', 'target/tmp', 'CARGO_TARGET_DIR', 'worktree', '临时目录'):
            with self.subTest(word=word):
                self.assertTrue(hygiene_needed('# Plan\nBuild with ' + word))
        self.assertFalse(hygiene_needed('```\npytest target/tmp\n```'))

    def test_section_requires_all_four_fields(self):
        self.assertFalse(hygiene_section('## 产物与临时文件生命周期\n\nOnly a title.\n'))
        for line in COMPLETE_SECTION.splitlines():
            with self.subTest(line=line):
                partial = COMPLETE_SECTION.replace(line + '\n', '')
                self.assertFalse(hygiene_section('## 产物与临时文件生命周期\n\n' + partial))
        text = '## 产物与临时文件生命周期\n\n' + COMPLETE_SECTION
        self.assertTrue(hygiene_section(text))
        english = text.replace('产物与临时文件生命周期', 'Product & Lifecycle')
        self.assertTrue(hygiene_section(english))

    def test_missing_section_is_warning_by_default_and_blocking_when_required(self):
        text = plan(hygiene=None)
        warning = check_plan(text, ['T01'])
        self.assertTrue(warning['structural_ok'], warning)
        finding = next(row for row in warning['findings']
                       if row['code'] == 'HYGIENE_SECTION_MISSING')
        self.assertEqual(finding['severity'], 'warning')
        blocking = check_plan(text, ['T01'], hygiene_blocking=True)
        self.assertFalse(blocking['structural_ok'])
        finding = next(row for row in blocking['findings']
                       if row['code'] == 'HYGIENE_SECTION_MISSING')
        self.assertEqual(finding['severity'], 'blocking')

    def test_present_section_removes_the_finding(self):
        result = check_plan(plan(hygiene=COMPLETE_SECTION), ['T01'], hygiene_blocking=True)
        self.assertTrue(result['structural_ok'], result)
        self.assertNotIn('HYGIENE_SECTION_MISSING', {row['code'] for row in result['findings']})

    def test_large_size_blocks_through_the_cli(self):
        with tempfile.TemporaryDirectory(prefix='gsp-plan-hygiene-') as directory:
            root = Path(directory)

            def cli(*args):
                return subprocess.run(
                    [sys.executable, str(ROOT / 'scripts' / 'gsp.py'),
                     '--project', directory, *args],
                    capture_output=True, text=True, encoding='utf-8')

            self.assertEqual(cli('init').returncode, 0)
            self.assertEqual(cli('new', 'sample', '--title', 'Large task', '--size', 'large').returncode, 0)
            state = json.loads((root / '.gsp' / 'runs' / 'sample' / 'state.json').read_text(encoding='utf-8'))
            design = root / state['documents']['design']
            design.write_text('# Design\nLarge fixture design with enough prose.\n', encoding='utf-8')
            self.assertEqual(cli('approve', 'sample', '--quote', 'SYNTHETIC approved').returncode, 0)
            self.assertEqual(cli('task-add', 'sample', 'T01', '--title', 'Temp',
                                 '--acceptance', 'cleanup verified').returncode, 0)
            planned = root / state['documents']['plan']
            planned.write_text(plan(hygiene=None), encoding='utf-8')
            result = cli('plan-check', 'sample')
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertFalse(json.loads(result.stdout)['structural_ok'])
            planned.write_text(plan(hygiene=COMPLETE_SECTION), encoding='utf-8')
            result = cli('plan-check', 'sample')
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
