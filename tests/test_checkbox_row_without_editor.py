from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import _field_batches as batches


class CheckboxRowWithoutEditorTests(unittest.TestCase):
    def run_write(self, *, checked=True, text=False, readonly=False, initial_edit=False, auto_edit=False):
        self.value = checked
        self.mode = initial_edit
        self.toggles = self.starts = self.ends = 0
        self.checkbox = {'key': 'Frame.Form.Table[Rows].EditField[Flag]', 'handle': 'flag', 'name': 'Flag'}
        if text:
            self.checkbox['text'] = 'replacement'
            cells = [{'column': 'Flag', 'text': 'replacement'}]
        else:
            self.checkbox['checked'] = False
            cells = [{'column': 'Flag', 'checked': False}]
        window = 'Frame'
        client = SimpleNamespace()
        def ready(connection, key, handle):
            if readonly and key == self.checkbox['key']:
                raise batches.Failure('readonly', 'The checkbox is read-only')
        def start(*args):
            self.starts += 1
            return {'ok': False, 'code': 'row_edit_refused', 'error': 'Row editing did not start.'}
        def finish(*args):
            self.ends += 1
            self.mode = False
            return {'ok': True}
        def apply(server, connection, obj):
            if self.value != obj['checked']:
                self.toggles += 1
                self.value = obj['checked']
                if auto_edit:
                    self.mode = True
            return {'ok': True}
        server = SimpleNamespace(
            _need=lambda: client, _key_class=lambda key: 'Table',
            _collection_parent=lambda form: window, _cell_window=lambda *args: window,
            _cell_ready=ready, G=SimpleNamespace(CURRENT_MODE_IS_EDIT='mode'),
            _cell_flag=lambda *args: self.mode,
            _obs_cell=lambda *args: 'Да' if self.value else 'Нет',
            tc_activate=lambda *args: {'ok': True},
            tc_get_current_item=lambda *args: {'item': [{'key': self.checkbox['key']}]},
            tc_change_row=start, tc_end_edit_row=finish)
        with patch.object(batches, 'observations', side_effect=lambda *args: nullcontext()):
            with patch.object(batches, 'live'), patch.object(batches, 'ensure_pending'):
                with patch.object(batches, 'owner', return_value='Frame.Form'):
                    with patch.object(batches, 'row_objects', return_value=[self.checkbox]):
                        with patch.object(batches, 'begin_record', return_value=None), patch.object(batches, 'finish_record'):
                            with patch.object(batches, 'apply_checkbox', side_effect=apply):
                                with patch.object(batches, 'read_property', side_effect=lambda *args: 'Да' if self.value else 'Нет'):
                                    return batches.write(server, key='Frame.Form.Table[Rows]', handle='rows',
                                                         cells=cells)

    def test_changes_an_immediate_checkbox_without_starting_row_editing(self):
        result = self.run_write()
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['edit_finished'])
        self.assertTrue(result['final_verified'])
        self.assertFalse(self.value)
        self.assertEqual((self.starts, self.ends, self.toggles), (0, 0, 1))

    def test_does_not_toggle_an_already_matching_checkbox(self):
        result = self.run_write(checked=False)
        self.assertTrue(result['ok'], result)
        self.assertEqual((self.starts, self.ends, self.toggles), (0, 0, 0))

    def test_finishes_editing_if_the_checkbox_starts_it_automatically(self):
        result = self.run_write(auto_edit=True)
        self.assertTrue(result['ok'], result)
        self.assertEqual((self.starts, self.ends, self.toggles), (0, 1, 1))

    def test_preserves_the_existing_editing_row_path(self):
        result = self.run_write(initial_edit=True)
        self.assertTrue(result['ok'], result)
        self.assertEqual((self.starts, self.ends, self.toggles), (0, 1, 1))

    def test_text_still_requires_the_row_editor(self):
        result = self.run_write(text=True)
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'row_edit_refused')
        self.assertEqual((self.starts, self.toggles), (1, 0))

    def test_readonly_checkbox_is_refused_before_any_change(self):
        result = self.run_write(readonly=True)
        self.assertFalse(result['ok'])
        self.assertEqual(result['code'], 'readonly')
        self.assertEqual((self.starts, self.toggles), (0, 0))


if __name__ == '__main__':
    unittest.main()
