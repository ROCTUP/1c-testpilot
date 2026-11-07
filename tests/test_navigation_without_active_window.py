from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import _runtime as runtime


class NavigationWithoutActiveWindowTests(unittest.TestCase):
    def resolve(self, active, windows):
        with patch.object(runtime, '_window', return_value=active):
            with patch.object(runtime, '_read_open_windows', return_value=windows):
                with patch.object(runtime._code_execution, 'protected', return_value=False):
                    with patch.object(runtime._code_execution, 'guard'):
                        return runtime._winkey(object())

    def test_selects_the_only_main_window_after_an_empty_active_window_response(self):
        active = {'ok': False, 'key': None, 'class': None, 'title': None, 'addressable': False}
        self.assertEqual(self.resolve(active, [{'class': 'MainFrame', 'key': 'MainFrame[owned]'}]),
                         'MainFrame[owned]')

    def test_preserves_the_protected_helper_fallback(self):
        self.assertEqual(self.resolve({'key': None, 'code': 'no_active_work_window'},
                                      [{'class': 'MainFrame', 'key': 'MainFrame[owned]'}]),
                         'MainFrame[owned]')

    def test_does_not_choose_between_two_main_windows(self):
        with self.assertRaisesRegex(RuntimeError, 'no active window'):
            self.resolve({'key': None}, [{'class': 'MainFrame', 'key': 'MainFrame[first]'},
                                         {'class': 'MainFrame', 'key': 'MainFrame[second]'}])

    def test_does_not_substitute_a_secondary_window(self):
        with self.assertRaisesRegex(RuntimeError, 'no active window'):
            self.resolve({'key': None}, [{'class': 'SecondaryFrame', 'key': 'SecondaryFrame[dialog]'}])

    def test_does_not_bypass_a_native_preview_or_an_unavailable_window(self):
        for active in ({'key': None, 'native': True},
                       {'key': None, 'code': 'active_window_unavailable'}):
            with self.subTest(active=active):
                with self.assertRaisesRegex(RuntimeError, 'no active window'):
                    self.resolve(active, [{'class': 'MainFrame', 'key': 'MainFrame[owned]'}])

    def test_preserves_a_valid_active_window(self):
        self.assertEqual(self.resolve({'key': 'SecondaryFrame[active]'}, []), 'SecondaryFrame[active]')


if __name__ == '__main__':
    unittest.main()
