#!/usr/bin/env python
"""Run the unittest modules in deterministic shards with one merged summary.

This is an execution wrapper only: it does not change test semantics, does
not touch the repository, and keeps the serial ``unittest discover`` entry as
the baseline.  Output is shaped so that ``gsp.runtime.test_count`` sees a
single ``Ran N tests`` line.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / 'tests'
TIERS = TESTS / 'tiers.json'

MODULE_NAME = re.compile(r'^test_[A-Za-z0-9_]+$')
RAN_LINE = re.compile(r'^Ran\s+(\d+)\s+tests?\s+in\s+([0-9.]+)s\s*$', re.MULTILINE)
RESULT_LINE = re.compile(r'^(OK|FAILED)(?:\s*\(([^)]*)\))?\s*$', re.MULTILINE)
STAT = re.compile(r'\b(failures|errors|skipped)=(\d+)')
TEST_DEF = re.compile(r'^\s*def test_', re.MULTILINE)


def discover_modules(root: Path, tier: str) -> list[str]:
    tests = root / 'tests'
    if tier == 'full':
        modules = sorted(path.stem for path in tests.glob('test_*.py') if path.is_file())
    elif tier == 'fast':
        tiers = tests / 'tiers.json'
        if not tiers.is_file():
            raise SystemExit(f'missing tier definition: {tiers}')
        try:
            data = json.loads(tiers.read_text(encoding='utf-8'))
        except ValueError as exc:
            raise SystemExit(f'invalid tier definition: {exc}')
        modules = list(data.get('fast') or [])
        if not modules:
            raise SystemExit('fast tier is empty')
    else:
        raise SystemExit(f'unknown tier: {tier}')
    modules = sorted(set(modules))
    missing = [name for name in modules if not MODULE_NAME.match(name) or not (tests / f'{name}.py').is_file()]
    if missing:
        raise SystemExit('unknown or missing test modules: ' + ', '.join(missing))
    return modules


def load_tiers(root: Path) -> dict:
    tiers = root / 'tests/tiers.json'
    if not tiers.is_file():
        return {}
    try:
        data = json.loads(tiers.read_text(encoding='utf-8'))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def serial_modules(root: Path, tier: str) -> list[str]:
    """Modules that must not compete with other shards (OS-global lock/PID state)."""
    if tier != 'full':
        return []
    return [str(name) for name in load_tiers(root).get('serial') or []]


def module_weight(tests: Path, name: str) -> int:
    text = (tests / f'{name}.py').read_text(encoding='utf-8', errors='replace')
    return len(TEST_DEF.findall(text))


def pack_shards(tests: Path, modules: list[str], jobs: int,
                weights: dict | None = None) -> list[list[str]]:
    """Greedy balance by observed seconds (else test count), deterministic."""
    weights = weights or {}
    measured = {}
    for name in modules:
        value = weights.get(name)
        measured[name] = float(value) if isinstance(value, (int, float)) else module_weight(tests, name)
    bins: list[dict] = [{'modules': [], 'weight': 0} for _ in range(jobs)]
    for name in sorted(modules, key=lambda item: (-measured[item], item)):
        target = min(range(jobs),
                     key=lambda index: (bins[index]['weight'], len(bins[index]['modules']), index))
        bins[target]['modules'].append(name)
        bins[target]['weight'] += measured[name]
    return [bucket['modules'] for bucket in bins if bucket['modules']]


def parse_summary(text: str) -> dict:
    """Parse one shard's unittest output into counts; crash when no Ran line."""
    ran = RAN_LINE.findall(text)
    result = RESULT_LINE.findall(text)
    counts = {'ran': 0, 'failures': 0, 'errors': 0, 'skipped': 0}
    if not ran:
        counts['errors'] = 1
        return counts
    counts['ran'] = int(ran[-1][0])
    if result:
        verdict, detail = result[-1]
        stats = {key: int(value) for key, value in STAT.findall(detail or '')}
        counts['skipped'] = stats.get('skipped', 0)
        counts['failures'] = stats.get('failures', 0) if verdict == 'FAILED' else 0
        counts['errors'] = stats.get('errors', 0) if verdict == 'FAILED' else 0
    else:
        counts['errors'] = 1
    return counts


def merge_summaries(texts: list[str]) -> dict:
    merged = {'ran': 0, 'failures': 0, 'errors': 0, 'skipped': 0}
    for text in texts:
        part = parse_summary(text)
        for key in merged:
            merged[key] += part[key]
    return merged


def _strip_summary(text: str) -> str:
    """Drop the trailing unittest summary so only the merged one is printed."""
    matches = list(RAN_LINE.finditer(text))
    if not matches:
        return text
    start = text.rfind('\n', 0, matches[-1].start())
    return text[:start] if start >= 0 else ''


def _child_env(root: Path) -> dict:
    env = dict(os.environ)
    parts = [str(root), str(root / 'tests')]
    if env.get('PYTHONPATH'):
        parts.append(env['PYTHONPATH'])
    env['PYTHONPATH'] = os.pathsep.join(parts)
    return env


def _run_shard(root: Path, modules: list[str]) -> tuple[str, int]:
    argv = [sys.executable, '-m', 'unittest', '-v', *[f'tests.{name}' for name in modules]]
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    try:
        result = subprocess.run(argv, cwd=str(root), env=_child_env(root), capture_output=True,
                                text=True, encoding='utf-8', errors='replace', creationflags=flags)
    except (OSError, subprocess.SubprocessError) as exc:
        return f'launch failed: {type(exc).__name__}: {exc}', 1
    return (result.stdout or '') + (result.stderr or ''), result.returncode


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    root = Path(root) if root is not None else ROOT
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs', type=int, default=None,
                        help='shard count (default min(4, cpu)); capped at cpu count')
    parser.add_argument('--tier', choices=['full', 'fast'], default='full')
    parser.add_argument('--list', action='store_true', help='print selected modules and exit')
    args = parser.parse_args(argv)

    cpu = os.cpu_count() or 1
    jobs = min(4, cpu) if args.jobs is None else args.jobs
    if jobs < 1:
        parser.error('--jobs must be >= 1')
    jobs = min(jobs, cpu)

    modules = discover_modules(root, args.tier)
    if args.list:
        for name in modules:
            print(name)
        return 0

    started = time.monotonic()
    runs: list[tuple[list[str], str, int]] = []
    isolated = set(serial_modules(root, args.tier)) if jobs > 1 else set()
    serial_here = [name for name in modules if name in isolated]
    parallel_modules = [name for name in modules if name not in isolated]
    if serial_here:
        text, code = _run_shard(root, serial_here)
        runs.append((serial_here, text, code))
    shards = ([modules] if jobs == 1
              else pack_shards(root / 'tests', parallel_modules, jobs,
                               load_tiers(root).get('weights')))
    outputs: list[tuple[list[str], str, int]] = [None] * len(shards)
    with ThreadPoolExecutor(max_workers=max(1, len(shards))) as pool:
        futures = {pool.submit(_run_shard, root, shard): index
                   for index, shard in enumerate(shards)}
        for future in as_completed(futures):
            index = futures[future]
            text, code = future.result()
            outputs[index] = (shards[index], text, code)
    runs.extend(outputs)
    texts = [text for _, text, _ in runs]
    failures = 0
    for index, (shard, text, code) in enumerate(runs, 1):
        summary = parse_summary(text)
        bad = code != 0 or summary['failures'] or summary['errors'] or not summary['ran']
        if bad:
            failures += 1
            heading = f'===== shard {index}/{len(runs)} failed: {" ".join(shard)} ====='
            print(heading, file=sys.stderr)
            print(_strip_summary(text).rstrip(), file=sys.stderr)
    elapsed = time.monotonic() - started

    merged = merge_summaries(texts)
    if failures or merged['failures'] or merged['errors']:
        detail = [f'failures={merged["failures"]}', f'errors={merged["errors"]}']
        if merged['skipped']:
            detail.append(f'skipped={merged["skipped"]}')
        print(f'Ran {merged["ran"]} tests in {elapsed:.1f}s')
        print('FAILED (' + ', '.join(detail) + ')')
        return 1
    if not merged['ran']:
        print('Ran 0 tests in %.1fs' % elapsed)
        print('FAILED (errors=1)')
        return 1
    suffix = f' (skipped={merged["skipped"]})' if merged['skipped'] else ''
    print(f'Ran {merged["ran"]} tests in {elapsed:.1f}s')
    print('OK' + suffix)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
