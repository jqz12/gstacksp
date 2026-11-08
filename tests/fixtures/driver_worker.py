"""Zero-cost process fixture. Never described as a real Codex/model test."""
import json
import os
from pathlib import Path
import time
import sys

sys.stdin.read()  # Behave like a real host consuming the supplied prompt.

job = json.loads(Path(os.environ['GSP_DRIVER_JOB']).read_text(encoding='utf-8'))
root = Path(job['project'])
mode_file = root / '.gsp/fixture-mode.txt'
mode = mode_file.read_text().strip() if mode_file.exists() else 'ok'
counter = root / '.gsp/fixture-counts.json'
counts = json.loads(counter.read_text()) if counter.exists() else {}
phase = job['phase']
counts[phase] = counts.get(phase, 0) + 1
counter.write_text(json.dumps(counts))
result = {'job_id': job['id'], 'outcome': 'completed', 'summary': 'Fixture verified local behavior',
          'question_kind': 'technical', 'question': '', 'recommendation': '', 'plan': None, 'findings': []}
if mode == 'slow':
    time.sleep(20)
if mode == 'invalid':
    Path(os.environ['GSP_DRIVER_RESULT']).write_text('invalid result', encoding='utf-8')
    raise SystemExit(0)
if mode == 'tamper':
    state_file = Path(job['workflow_state'])
    state = json.loads(state_file.read_text(encoding='utf-8'))
    state['mode_quote'] = 'fixture unexpected control change'
    state_file.write_text(json.dumps(state), encoding='utf-8')
elif phase == 'plan':
    result['plan'] = {'text': '# Fixture plan\nT1 implement add and test its actual result.\n',
                      'tasks': [{'id': 'T1', 'title': 'Add', 'acceptance': 'Two integers are added', 'depends': [], 'no_tests_reason': ''}]}
elif phase == 'review':
    if mode == 'review-fail-once' and counts[phase] == 1:
        result['findings'] = [{'blocking': True, 'text': 'Fixture asks for the explicit return annotation'}]
elif mode in ('question', 'technical') and counts[phase] == 1:
    result.update(outcome='needs_input', question_kind='dependency' if mode == 'question' else 'technical',
                  question='Which fixture implementation?', recommendation='Use the simple sum')
else:
    bad = mode == 'always-fail' or (mode == 'fail-once' and phase == 'execute' and counts[phase] == 1)
    (root / 'app.py').write_text('def add(a, b): return a - b\n' if bad else 'def add(a, b): return a + b\n', encoding='utf-8')
    (root / 'test_app.py').write_text('import unittest\nfrom app import add\nclass AppTests(unittest.TestCase):\n def test_add(self): self.assertEqual(add(2, 3), 5)\n', encoding='utf-8')
Path(os.environ['GSP_DRIVER_RESULT']).write_text(json.dumps(result), encoding='utf-8')
