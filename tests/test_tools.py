import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forge.tools import ToolExecutor


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'workspace'
        self.root.mkdir()
        self.approvals = []
        self.tool = ToolExecutor(self.root, self.approve)

    def approve(self, message):
        self.approvals.append(message)
        return True

    def call(self, name, **arguments):
        return json.loads(self.tool.execute(name, arguments))

    def test_write_read_replace_and_search(self):
        self.assertTrue(self.call('write_file', path='src/a.txt', content='hello world')['ok'])
        self.assertEqual(self.call('read_file', path='src/a.txt')['content'], 'hello world')
        self.assertTrue(self.call('replace_text', path='src/a.txt', old='world', new='team')['ok'])
        self.assertEqual(self.call('search_files', pattern='team')['matches'],
                         [{'path': 'src/a.txt', 'line': 1, 'text': 'hello team'}])
        self.assertEqual(len(self.approvals), 2)

    def test_escape_and_symlink_and_secret_are_rejected(self):
        outside = Path(self.temp.name) / 'outside'
        outside.write_text('secret')
        (self.root / 'link').symlink_to(outside)
        for path in ('../outside', str(outside), 'link', '.env', '.git/config'):
            with self.subTest(path=path):
                self.assertFalse(self.call('read_file', path=path)['ok'])
                self.assertFalse(self.call('write_file', path=path, content='bad')['ok'])
        self.assertEqual(outside.read_text(), 'secret')
        self.assertEqual(self.approvals, [])

    def test_listing_and_search_exclude_private_files_and_symlinks(self):
        (self.root / 'visible').write_text('needle')
        (self.root / '.env').write_text('needle')
        (self.root / '.git').mkdir()
        (self.root / '.git' / 'config').write_text('needle')
        (self.root / 'alias').symlink_to(self.root / 'visible')
        self.assertEqual(self.call('list_files')['files'], ['visible'])
        self.assertEqual(len(self.call('search_files', pattern='needle')['matches']), 1)

    def test_denial_and_ambiguous_replace_leave_file_unchanged(self):
        target = self.root / 'a'
        target.write_text('same same')
        self.assertFalse(self.call('replace_text', path='a', old='same', new='other')['ok'])
        self.tool.approve = lambda _: False
        self.assertFalse(self.call('write_file', path='a', content='new')['ok'])
        self.assertEqual(target.read_text(), 'same same')

    def test_shell_requires_enablement_and_approval(self):
        self.assertFalse(self.call('run_command', command='printf hello')['ok'])
        self.tool.allow_shell = True
        self.tool.approve = lambda _: False
        self.assertFalse(self.call('run_command', command='touch marker')['ok'])
        self.assertFalse((self.root / 'marker').exists())
        self.tool.approve = self.approve
        result = self.call('run_command', command='printf hello')
        self.assertEqual(result['output'], 'hello')
        self.assertEqual(result['exit_code'], 0)
        self.assertIn('UNSANDBOXED', self.approvals[-1])

    def test_command_timeout_and_output_limit(self):
        self.tool.allow_shell = True
        with patch('forge.tools.COMMAND_TIMEOUT', .05):
            self.assertTrue(self.call('run_command', command='sleep 2')['timed_out'])
        with patch('forge.tools.MAX_OUTPUT_BYTES', 10):
            result = self.call('run_command', command='printf 12345678901234567890')
        self.assertTrue(result['truncated'])
        self.assertEqual(result['output'], '1234567890')

    def test_invalid_tool_arguments_return_errors(self):
        self.assertFalse(self.call('missing')['ok'])
        self.assertFalse(self.call('read_file')['ok'])
        self.assertFalse(self.call('read_file', path=1)['ok'])
        self.assertFalse(self.call('list_files', extra='x')['ok'])

    def test_approval_contains_complete_change(self):
        self.call('write_file', path='a', content='before\n')
        self.assertIn('+before', self.approvals[-1])
        self.call('replace_text', path='a', old='before', new='after')
        self.assertIn('-before', self.approvals[-1])
        self.assertIn('+after', self.approvals[-1])
        count = len(self.approvals)
        result = self.call('write_file', path='a', content='x' * 17000)
        self.assertFalse(result['ok'])
        self.assertEqual(len(self.approvals), count)
        self.assertEqual((self.root / 'a').read_text(), 'after\n')

    def test_fifo_cannot_be_read_or_overwritten(self):
        os.mkfifo(self.root / 'pipe')
        self.assertFalse(self.call('read_file', path='pipe')['ok'])
        self.assertFalse(self.call('write_file', path='pipe', content='x')['ok'])


if __name__ == '__main__':
    unittest.main()
