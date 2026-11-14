"""Guards for automatic user-level updates."""
import json
from pathlib import Path


def update_guard(project_root):
    root = Path(project_root).resolve()
    active = root / '.gsp' / 'active.json'
    if active.exists():
        try:
            task = json.loads(active.read_text(encoding='utf-8')).get('task')
        except (OSError, ValueError):
            return {'allowed': False, 'reason': 'active-state-unreadable'}
        return {'allowed': False, 'reason': 'active-task', 'task': task}
    runs = root / '.gsp' / 'runs'
    if runs.exists():
        for state_file in runs.glob('*/state.json'):
            try:
                state = json.loads(state_file.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                return {'allowed': False, 'reason': 'run-state-unreadable', 'path': str(state_file)}
            if state.get('inflight') or state.get('status') in {'checking', 'execute', 'review', 'needs-verification'}:
                return {'allowed': False, 'reason': 'active-run', 'task': state.get('id')}
    return {'allowed': True, 'reason': 'no-active-task'}
