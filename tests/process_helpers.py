"""Test-owned subprocesses stay hidden on Windows; production is not patched."""
import os
import subprocess


class temporary_env:
    _missing = object()

    def __init__(self, values):
        self.values = dict(values)
        self.previous = {}

    def start(self):
        for key in self.values:
            self.previous[key] = os.environ.get(key, self._missing)
        os.environ.update(self.values)
        return self

    def stop(self):
        for key, value in self.previous.items():
            if value is self._missing:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.previous.clear()

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop()


def options(kwargs):
    kwargs = dict(kwargs)
    if os.name == 'nt':
        kwargs['creationflags'] = kwargs.get('creationflags', 0) | subprocess.CREATE_NO_WINDOW
    return kwargs


def run(*args, **kwargs):
    return subprocess.run(*args, **options(kwargs))


def popen(*args, **kwargs):
    return subprocess.Popen(*args, **options(kwargs))


def document(root, task, kind):
    from pathlib import Path
    import json
    root=Path(root)
    state=json.loads((root/'.gsp/runs'/task/'state.json').read_text(encoding='utf-8'))
    return root/state['documents'][kind] if 'documents' in state else root/'docs/gsp/tasks'/task/(kind+'.md')


def docfolder(root, task):
    from pathlib import Path
    import json
    root=Path(root)
    state=json.loads((root/'.gsp/runs'/task/'state.json').read_text(encoding='utf-8'))
    return root/state.get('doc_dir','docs/gsp/tasks/'+task)

def session_binding(root, work_uid, kind='main', host='codex', thread='thread-main',
                    capabilities=None, register_controller=False):
    """Issue a real durable session binding for tests that exercise write gates.

    Returns the SessionBinding; use ``.handle_path`` (project-relative) to pass
    ``--context`` to session-register or ``gsp_session_context`` in hook payloads.
    """
    from pathlib import Path
    from gsp.identity_store import IdentityStore
    from gsp.session_context import create_binding
    store = IdentityStore(Path(root))
    default_caps = {'accept', 'brief', 'claim', 'dispatch', 'handoff', 'plan', 'report', 'write'}
    caps = frozenset(default_caps if capabilities is None else capabilities)
    binding = create_binding(store, work_uid, kind, host, thread, caps)
    if register_controller:
        if kind != 'main':
            raise ValueError('register_controller requires a main binding')
        from gsp.runtime import Project, dispatch, parser
        project = Project(Path(root))
        args = parser().parse_args([
            'session-register', Path(root).name, '--role', 'controller',
            '--host', host, '--thread', thread, '--write-mode', 'write',
            '--path', '.', '--context', str(Path(root) / binding.handle_path),
        ])
        # The run id is normally 'work' in integration fixtures.  Resolve it
        # from the binding's canonical work instead of guessing when possible.
        from gsp.documents import Governance
        record = Governance(Path(root)).get(work_uid)
        run_id = record.get('run_id')
        if run_id:
            args.id = run_id
        dispatch(project, args)
    return binding


def public_main_binding(root, work_uid, thread='thread-main', caller_context=None):
    """Issue the canonical main role through the real CLI in a fixture."""
    import json
    import sys
    import uuid
    from pathlib import Path
    from gsp.session_context import load_binding
    root = Path(root)
    output = root / '.gsp' / 'test-contexts' / (uuid.uuid4().hex + '.json')
    env = os.environ.copy()
    env.pop('GSP_SESSION_CONTEXT', None)
    if caller_context is not None:
        env['GSP_SESSION_CONTEXT'] = str(caller_context)
    result = run([
        sys.executable, '-X', 'utf8', '-B',
        str(Path(__file__).resolve().parents[1] / 'scripts' / 'gsp.py'),
        '--project', str(root), 'session-bind', work_uid,
        '--kind', 'main_chat', '--host', 'codex', '--thread', thread,
        '--context-out', str(output),
    ], capture_output=True, text=True, encoding='utf-8', env=env)
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    value = json.loads(result.stdout)
    if value['kind'] != 'main_chat':
        raise AssertionError('CLI did not issue the requested canonical role')
    return load_binding(output)
