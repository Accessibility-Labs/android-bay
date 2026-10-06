"""Launcher regressions using temporary state and separate Windows processes."""
import io
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import launcher


APP = Path(__file__).resolve().parents[1]
PYTHON = APP / 'runtime' / 'python.exe'
if not PYTHON.is_file():
    PYTHON = Path(sys.executable)

# The bundled Python uses an isolated ._pth configuration, so explicitly add the
# app directory supplied as argv[1]. All state remains in argv[2]'s temp folder.
CHILD = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import launcher
try:
    handle = launcher.acquire_lock(Path(sys.argv[2]))
    if handle is None:
        print(json.dumps({'status': 'contended'}))
    else:
        handle.close()
        print(json.dumps({'status': 'acquired'}))
except Exception as error:
    print(json.dumps({'errorType': type(error).__name__}))
    raise SystemExit(3)
'''


@unittest.skipUnless(os.name == 'nt', 'Uses actual Windows msvcrt byte-range locks')
class WindowsLockTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def child_attempt(self):
        result = subprocess.run([str(PYTHON), '-c', CHILD, str(APP), str(self.folder)],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, 'Lock attempt crashed: ' + result.stdout.strip())
        self.assertEqual(result.stderr, '')
        return json.loads(result.stdout)

    def test_first_start_with_no_lock_file(self):
        self.assertFalse((self.folder / 'server.lock').exists())
        self.assertEqual(self.child_attempt()['status'], 'acquired')
        self.assertTrue((self.folder / 'server.lock').is_file())

    def test_first_start_with_preexisting_empty_lock_file(self):
        (self.folder / 'server.lock').touch()
        self.assertEqual(self.child_attempt()['status'], 'acquired')

    def test_other_process_contention_returns_none_then_reacquires_after_release(self):
        handle = launcher.acquire_lock(self.folder)
        self.assertIsNotNone(handle)
        try:
            self.assertEqual(self.child_attempt()['status'], 'contended')
        finally:
            handle.close()
        self.assertEqual(self.child_attempt()['status'], 'acquired')

    def test_noncontention_lock_errors_propagate_and_close_descriptor(self):
        for code in (errno.EBADF, errno.EINVAL):
            descriptors = []
            def fail(descriptor, *unused):
                descriptors.append(descriptor)
                raise OSError(code, 'synthetic lock failure')
            with self.subTest(errno=code), patch('msvcrt.locking', side_effect=fail):
                with self.assertRaises(OSError) as caught:
                    launcher.acquire_lock(self.folder)
                self.assertEqual(caught.exception.errno, code)
            self.assertEqual(len(descriptors), 1)
            with self.assertRaises(OSError):
                os.fstat(descriptors[0])


class ExistingServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def save(self, value):
        (self.folder / 'server.json').write_text(json.dumps(value), encoding='utf-8')

    def test_missing_or_malformed_state_does_not_request_network(self):
        with patch.object(launcher.urllib.request, 'urlopen') as request:
            self.assertIsNone(launcher.existing_server(self.folder))
            (self.folder / 'server.json').write_text('{', encoding='utf-8')
            self.assertIsNone(launcher.existing_server(self.folder))
        request.assert_not_called()

    def test_non_loopback_urls_are_rejected_without_request(self):
        with patch.object(launcher.urllib.request, 'urlopen') as request:
            for url in ['https://127.0.0.1:1234', 'http://example.test:1234',
                        'http://127.0.0.1', 'file:///C:/private']:
                self.save({'url': url, 'token': 'synthetic-token'})
                self.assertIsNone(launcher.existing_server(self.folder))
        request.assert_not_called()

    def test_valid_local_health_returns_saved_state(self):
        state = {'url': 'http://127.0.0.1:12345', 'token': 'synthetic-token'}
        self.save(state)
        with patch.object(launcher.urllib.request, 'urlopen', return_value=io.BytesIO(b'{"ok":true}')) as request:
            self.assertEqual(launcher.existing_server(self.folder), state)
        request.assert_called_once_with('http://127.0.0.1:12345/api/health', timeout=2)

    def test_unreachable_or_unhealthy_local_server_returns_none(self):
        self.save({'url': 'http://127.0.0.1:12345', 'token': 'synthetic-token'})
        with patch.object(launcher.urllib.request, 'urlopen', side_effect=OSError('synthetic connection failure')):
            self.assertIsNone(launcher.existing_server(self.folder))
        with patch.object(launcher.urllib.request, 'urlopen', return_value=io.BytesIO(b'{"ok":false}')):
            self.assertIsNone(launcher.existing_server(self.folder))


if __name__ == '__main__':
    unittest.main()
