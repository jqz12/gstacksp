import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

class PreservationTests(unittest.TestCase):
    def test_complete_baseline_and_generated_sources(self):
        spec = importlib.util.spec_from_file_location('sp_preservation', ROOT/'scripts/sp_preservation.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.verify(ROOT)
        self.assertEqual(result['upstream_skills'], 15)
        self.assertGreaterEqual(result['source_files'], 82)
        self.assertEqual(result['shortened_bodies'], 0)

    def test_full_tdd_method_and_dependencies_are_installed(self):
        text = (ROOT/'skills/gsp-test-driven-development/SKILL.md').read_text(encoding='utf-8')
        self.assertIn('## The Iron Law', text)
        self.assertIn('## Red-Green-Refactor', text)
        self.assertTrue((ROOT/'skills/gsp-workflow/upstream/skills/test-driven-development/writing-good-tests.md').is_file())
        self.assertTrue((ROOT/'skills/gsp-workflow/upstream/skills/subagent-driven-development/references/fix-loop.md').is_file())
        # Originals are readable resources, never extra discoverable skill entries.
        self.assertEqual(list((ROOT/'skills/gsp-workflow/upstream').rglob('SKILL.md')), [])

    def test_workflow_platform_references_resolve_from_generated_skill(self):
        text = (ROOT/'skills/gsp-workflow/SKILL.md').read_text(encoding='utf-8')
        for name in ('claude-code-tools.md', 'codex-tools.md', 'pi-tools.md',
                     'antigravity-tools.md', 'hermes-tools.md', 'muse-tools.md'):
            marker = f'`upstream/skills/using-superpowers/references/{name}`'
            self.assertIn(marker, text)
            self.assertTrue((ROOT/'skills/gsp-workflow/upstream/skills/using-superpowers/references'/name).is_file())
        self.assertNotIn('`references/claude-code-tools.md`', text)

    def test_generation_is_idempotent_and_drift_is_detected(self):
        import shutil
        import tempfile
        spec=importlib.util.spec_from_file_location('sp_preservation',ROOT/'scripts/sp_preservation.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix='gsp-sp-snapshot-') as folder:
            temp=Path(folder)
            shutil.copytree(ROOT/'sources',temp/'sources')
            shutil.copytree(ROOT/'skills',temp/'skills')
            before={p.relative_to(temp).as_posix():p.read_bytes() for p in (temp/'skills').rglob('*') if p.is_file()}
            module.build(temp)
            after={p.relative_to(temp).as_posix():p.read_bytes() for p in (temp/'skills').rglob('*') if p.is_file()}
            self.assertEqual(before,after)
            resource=temp/'skills/gsp-workflow/upstream/skills/test-driven-development/writing-good-tests.md'
            resource.write_text('accidental truncation',encoding='utf-8')
            with self.assertRaises(AssertionError):module.verify(temp)

    def test_headless_worker_receives_full_stage_method(self):
        import sys
        sys.path.insert(0,str(ROOT))
        from gsp.hosts import prompt
        self.assertIn('gsp-writing-plans/SKILL.md',prompt({'phase':'plan'}).replace('\\','/'))
        self.assertIn('gsp-review/SKILL.md',prompt({'phase':'review'}).replace('\\','/'))
        self.assertIn('COMPLETE',prompt({'phase':'execute'}))

    def test_installed_originals_keep_exact_baseline_bytes(self):
        import tempfile
        import hashlib
        import sys
        sys.path.insert(0,str(ROOT))
        from gsp.user_install import UserInstall
        with tempfile.TemporaryDirectory(prefix='gsp-sp-install-') as folder:
            profile=Path(folder)
            result=UserInstall('codex',profile).apply('install')
            self.assertGreater(result['files'],80)
            baseline=json.loads((ROOT/'sources/superpowers/baseline.json').read_text(encoding='utf-8'))
            for rel,expected in baseline['files'].items():
                resource=Path(rel)
                if resource.name=='SKILL.md':resource=resource.with_name('ORIGINAL.md')
                data=(profile/'skills/gsp-workflow/upstream'/resource).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(),expected,rel)
