"""Test-owned subprocesses stay hidden on Windows; production is not patched."""
from contextlib import contextmanager
import os
import subprocess


def options(kwargs):
    kwargs = dict(kwargs)
    if os.name == 'nt':
        kwargs['creationflags'] = kwargs.get('creationflags', 0) | subprocess.CREATE_NO_WINDOW
    return kwargs


def run(*args, **kwargs):
    return subprocess.run(*args, **options(kwargs))


def popen(*args, **kwargs):
    return subprocess.Popen(*args, **options(kwargs))


@contextmanager
def environment(name, value):
    """Temporarily set one variable without rebuilding the whole native environment."""
    missing = object()
    previous = os.environ.get(name, missing)
    os.environ[name] = value
    try:
        yield
    finally:
        if previous is missing:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


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
