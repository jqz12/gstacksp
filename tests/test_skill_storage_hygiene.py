"""Generated skills and references carry the storage-hygiene contract."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class SkillStorageHygieneTests(unittest.TestCase):
    def text(self, relative):
        path = ROOT / relative
        self.assertTrue(path.is_file(), relative)
        return path.read_text(encoding='utf-8')

    def test_plan_skill_requires_lifecycle_section(self):
        text = self.text('skills/gsp-writing-plans/SKILL.md')
        for needle in ('产物与临时文件生命周期', 'HYGIENE_SECTION_MISSING', '反模式'):
            self.assertIn(needle, text)

    def test_execute_skill_treats_residue_as_defect(self):
        text = self.text('skills/gsp-execute/SKILL.md')
        for needle in ('残留即缺陷', 'CARGO_TARGET_DIR', 'topology-clean'):
            self.assertIn(needle, text)

    def test_verify_skill_blocks_unexplained_leftovers(self):
        text = self.text('skills/gsp-verify/SKILL.md')
        for needle in ('HYGIENE_LEFTOVER', 'git_clean', 'branch_merged'):
            self.assertIn(needle, text)

    def test_ship_skill_orders_topology_cleanup(self):
        text = self.text('skills/gsp-ship/SKILL.md')
        for needle in ('close <id> --cleanup', 'topology-clean', '--allow-dirty', '--reconcile'):
            self.assertIn(needle, text)

    def test_manual_contract_and_workflow_reference_the_same_rules(self):
        contract = self.text('skills/gsp-workflow/references/contract.md')
        routing = self.text('skills/gsp-workflow/references/routing.md')
        entry = self.text('skills/gsp-workflow/SKILL.md')
        self.assertIn('产物与临时文件生命周期', contract)
        self.assertIn('HYGIENE_LEFTOVER', contract)
        self.assertIn('topology-clean', contract)
        self.assertIn('HYGIENE_SECTION_MISSING', routing)
        self.assertIn('构建与临时产物', entry)

    def test_public_docs_entry_is_present(self):
        docs = self.text('docs/README.md')
        self.assertIn('产物与临时文件生命周期', docs)
        self.assertIn('topology-clean', docs)


if __name__ == '__main__':
    unittest.main()
