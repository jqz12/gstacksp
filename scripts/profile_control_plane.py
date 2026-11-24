#!/usr/bin/env python
"""Measure control-plane cost from a chosen source root.

The fixture is synthetic and isolated.  The script loads ``gsp`` and the test
helpers from ``--source-root`` in this process, so paired before/after samples
cannot accidentally measure the checkout that merely houses this script.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
from unittest.mock import patch


def _sha_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _load(source_root):
    source = Path(source_root).resolve()
    sys.path.insert(0, str(source))
    for name in list(sys.modules):
        if name == 'gsp' or name.startswith('gsp.'):
            del sys.modules[name]
    import gsp.runtime as runtime
    from tests.test_topology_lifecycle import TopologyLifecycleTests
    runtime_file = Path(runtime.__file__).resolve()
    if runtime_file.parent != source / 'gsp':
        raise SystemExit(f'loaded gsp from {runtime_file}, expected under {source / "gsp"}')
    return source, runtime, TopologyLifecycleTests


def _prepare(fixture, runtime, task_count):
    fixture.setUp()
    fixture.setup_git()
    fixture.prepare(intent='single-task' if task_count == 1 else 'full-stack')
    if task_count == 1:
        fixture.cmd('topology-select', 'work', '--mode', 'worktree',
                    '--quote', 'SYNTHETIC profile fixture')
    else:
        integration = f'T{task_count:02}'
        fixture.cmd('task-add', 'work', integration, '--title', 'Synthetic integration',
                    '--role', 'integration', '--acceptance', 'Fixture passes',
                    '--depends', 'T01')
        for index in range(2, task_count):
            fixture.cmd('task-add', 'work', f'T{index:02}', '--title', f'Synthetic child {index}',
                        '--role', 'backend', '--owned-by', integration,
                        '--acceptance', 'Fixture passes')
        fixture.cmd('seal-plan', 'work')
        fixture.cmd('topology-select', 'work', '--mode', 'in-place',
                    '--quote', 'SYNTHETIC control root')
        for index in range(1, task_count + 1):
            fixture.cmd('topology-select', 'work', '--task', f'T{index:02}',
                        '--mode', 'worktree', '--quote', 'SYNTHETIC isolated child')
    fixture.cmd('advance', 'work', '--to', 'execute')
    for index in range(1, task_count + 1):
        task = f'T{index:02}'
        fixture.cmd('task-start', 'work', task)
        fixture.check(task)
        fixture.cmd('task-done', 'work', task)
    fixture.check('all')
    fixture.cmd('advance', 'work', '--to', 'review')
    document = fixture.project.paths('work')[1]
    (document / 'review.md').write_text(
        '# Review\nSynthetic profile fixture reviewed without blocking findings.\n',
        encoding='utf-8')
    fixture.cmd('review', 'work', '--reviewer', 'self', '--blocking', '0')
    fixture.cmd('advance', 'work', '--to', 'checking')


def _invoke(fixture, runtime, argv):
    calls = []
    scans = []
    functions = {}
    cache = {}
    real_run = subprocess.run

    def counting_run(*args, **kwargs):
        command = args[0] if args else kwargs.get('args')
        is_git = (isinstance(command, (list, tuple)) and command and
                  Path(str(command[0])).stem.lower() == 'git')
        started = time.perf_counter()
        try:
            return real_run(*args, **kwargs)
        finally:
            if is_git:
                calls.append({'argv': [str(value) for value in command],
                              'seconds': time.perf_counter() - started})

    def named(name, real):
        def wrapper(*args, **kwargs):
            functions[name] = functions.get(name, 0) + 1
            return real(*args, **kwargs)
        return wrapper

    def counting_scan(*args, **kwargs):
        scans.append(1)
        return real_scan(*args, **kwargs)

    real_scan = runtime.snapshot
    real_get = (runtime.ValidationContext.get
                if hasattr(runtime, 'ValidationContext') else None)

    def counting_get(self, namespace, key, loader):
        token = (namespace, key)
        bucket = cache.setdefault(namespace, {'hits': 0, 'misses': 0})
        bucket['hits' if token in self._cache else 'misses'] += 1
        return real_get(self, namespace, key, loader)

    started = time.perf_counter()
    outcome = 'ok'
    patches = [
        patch('subprocess.run', counting_run),
        patch.object(runtime, 'snapshot', counting_scan),
        patch.object(runtime, 'fingerprint', named('fingerprint', runtime.fingerprint)),
        patch.object(runtime, 'digest', named('digest', runtime.digest)),
        patch.object(runtime, 'contract_hash', named('contract_hash', runtime.contract_hash)),
        patch.object(runtime, 'evidence_for', named('evidence_for', runtime.evidence_for)),
        patch.object(runtime, 'validate_delivery',
                     named('validate_delivery', runtime.validate_delivery)),
        patch.object(runtime.Project, 'state', named('state', runtime.Project.state)),
    ]
    if real_get is not None:
        patches.append(patch.object(runtime.ValidationContext, 'get', counting_get))
    try:
        for item in patches:
            item.start()
        try:
            args = runtime.parser().parse_args(list(argv))
            result, ok = runtime.dispatch(fixture.project, args)
            if isinstance(result, dict) and result.get('status'):
                outcome = str(result['status'])
            elif not ok:
                outcome = 'blocked'
        except runtime.WorkflowError as exc:
            outcome = str(exc)
        finally:
            for item in reversed(patches):
                item.stop()
    finally:
        elapsed = time.perf_counter() - started
    return {
        'argv': list(argv),
        'elapsed_s': elapsed,
        'outcome': outcome,
        'git_calls': len(calls),
        'git_s': sum(row['seconds'] for row in calls),
        'git_trace': calls,
        'scans': len(scans),
        'functions': functions,
        'cache': cache,
    }


def _supports_brief(runtime):
    parser = runtime.parser()
    for action in parser._actions:
        if getattr(action, 'choices', None) and 'status' in action.choices:
            return any('--brief' in option
                       for child in action.choices['status']._actions
                       for option in child.option_strings)
    return False


def _record(label, fixture, runtime, argv, repetitions):
    rows = []
    for index in range(repetitions):
        sample = _invoke(fixture, runtime, argv)
        sample['label'] = label
        sample['sample'] = index + 1
        rows.append(sample)
    return rows


def _summary(rows):
    result = {}
    labels = sorted({row['label'] for row in rows})
    for label in labels:
        samples = [row for row in rows if row['label'] == label]
        elapsed = [row['elapsed_s'] for row in samples]
        subsequent = elapsed[1:] if len(elapsed) > 1 else elapsed
        result[label] = {
            'samples': len(samples),
            'first_s': elapsed[0],
            'median_all_s': statistics.median(elapsed),
            'median_subsequent_s': statistics.median(subsequent),
            'min_s': min(elapsed),
            'max_s': max(elapsed),
            'git_calls_first': samples[0]['git_calls'],
            'git_calls_median': statistics.median(row['git_calls'] for row in samples),
            'scans_median': statistics.median(row['scans'] for row in samples),
            'outcomes': sorted({row['outcome'] for row in samples}),
        }
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', required=True)
    parser.add_argument('--repetitions', type=int, default=5)
    parser.add_argument('--output', required=True)
    parser.add_argument('--task-count', type=int, default=5)
    args = parser.parse_args(argv)
    if args.repetitions < 1:
        raise SystemExit('--repetitions must be positive')
    if args.task_count < 1:
        raise SystemExit('--task-count must be positive')
    source, runtime, fixture_class = _load(args.source_root)
    head = subprocess.run(['git', '-C', str(source), 'rev-parse', 'HEAD'],
                          capture_output=True, text=True, encoding='utf-8')
    branch = subprocess.run(['git', '-C', str(source), 'branch', '--show-current'],
                            capture_output=True, text=True, encoding='utf-8')
    fixture = fixture_class(methodName='runTest')
    rows = []
    fixture_meta = {}
    try:
        _prepare(fixture, runtime, args.task_count)
        state = fixture.state()
        fixture_meta = {
            'task_count': args.task_count,
            'status': state.get('status'),
            'bound_workspaces': 1 + len([task for task in state.get('tasks') or []
                                        if (task.get('topology') or {}).get('workspace_root')]),
        }
        print(f'profiling {source} ({args.task_count} tasks)', file=sys.stderr, flush=True)
        rows += _record('advance-ready', fixture, runtime,
                        ['advance', 'work', '--to', 'ready'], 1)
        rows += _record('status-ready', fixture, runtime,
                        ['status', 'work'], args.repetitions)
        rows += _record('resume-ready', fixture, runtime,
                        ['resume', 'work'], args.repetitions)
        rows += _record('ready-command', fixture, runtime,
                        ['ready', 'work'], args.repetitions)
        if _supports_brief(runtime):
            rows += _record('status-brief', fixture, runtime,
                            ['status', 'work', '--brief'], 1)
        rows += _record('close-missing-condition', fixture, runtime,
                        ['close', 'work', '--reason', 'SYNTHETIC diagnostic only'], 1)
    finally:
        fixture.doCleanups()
    output = {
        'schema': 1,
        'synthetic': True,
        'source_root': str(source),
        'runtime_file': str(Path(runtime.__file__).resolve()),
        'runtime_sha256': _sha_file(Path(runtime.__file__).resolve()),
        'head': head.stdout.strip() if head.returncode == 0 else None,
        'branch': branch.stdout.strip() if branch.returncode == 0 else None,
        'host': {'platform': sys.platform, 'python': sys.version.split()[0],
                 'cwd': os.getcwd()},
        'repetitions': args.repetitions,
        'fixture': fixture_meta,
        'summary': _summary(rows),
        'samples': rows,
    }
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n',
                      encoding='utf-8')
    print(json.dumps(output['summary'], ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
