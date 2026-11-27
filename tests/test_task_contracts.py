import copy
import unittest

from gsp.task_contracts import (
    compile_task_contract,
    render_task_contract,
    verify_task_contract,
    write_conflicts,
)


class TaskContractTests(unittest.TestCase):
    def setUp(self):
        self.state = {'project_id': 'project', 'work_uuid': 'work', 'tasks': [
            {'id': 'T01', 'title': 'One', 'acceptance': 'works', 'depends': []},
            {'id': 'T02', 'title': 'Two', 'acceptance': 'works too', 'depends': []}]}
        self.plan = ('# Plan\n## Global Constraints\nKeep all quality gates.\n'
                     '## Task T01: One\nComplete first task body.\n'
                     '**Writes:** {"paths":["src/one.py"]}\n'
                     '## Task T02: Two\nUnrelated second task.\n'
                     '**Writes:** {"paths":["src/two.py"]}\n')

    def test_complete_task_and_global_constraints_without_other_task(self):
        c = compile_task_contract(self.state, 'T01', self.plan)
        self.assertIn('Complete first task body.', c['task_body'])
        self.assertIn('Keep all quality gates.', c['global_constraints'])
        self.assertNotIn('Unrelated second task.', c['task_body'] + c['global_constraints'])
        self.assertEqual(c['writes'], ['src/one.py'])

    def test_unrelated_task_change_keeps_contract_but_own_change_invalidates(self):
        c = compile_task_contract(self.state, 'T01', self.plan)
        verify_task_contract(c, self.state, self.plan.replace('Unrelated second', 'Changed second'))
        with self.assertRaisesRegex(ValueError, 'TASK_CONTRACT_STALE'):
            verify_task_contract(c, self.state, self.plan.replace('Complete first', 'Changed first'))

    def test_tampered_contract_rejected(self):
        c = compile_task_contract(self.state, 'T01', self.plan)
        c['task_body'] = 'silently weakened'
        with self.assertRaisesRegex(ValueError, 'TASK_CONTRACT_TAMPERED'):
            verify_task_contract(c, self.state, self.plan)

    def test_global_quality_sections_after_tasks_apply_to_every_task(self):
        plan = self.plan + '\n## Final verification\nRun all required checks.\n'
        for task in ('T01', 'T02'):
            c = compile_task_contract(self.state, task, plan)
            self.assertIn('Run all required checks.', c['global_constraints'])

    def test_windows_path_overlap_requires_dependency_order(self):
        plan = self.plan.replace('src/one.py', 'SRC\\\\shared').replace('src/two.py', 'src/shared/file.py')
        self.assertEqual(len(write_conflicts(self.state, plan)), 1)
        state = copy.deepcopy(self.state)
        state['tasks'][1]['depends'] = ['T01']
        self.assertEqual(write_conflicts(state, plan), [])

    def test_escape_and_ambiguous_task_rejected(self):
        with self.assertRaisesRegex(ValueError, 'WRITE_PATH_INVALID'):
            compile_task_contract(self.state, 'T01', self.plan.replace('src/one.py', '../escape'))
        with self.assertRaisesRegex(ValueError, 'TASK_CONTRACT_AMBIGUOUS'):
            compile_task_contract(self.state, 'T01', self.plan + '\n## Task T01: Again\nwrong\n')

    def test_legacy_plan_gets_explicit_task_acceptance(self):
        c = compile_task_contract(self.state, 'T01', '# Old short plan\nKeep compatibility.')
        self.assertIn('works', c['task_body'])
        self.assertTrue(c['legacy_plan'])

    def test_source_references_and_consumed_content_use_separate_digests(self):
        paths = {
            'design': 'docs/work/2026/11/GSP-000034/design.md',
            'plan': 'docs/work/2026/11/GSP-000034/plan.md',
        }
        contract = compile_task_contract(
            self.state, 'T01', self.plan, 'Approved design',
            source_paths=paths,
        )
        self.assertEqual(contract['source_refs']['design']['path'], paths['design'])
        self.assertEqual(len(contract['source_refs']['plan']['digest']), 64)
        self.assertNotEqual(contract['contract_uid'], contract['content_digest'])
        without_sources = compile_task_contract(self.state, 'T01', self.plan)
        self.assertNotEqual(contract['content_digest'], without_sources['content_digest'])

        unrelated = verify_task_contract(
            contract, self.state,
            self.plan.replace('Unrelated second', 'Changed second'),
            'Approved design', source_paths=paths,
        )
        self.assertEqual(
            unrelated['content_digest'], contract['content_digest']
        )
        self.assertNotEqual(
            unrelated['contract_uid'], contract['contract_uid']
        )

        global_change = self.plan.replace(
            'Keep all quality gates.', 'Remove every quality gate.'
        )
        with self.assertRaisesRegex(ValueError, 'TASK_CONTRACT_STALE'):
            verify_task_contract(
                contract, self.state, global_change,
                'Approved design', source_paths=paths,
            )

        changed_dependency_state = copy.deepcopy(self.state)
        changed_dependency_state['tasks'][0]['depends'] = ['T02']
        with self.assertRaisesRegex(ValueError, 'TASK_CONTRACT_STALE'):
            verify_task_contract(
                contract, changed_dependency_state, self.plan,
                'Approved design', source_paths=paths,
            )

        with self.assertRaisesRegex(ValueError, 'TASK_CONTRACT_STALE'):
            verify_task_contract(
                contract, self.state, self.plan,
                'Changed approved design', source_paths=paths,
            )

        tampered = copy.deepcopy(contract)
        tampered['source_refs']['plan']['digest'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'TASK_CONTRACT_TAMPERED'):
            verify_task_contract(
                tampered, self.state, self.plan,
                'Approved design', source_paths=paths,
            )

    def test_fenced_writes_example_does_not_override_real_declaration(self):
        plan = self.plan.replace(
            '**Writes:** {"paths":["src/one.py"]}',
            '```markdown\n'
            '**Writes:** {"paths":["src/fenced-example.py"]}\n'
            '```\n'
            '**Writes:** {"paths":["src/one.py"]}',
        )
        contract = compile_task_contract(self.state, 'T01', plan)
        self.assertEqual(contract['writes'], ['src/one.py'])

    def test_rendered_contract_contains_full_unfiltered_specification(self):
        state = copy.deepcopy(self.state)
        state['id'] = 'work'
        task = state['tasks'][0]
        specification = (
            '## Mandatory interfaces\n'
            'Preserve complete public contract and error ordering.\n'
        )
        contract = compile_task_contract(
            state, 'T01', self.plan, specification,
        )
        rendered = render_task_contract(contract, state, task, {
            'session_hint': 'S01',
            'platform': 'manual',
            'dispatch_mode': 'manual',
            'claimed_paths': ['src/one.py'],
            'model_alias': 'group/deepseek-2',
            'effort': 'max',
        })
        self.assertIn('## 完整适用规格', rendered)
        self.assertIn(
            'Preserve complete public contract and error ordering.',
            rendered,
        )

    def test_single_rendered_contract_contains_complete_dependencies(self):
        state = copy.deepcopy(self.state)
        state['id'] = 'work'
        state['tasks'][1]['depends'] = ['T01']
        task = state['tasks'][1]
        contract = compile_task_contract(state, 'T02', self.plan)
        rendered = render_task_contract(contract, state, task, {
            'session_hint': 'S02',
            'platform': 'manual',
            'dispatch_mode': 'manual',
            'claimed_paths': ['src/two.py'],
            'model_alias': 'group/deepseek-2',
            'effort': 'max',
        })
        self.assertIn('"T01"', rendered)
        self.assertIn('"acceptance": "works"', rendered)
        self.assertIn('Unrelated second task.', rendered)
        self.assertIn('本文件同时是完整任务契约', rendered)
