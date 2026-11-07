from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import unittest
from unittest.mock import patch
import tc1c


def native_user(buffer, size):
    buffer.value = 'NativeTestUser'
    return 1


class UserIdentityTests(unittest.TestCase):
    def test_missing_non_windows_identity_retains_the_original_error(self):
        with patch('tc1c.os.name', 'posix'):
            with patch('getpass.getuser', side_effect=OSError('original identity error')):
                with self.assertRaisesRegex(OSError, 'original identity error'):
                    tc1c._current_user_name()

    def test_preserves_environment_identity(self):
        with patch('getpass.getuser', return_value='EnvironmentUser'):
            self.assertEqual(tc1c._current_user_name(), 'EnvironmentUser')

    @unittest.skipUnless(tc1c.os.name == 'nt', 'Windows native identity')
    def test_resolves_native_windows_user_when_environment_is_empty(self):
        with patch('getpass.getuser', side_effect=OSError('No username set in the environment')):
            with patch('ctypes.windll.advapi32.GetUserNameW', side_effect=native_user):
                self.assertEqual(tc1c._current_user_name(), 'NativeTestUser')

    @unittest.skipUnless(tc1c.os.name == 'nt', 'Windows native identity')
    def test_native_lookup_failure_is_reported(self):
        with patch('getpass.getuser', side_effect=OSError('No username set in the environment')):
            with patch('ctypes.windll.advapi32.GetUserNameW', return_value=0):
                with self.assertRaises(OSError):
                    tc1c._current_user_name()

    @unittest.skipUnless(tc1c.os.name == 'nt', 'Windows native identity')
    def test_connect_reaches_socket_without_environment_username(self):
        with patch('getpass.getuser', side_effect=OSError('No username set in the environment')):
            with patch('ctypes.windll.advapi32.GetUserNameW', side_effect=native_user):
                with patch('tc1c.socket.create_connection', side_effect=RuntimeError('socket reached')):
                    with self.assertRaisesRegex(RuntimeError, 'socket reached'):
                        tc1c.TestClient().connect(retries=1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
