import inspect
import unittest
import uuid
from unittest.mock import Mock, patch

import server
import tc1c


class SaveCleanupTests(unittest.TestCase):
    def invoke(self, responses, filename='output.txt', can_clear=True, wrapped=False):
        client = Mock()
        client.platform_version = None
        client._object_handles = {}
        client.send_cmd.side_effect = responses
        key = 'MainFrame[%s].ManagedForm[%s].EditField[Document]' % (uuid.uuid4(), uuid.uuid4())
        handle = str(uuid.uuid4())
        with patch.object(server, '_need', return_value=client), \
             patch.object(server, '_state', {'client': client}), \
             patch.object(server, '_verify_target', return_value=('present', True)), \
             patch.object(server, '_observe', return_value=(None, None)), \
             patch.object(server, '_kind_of', return_value='TextDocumentField'), \
             patch.object(server, '_guid_available', return_value=can_clear):
            handler = server.tc_write_content_to_file if wrapped else inspect.unwrap(server.tc_write_content_to_file)
            result = handler(key, handle, filename=filename)
        return result, client

    def test_successful_save_and_cleanup(self):
        result, client = self.invoke([{'ok': True}] * 5)
        self.assertTrue(result['ok'])
        self.assertTrue(result['dialog_answer_cleared'])
        self.assertNotIn('cleanup_error', result)
        self.assertEqual([c.args[0] for c in client.send_cmd.call_args_list], [
            server.G.CLEAR_FILE_DIALOG_RESULT, server.G.SET_FILE_DIALOG_RESULT,
            server.G.WRITE_CONTENT_TO_FILE, server.G.WRITE_CONTENT_TO_FILE,
            server.G.CLEAR_FILE_DIALOG_RESULT])

    def test_cleanup_failure_preserves_accepted_save(self):
        failures = [{'ok': False}, {'ok': False, 'error': 'cleanup refused'},
                    tc1c.OperationError(15, None), OSError('socket closed'), ValueError('malformed reply')]
        for failure in failures:
            with self.subTest(failure=repr(failure)):
                result, _ = self.invoke([{'ok': True}] * 4 + [failure])
                self.assertTrue(result['ok'])
                self.assertFalse(result['dialog_answer_cleared'])
                self.assertTrue(result['cleanup_error'])
                self.assertNotIn('error', result)
                self.assertEqual(result['filename'], 'output.txt')

    def test_cleanup_failure_preserves_preparation_error(self):
        result, client = self.invoke([
            {'ok': True}, {'ok': False}, tc1c.OperationError(15, None)])
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'the file dialog answer was not accepted; save was not sent')
        self.assertTrue(result['cleanup_error'])
        self.assertNotIn(server.G.WRITE_CONTENT_TO_FILE,
                         [c.args[0] for c in client.send_cmd.call_args_list])

    def test_cleanup_failure_preserves_save_exception(self):
        primary = OSError('save disconnected')
        with self.assertRaises(OSError) as caught:
            self.invoke([{'ok': True}] * 2 + [primary, OSError('cleanup disconnected')])
        self.assertIs(caught.exception, primary)

    def test_wrapped_platform_failure_retains_cleanup_diagnostics(self):
        for cleanup in ({'ok': True}, OSError('cleanup disconnected')):
            with self.subTest(cleanup=repr(cleanup)):
                result, _ = self.invoke([{'ok': True}] * 2 + [
                    tc1c.OperationError(9, 'field'), cleanup], wrapped=True)
                self.assertFalse(result['ok'])
                self.assertEqual(result['code'], 'target_not_interactive')
                self.assertEqual(result['status_code'], 9)
                self.assertEqual(result['filename'], 'output.txt')
                self.assertEqual(result['dialog_answer_cleared'], isinstance(cleanup, dict))
                self.assertEqual('cleanup_error' in result, isinstance(cleanup, OSError))

    def test_failed_save_stays_failed(self):
        result, _ = self.invoke([{'ok': True}, {'ok': True}, {'ok': False},
                                 {'ok': True}, {'ok': False}])
        self.assertFalse(result['ok'])
        self.assertTrue(result['cleanup_error'])

    def test_initial_cleanup_failure_does_not_send_save(self):
        result, client = self.invoke([{'ok': False}])
        self.assertFalse(result['ok'])
        self.assertEqual(client.send_cmd.call_count, 1)
        self.assertNotIn('dialog_answer_cleared', result)
        self.assertNotIn('cleanup_error', result)

    def test_without_filename_does_not_clear_dialog_answers(self):
        result, client = self.invoke([{'ok': True}] * 2, filename=None)
        self.assertTrue(result['ok'])
        self.assertNotIn('dialog_answer_cleared', result)
        self.assertEqual(client.send_cmd.call_count, 2)

    def test_older_platform_does_not_clear_dialog_answers(self):
        result, client = self.invoke([{'ok': True}] * 3, can_clear=False)
        self.assertTrue(result['ok'])
        self.assertFalse(result['dialog_answer_cleared'])
        self.assertNotIn('cleanup_error', result)
        self.assertNotIn(server.G.CLEAR_FILE_DIALOG_RESULT,
                         [c.args[0] for c in client.send_cmd.call_args_list])


if __name__ == '__main__':
    unittest.main()
