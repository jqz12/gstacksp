"""The CLI is a compatibility boundary, not a dependency of business services."""
import ast
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gsp import runtime


class DomainDependencyTests(unittest.TestCase):
    def test_domain_modules_do_not_import_runtime_cli(self):
        root=Path(__file__).resolve().parents[1]/'gsp'
        violations=[]
        for path in root.glob('*.py'):
            if path.name in {'runtime.py','cli.py'}:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                if isinstance(node,ast.ImportFrom) and node.module in {'runtime','gsp.runtime'}:
                    violations.append((path.name,node.lineno))
        self.assertEqual(violations,[])

    def test_cli_facade_has_no_business_dispatch_or_project_class(self):
        tree=ast.parse(Path(runtime.__file__).read_text(encoding='utf-8'))
        names={n.name for n in tree.body if isinstance(n,(ast.ClassDef,ast.FunctionDef))}
        self.assertNotIn('Project',names)
        self.assertNotIn('_dispatch',names)
        self.assertEqual(runtime.Project.__module__,'gsp.project')

    def test_legacy_runtime_patch_point_reaches_business_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('gsp.runtime.snapshot',return_value={'a.py':'digest'}) as scan:
                self.assertEqual(runtime.fingerprint(directory),
                                 runtime.sha(b'{"a.py": "digest"}'))
                scan.assert_called_once_with(Path(directory).resolve())

    def test_imported_legacy_module_patch_reaches_git_service(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('gsp.runtime.subprocess') as process:
                self.assertIs(runtime._git_run(Path(directory),'status'),process.run.return_value)
                process.run.assert_called_once()


if __name__=='__main__':
    unittest.main()
