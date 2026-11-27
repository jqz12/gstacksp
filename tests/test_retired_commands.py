"""Retired public capabilities cannot return through compatibility routing."""
import contextlib
import io
import unittest

from gsp.runtime import parser


class RetiredCommandTests(unittest.TestCase):
    def test_learning_command_is_retired(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser().parse_args(['learn'])
