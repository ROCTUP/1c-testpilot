"""Verify committed zero behind a blank cell without equating arbitrary emptiness with zero."""

import ast
from pathlib import Path
import re
from types import SimpleNamespace
import unittest


def load_cell_methods():
    source = (Path(__file__).resolve().parents[1] / "app" / "_runtime.py").read_text(encoding="utf-8")
    names = {"_CellEditFailure", "_cell_step", "_cell_result", "_numeric_value", "_numeric_equivalent",
             "_possibly_rounded", "_read_cell_editor_text", "_verify_empty_cell", "tc_set_cell_text"}
    nodes = [node for node in ast.parse(source).body if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
    for node in nodes:
        if isinstance(node, ast.FunctionDef):
            node.decorator_list = []
    namespace = {"re": re}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "_runtime.py", "exec"), namespace)
    return namespace


class ZeroCellReadbackTests(unittest.TestCase):
    def run_edit(self, text="0", after="", editor="0,000", before="1,000", cancel_ok=True):
        runtime = load_cell_methods()
        mode = False
        calls = []
        connection = SimpleNamespace(_track=object())
        track = connection._track
        observations = object()
        state = {"rec_obs": observations}
        column = {"key": "Frame.Form.Table[Rows].EditField[Quantity]", "handle": "column", "name": "Quantity", "type": "InputField"}

        def begin(*args):
            nonlocal mode
            mode = True
            calls.append("begin")
            return {"ok": True}

        def end(*args, cancel=False):
            nonlocal mode
            calls.append("cancel" if cancel else "end")
            if cancel and not cancel_ok:
                return {"ok": False, "error": "cancel refused"}
            mode = False
            return {"ok": True}

        def observe(*args):
            count = sum(value == "observe" for value in calls)
            calls.append("observe")
            return before if count == 0 else after

        def present(*args):
            calls.append("read_editor")
            return {"ok": editor is not None, "presentation": editor}

        runtime.update(_need=lambda: connection, _key_class=lambda key: "Table", _cell_window=lambda *args: "Frame",
                       _cell_ready=lambda *args: None, _guid_available=lambda *args: False,
                       G=SimpleNamespace(CURRENT_MODE_IS_EDIT="mode", GET_CURRENT_ITEM="item", GET_CELL_TEXT="cell", GET_CURRENT_ROW="row"),
                       _cell_flag=lambda *args: mode, _table_columns=lambda *args: [column],
                       tc_get_current_item=lambda *args: {"ok": True, "item": [column]},
                       tc_activate=lambda *args: {"ok": True}, tc_change_row=begin,
                       tc_input_text=lambda *args: {"ok": True}, tc_end_edit_row=end, _obs_cell=observe,
                       tc_get_data_presentation=present, _native_composite_begin=lambda *args: None,
                       _native_composite_end=lambda *args: None, _state=state,
                       tc1c=SimpleNamespace(OperationError=type("OperationError", (Exception,), {})))
        runtime["_guid_available"] = lambda connection, guid: guid != "row"
        result = runtime["tc_set_cell_text"]("Frame.Form.Table[Rows]", "Quantity", text, "table")
        self.assertIs(connection._track, track)
        self.assertIs(state["rec_obs"], observations)
        return result, calls, mode

    def test_zero_uses_committed_numeric_editor_text(self):
        result, calls, mode = self.run_edit()
        self.assertTrue(result["ok"], result)
        self.assertIsNone(result["verified"])
        self.assertEqual(result["verification"], "numeric_editor_equivalent")
        self.assertEqual(result["editor_value"], "0,000")
        self.assertEqual((calls.count("begin"), calls.count("end"), calls.count("cancel")), (2, 1, 1))
        self.assertFalse(mode)

    def test_exact_editor_zero_is_verified(self):
        result, _, mode = self.run_edit(editor="0")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["verified"])
        self.assertEqual(result["verification"], "editor_value")
        self.assertFalse(mode)

    def test_empty_editor_does_not_confirm_zero(self):
        result, _, _ = self.run_edit(editor="")
        self.assertFalse(result["ok"])

    def test_wrong_or_unavailable_editor_does_not_confirm_zero(self):
        for editor in ("2", None, "0e0"):
            with self.subTest(editor=editor):
                result, _, _ = self.run_edit(editor=editor)
                self.assertFalse(result["ok"])

    def test_nonzero_and_leading_zero_code_do_not_use_zero_readback(self):
        for text in ("1", "00", "000", "0e0"):
            with self.subTest(text=text):
                result, calls, _ = self.run_edit(text=text)
                self.assertFalse(result["ok"])
                self.assertNotIn("read_editor", calls)

    def test_failed_readback_cleanup_is_an_error(self):
        result, _, _ = self.run_edit(cancel_ok=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "verification_cleanup_failed")

    def test_existing_empty_value_verification_is_preserved(self):
        result, calls, mode = self.run_edit(text="", after="Not filled", editor="")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["verified"])
        self.assertEqual(result["verification"], "empty_value")
        self.assertEqual(calls.count("cancel"), 1)
        self.assertFalse(mode)

    def test_numeric_zero_is_not_certified_as_empty(self):
        result, _, _ = self.run_edit(text="", after="Not filled", editor="0,000")
        self.assertFalse(result["ok"])

    def test_visible_numeric_equivalence_uses_existing_path(self):
        result, calls, _ = self.run_edit(after="0,000")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["verification"], "numeric_equivalent")
        self.assertNotIn("read_editor", calls)

    def test_already_blank_display_keeps_changed_false(self):
        result, _, _ = self.run_edit(before="")
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["changed"])


if __name__ == "__main__":
    unittest.main()
