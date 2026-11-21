import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SkillHandoffGenerationTests(unittest.TestCase):
    def test_primary_stages_route_to_artifact_checks(self):
        for name in ('brainstorming', 'writing-plans', 'execute', 'review', 'verify', 'ship'):
            with self.subTest(stage=name):
                text = (ROOT/'skills'/('gsp-'+name)/'SKILL.md').read_text(encoding='utf-8')
                self.assertIn('artifact-fidelity.md', text.split('<!-- sp-preserved-body:begin -->')[0])

    def test_asset_governance_rules_reach_generated_skills(self):
        governance = (ROOT/'skills/gsp-document-governance/SKILL.md').read_text(encoding='utf-8')
        health = (ROOT/'skills/gsp-health/SKILL.md').read_text(encoding='utf-8')
        self.assertIn('非Markdown', governance)
        self.assertIn('保留规则', governance)
        self.assertIn('asset_scan', health)
        self.assertIn('动态生成路径', health)

    def test_one_fixed_skill_report_format_for_stages_and_final(self):
        contract = (ROOT/'skills/gsp-workflow/references/output-handoff.md').read_text(encoding='utf-8')
        self.assertIn('## 本轮技能执行顺序', contract)
        self.assertIn('| 顺序 | 实际执行的技能 | 本次动作 | 结果/产物/证据 |', contract)
        self.assertIn('阶段总结与最终交付使用同一标题和四列', contract)
        self.assertNotIn('格式按任务规模精简', contract)
        self.assertNotIn('结果很小时可用一段话或短列表', contract)
        ship = (ROOT/'skills/gsp-ship/SKILL.md').read_text(encoding='utf-8').split('<!-- sp-preserved-body:begin -->')[0]
        self.assertIn('本轮技能执行顺序', ship)

    def test_final_primary_skills_keep_lifecycle_adaptations(self):
        spec = importlib.util.spec_from_file_location('skill_definitions', ROOT/'scripts/build_skills.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        names = ('brainstorming', 'writing-plans', 'execute', 'systematic-debugging', 'review', 'verify', 'ship')
        for name in names:
            with self.subTest(name=name):
                text = (ROOT/'skills'/('gsp-'+name)/'SKILL.md').read_text(encoding='utf-8')
                adaptation = text.split('<!-- sp-preserved-body:begin -->')[0]
                self.assertIn(module.LIFECYCLE_NOTES[name], adaptation)

    def test_every_final_skill_routes_to_shared_handoff_contract(self):
        import json
        catalog = json.loads((ROOT/'catalog.json').read_text(encoding='utf-8'))
        for item in catalog['skills']:
            with self.subTest(name=item['name']):
                text = (ROOT/'skills'/item['name']/'SKILL.md').read_text(encoding='utf-8')
                self.assertIn('output-handoff.md', text.split('<!-- sp-preserved-body:begin -->')[0])

    def test_git_topology_handoff_reaches_all_affected_skills(self):
        required = (
            'topology', 'topology-select', 'main/master', 'worktree',
            'GSP 编号', 'workspace_root', 'control_root', 'Issue/PR',
        )
        for name in ('git', 'workflow', 'execute', 'captain', 'ship'):
            with self.subTest(name=name):
                text = (ROOT / 'skills' / ('gsp-' + name) / 'SKILL.md').read_text(encoding='utf-8')
                for phrase in required:
                    self.assertIn(phrase, text, f'{name} missing {phrase}')

    def test_public_git_topology_docs_name_the_identity_layers(self):
        for path in (ROOT / 'docs/安装与使用.md', ROOT / 'docs/技能目录.md'):
            text = path.read_text(encoding='utf-8')
            self.assertIn('GSP 编号', text)
            self.assertIn('worktree', text)

    def test_cross_session_contract_reaches_generated_sources_and_skills(self):
        source = (ROOT / 'scripts/build_skills.py').read_text(encoding='utf-8')
        self.assertIn('CROSS_SESSION_GUIDANCE', source)
        self.assertIn('主会话维护', source)
        self.assertIn('SESSION_PATH_NOT_CLAIMED', (ROOT / 'skills/gsp-execute/SKILL.md').read_text(encoding='utf-8'))
        self.assertIn('唯一总体计划', (ROOT / 'skills/gsp-writing-plans/SKILL.md').read_text(encoding='utf-8'))
        self.assertIn('sessions/history', (ROOT / 'skills/gsp-context-restore/SKILL.md').read_text(encoding='utf-8'))

    def test_cross_session_public_document_is_indexed(self):
        index = (ROOT / 'docs/README.md').read_text(encoding='utf-8')
        root_readme = (ROOT / 'README.md').read_text(encoding='utf-8')
        reference = ROOT / 'docs/reference/cross-session-development.md'
        self.assertTrue(reference.is_file())
        self.assertIn('reference/cross-session-development.md', index)
        self.assertIn('docs/reference/cross-session-development.md', root_readme)
        self.assertIn('主会话维护', reference.read_text(encoding='utf-8'))

    def test_cross_session_worktree_lifecycle_guidance_is_consistent(self):
        source = (ROOT / 'scripts/build_skills.py').read_text(encoding='utf-8')
        workflow = (ROOT / 'skills/gsp-execute/SKILL.md').read_text(encoding='utf-8')
        contract = (ROOT / 'skills/gsp-workflow/references/contract.md').read_text(encoding='utf-8')
        reference = (ROOT / 'docs/reference/cross-session-development.md').read_text(encoding='utf-8')
        for text in (source, workflow, contract, reference):
            with self.subTest(document='cross-session lifecycle guidance'):
                self.assertIn('session-accept', text)
                self.assertIn('cleanup-pending', text)
                self.assertIn('managed worktree', text)
        self.assertIn('session-clean --retry', reference)

    def test_long_command_observability_reaches_generated_execute_skill(self):
        source = (ROOT / 'scripts/build_skills.py').read_text(encoding='utf-8')
        execute = (ROOT / 'skills/gsp-execute/SKILL.md').read_text(encoding='utf-8')
        contract = (ROOT / 'skills/gsp-workflow/references/contract.md').read_text(encoding='utf-8')
        hook_reference = (ROOT / 'skills/gsp-workflow/references/codex-hooks.md').read_text(encoding='utf-8')
        user_reference = (ROOT / 'docs/Codex全局hooks.md').read_text(encoding='utf-8')
        for text in (source, execute):
            with self.subTest(document='execute guidance'):
                self.assertIn('write_stdin', text)
                self.assertIn('两次', text)
                self.assertIn('日志', text)
                self.assertIn('进程', text)
                self.assertIn('退出码', text)
                self.assertIn('产物', text)
        self.assertIn('wait-guard', hook_reference)
        self.assertIn('NOT_CHECKED', hook_reference)
        self.assertIn('目标路径', contract)
        self.assertIn('wait-guard', user_reference)
