"""Incremental, reviewable architecture facts for existing and new projects.

Facts are not a waiver of tests or existing required quality checks. Historical
violations retain their identity; only newly introduced violations block the
change comparison. Unsupported languages and unresolved imports are explicit.
"""
import ast
import hashlib
import json
from pathlib import Path

RULE_VERSION = 1
IGNORED = frozenset({'.git', '.gsp', '.venv', 'venv', 'node_modules', 'target',
                     '__pycache__', 'dist', 'build'})


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def _path(value):
    text = str(value).replace('\\', '/').removeprefix('./')
    if text.startswith('/') or ':' in text or '..' in text.split('/'):
        raise ValueError('ARCHITECTURE_PATH_ESCAPE ' + text)
    return text


def _module(path):
    parts = list(Path(path).with_suffix('').parts)
    if parts[-1:] == ['__init__']:
        parts.pop()
    return '.'.join(parts)


def _imports(path, text):
    module = _module(path)
    package = module if path.endswith('/__init__.py') else module.rpartition('.')[0]
    imports = set()
    for node in ast.walk(ast.parse(text, filename=path)):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ''
            if node.level:
                parts = package.split('.') if package else []
                if node.level > len(parts) + 1:
                    raise ValueError('relative import escapes module root')
                prefix = '.'.join(parts[:len(parts) - node.level + 1])
                base = '.'.join(filter(None, (prefix, base)))
            if base:
                imports.add(base)
            imports.update('.'.join(filter(None, (base, alias.name)))
                           for alias in node.names if alias.name != '*')
    return sorted(imports)


def _components(graph):
    """Tarjan SCCs identify stable module cycles, independent of traversal."""
    indices, low, stack, active, result = {}, {}, [], set(), []

    def visit(node):
        indices[node] = low[node] = len(indices)
        stack.append(node)
        active.add(node)
        for target in graph.get(node, []):
            if target not in indices:
                visit(target)
                low[node] = min(low[node], low[target])
            elif target in active:
                low[node] = min(low[node], indices[target])
        if low[node] == indices[node]:
            component = []
            while True:
                target = stack.pop()
                active.remove(target)
                component.append(target)
                if target == node:
                    break
            if len(component) > 1 or node in graph.get(node, []):
                result.append(sorted(component))

    for node in sorted(graph):
        if node not in indices:
            visit(node)
    return sorted(result)


def analyze_project(root, *, baseline=None, changed_paths=None, rules=None):
    root = Path(root).resolve()
    rules = dict(rules or {'max_lines': 1000, 'forbidden_dependencies': []})
    baseline = baseline or {}
    compatible = (baseline.get('schema') == 1 and
                  baseline.get('rule_version') == RULE_VERSION and
                  baseline.get('rules_digest') == _digest(rules))
    old_facts = baseline.get('modules', {}) if compatible else {}
    changes = None if changed_paths is None or not compatible else {_path(p) for p in changed_paths}
    facts = dict(old_facts) if changes is not None else {}
    if changes is None:
        paths = sorted(path.relative_to(root).as_posix() for path in root.rglob('*')
                       if path.is_file() and not IGNORED.intersection(path.relative_to(root).parts)
                       and path.suffix in {'.py', '.rs', '.js', '.ts', '.tsx'})
    else:
        paths = sorted(changes)
    reads, unknown, errors = 0, [], []
    for relative in paths:
        relative = _path(relative)
        path = root / relative
        if not path.is_file():
            facts.pop(relative, None)
            continue
        if path.suffix not in {'.py', '.rs', '.js', '.ts', '.tsx'}:
            continue
        if not path.resolve().is_relative_to(root):
            raise ValueError('ARCHITECTURE_PATH_ESCAPE ' + relative)
        raw = path.read_bytes()
        reads += 1
        text = raw.decode('utf-8-sig', errors='replace')
        row = {'digest': hashlib.sha256(raw).hexdigest(), 'lines': len(text.splitlines()),
               'language': path.suffix.lstrip('.'), 'imports': [], 'coverage': 'unknown'}
        if path.suffix == '.py':
            try:
                row.update(imports=_imports(relative, text), coverage='static-imports')
            except (SyntaxError, ValueError) as exc:
                row['error'] = str(exc)
        facts[relative] = row
    names = {_module(path): path for path in facts if path.endswith('.py')}
    graph = {path: [] for path in names.values()}
    for path, row in facts.items():
        if row.get('error'):
            errors.append({'path': path, 'error': row['error']})
        if row.get('coverage') == 'unknown':
            unknown.append(path)
        for name in row['imports']:
            # Prefer the most specific in-project module; external imports do
            # not create false project edges.
            parts = name.split('.')
            while parts and '.'.join(parts) not in names:
                parts.pop()
            target = names.get('.'.join(parts))
            if target and target != path and target not in graph[path]:
                graph[path].append(target)
    violations = []

    def violation(kind, subjects, detail):
        subjects = sorted(subjects)
        violations.append({'uid': _digest({'kind': kind, 'subjects': subjects}),
                           'kind': kind, 'subjects': subjects, 'detail': detail})

    for path, row in facts.items():
        if row['lines'] > rules.get('max_lines', 1000):
            violation('large-module', [path], {'lines': row['lines']})
    for cycle in _components(graph):
        violation('dependency-cycle', cycle, {'modules': cycle})
    for source, target in rules.get('forbidden_dependencies', []):
        for path, dependencies in graph.items():
            if path.startswith(source):
                for dependency in dependencies:
                    if dependency.startswith(target):
                        violation('forbidden-dependency', [path, dependency],
                                  {'source': path, 'target': dependency})
    historical = {row['uid'] for row in baseline.get('violations', [])}
    introduced = [row for row in violations if row['uid'] not in historical]
    affected = set(paths)
    while True:
        expanded = affected | {path for path, targets in graph.items() if affected.intersection(targets)}
        if expanded == affected:
            break
        affected = expanded
    return {'schema': 1, 'rule_version': RULE_VERSION, 'rules': rules,
            'rules_digest': _digest(rules), 'modules': facts, 'dependencies': graph,
            'input_digest': _digest({path: row['digest'] for path, row in facts.items()}),
            'coverage': {'languages': ['python-static-imports'], 'unknown': sorted(unknown),
                         'errors': errors, 'files_read': reads, 'affected': sorted(affected)},
            'violations': violations, 'introduced': introduced,
            'historical': [row for row in violations if row['uid'] in historical],
            'baseline_compatible': compatible, 'semantic_review': 'NOT_CHECKED',
            'input_scope': 'full-scan' if changes is None else 'caller-declared-changes',
            'change_ok': (compatible and not introduced and not errors
                          and not affected.intersection(unknown))}


def make_baseline(report, *, reason, previous=None):
    """Build a proposed baseline; its diff must be reviewed before publication."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError('ARCHITECTURE_BASELINE_REASON_REQUIRED')
    previous = previous or {}
    history = list(previous.get('history', []))
    history.append({'reason': reason, 'input_digest': report['input_digest'],
                    'previous_input_digest': previous.get('input_digest'),
                    'rules_digest': report['rules_digest'],
                    'violation_uids': sorted(row['uid'] for row in report['violations'])})
    return {key: report[key] for key in ('schema', 'rule_version', 'rules', 'rules_digest',
                                        'input_digest', 'modules', 'violations')} | {
        'history': history, 'required_quality_checks_waived': False}


def main(argv=None):
    """A declared quality command; baseline proposals go to stdout for review."""
    import argparse
    parser = argparse.ArgumentParser(description='Architecture facts and reviewed legacy-baseline comparison')
    parser.add_argument('--project', default='.')
    parser.add_argument('--baseline', help='project-relative reviewed baseline JSON')
    parser.add_argument('--rules', help='project-relative rule JSON')
    parser.add_argument('--changed', action='append', help='explicit partial scope; never a full-coverage pass')
    parser.add_argument('--propose-baseline', metavar='REASON')
    args = parser.parse_args(argv)
    root = Path(args.project).resolve()
    try:
        def load(relative):
            path = (root / _path(relative)).resolve()
            if not path.is_relative_to(root):
                raise ValueError('ARCHITECTURE_PATH_ESCAPE')
            return json.loads(path.read_text(encoding='utf-8-sig'))
        baseline = load(args.baseline) if args.baseline else None
        rules = load(args.rules) if args.rules else (baseline or {}).get('rules')
        report = analyze_project(root, baseline=baseline, rules=rules, changed_paths=args.changed)
        if args.propose_baseline:
            result = make_baseline(report, previous=baseline, reason=args.propose_baseline)
            result['review_status'] = 'PROPOSED_NOT_ACCEPTED'
            code = 0
        else:
            result = report
            result['validation'] = 'PARTIAL' if args.changed is not None else 'CHECKED'
            code = 0 if report['change_ok'] and args.changed is None else 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return code
    except (OSError, ValueError) as exc:
        print(json.dumps({'status': 'NOT_CHECKED', 'error': str(exc)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
