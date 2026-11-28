"""Per-command validation cache with explicit phase boundaries.

The context is intentionally short-lived. It deduplicates identity probes,
fingerprints, receipt reads and delivery validation inside one dispatch command.
It never survives a command, and beginning a new phase drops every cached
value so the final gate re-reads source content, receipts and log digests.
"""
from datetime import datetime, timezone
import hashlib
from pathlib import Path


_ACTIVE = None


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def current_context():
    return _ACTIVE


def activate(context):
    global _ACTIVE
    previous = _ACTIVE
    _ACTIVE = context
    return previous


def deactivate(previous):
    global _ACTIVE
    _ACTIVE = previous


def invalidate(reason):
    context = _ACTIVE
    if context is not None:
        context.invalidate(reason)


class ValidationContext:
    def __init__(self, project, phase='initial', run_id=None):
        self.project = project
        self.run_id = str(run_id) if run_id else None
        self.phase = phase
        self.hits = 0
        self.misses = 0
        self._cache = {}
        self.invalidations = []
        self.delivery_validation = {}
        self._phase_state = {}
        self._state_path = (Path(project.meta) / 'runs' / self.run_id / 'state.json'
                            if self.run_id else None)
        self.begin_phase(phase)

    def _state_snapshot(self):
        if self._state_path is None:
            return {}
        try:
            content = self._state_path.read_bytes()
        except FileNotFoundError:
            return {str(self._state_path): None}
        except OSError as exc:
            return {str(self._state_path): 'error:' + type(exc).__name__}
        return {str(self._state_path): hashlib.sha256(content).hexdigest()}

    def get(self, namespace, key, loader):
        token = (namespace, key)
        if token in self._cache:
            self.hits += 1
            return self._cache[token]
        self.misses += 1
        value = loader()
        self._cache[token] = value
        return value

    def invalidate(self, reason):
        self._cache.clear()
        self.invalidations.append({'phase': self.phase, 'reason': str(reason), 'at': _now()})

    def invalidate_namespace(self, namespace):
        for token in [token for token in self._cache if token[0] == namespace]:
            del self._cache[token]
        self.invalidations.append({
            'phase': self.phase, 'reason': 'namespace:' + str(namespace), 'at': _now(),
        })

    def begin_phase(self, name):
        self.phase = name
        self._cache.clear()
        self.delivery_validation = {}
        self._phase_state = self._state_snapshot()

    def assert_unchanged(self):
        current = self._state_snapshot()
        if current == self._phase_state:
            return
        changed = sorted(path for path in set(current) | set(self._phase_state)
                         if current.get(path) != self._phase_state.get(path))
        from .core import (WorkflowError)
        raise WorkflowError('VALIDATION_STATE_CHANGED ' + ', '.join(changed))

    def summary(self):
        return {'phase': self.phase, 'run_id': self.run_id, 'hits': self.hits,
                'misses': self.misses, 'invalidations': list(self.invalidations)}
