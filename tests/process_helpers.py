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
