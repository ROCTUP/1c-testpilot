import inspect
import unittest
from unittest.mock import Mock, patch

import server
import tc1c


class WaitTimeoutTests(unittest.TestCase):
    def test_encoder_preserves_valid_boundaries(self):
        cases = {
            0: b'\x8b\x00\xe1',
            60: b'\x8b\x3c\xe1',
            255: b'\x8b\xff\xe1',
            256: b'\x8d\x00\x01\xe1',
            65535: b'\x8d\xff\xff\xe1',
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(tc1c.mk_wait(value), expected)

    def test_encoder_rejects_invalid_values(self):
        for value in (-1, 65536, 2**64, True, False, 1.5, '60', None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, '0 to 65535'):
                    tc1c.mk_wait(value)

    def test_handler_rejects_invalid_values_before_accessing_client(self):
        handler = inspect.unwrap(server.tc_wait_for_drop_list_generation)
        with patch.object(server, '_need', side_effect=AssertionError('Invalid timeout reached the client')) as need:
            for value in (-1, 65536, True, 1.5, '60', None):
                with self.subTest(value=value):
                    result = handler('field', 'handle', value)
                    self.assertFalse(result['ok'])
                    self.assertEqual(result['code'], 'invalid_timeout')
            need.assert_not_called()

    def test_handler_uses_same_timeout_for_wire_and_socket(self):
        handler = inspect.unwrap(server.tc_wait_for_drop_list_generation)
        for value in (0, 60, 255, 256, 65535):
            with self.subTest(value=value):
                client = Mock()
                client.send_cmd.return_value = {'ok': True}
                with patch.object(server, '_need', return_value=client), \
                     patch.object(server, '_scalar', return_value=True):
                    result = handler('field', 'handle', value)
                args = client.send_cmd.call_args.kwargs
                self.assertEqual(int.from_bytes(args['middle'][1:-1], 'little'), value)
                self.assertEqual(args['timeout'], value + tc1c.TestClient.RECV_TIMEOUT)
                self.assertTrue(result['generated'])
                self.assertTrue(result['ok'])


if __name__ == '__main__':
    unittest.main()
