import socket
import threading
import unittest
from collections import deque
from contextlib import contextmanager

import guids
import tc1c


TRAILER = bytes.fromhex('6653b2a6')
ESCAPE = bytes.fromhex('6552b1a5')
SESSION = '11111111-2222-3333-4444-555555555555'


class ReceiveChunks:
    def __init__(self, sock, sizes):
        self.sock = sock
        self.sizes = deque(sizes)

    def recv(self, size):
        if not self.sizes:
            return self.sock.recv(size)
        data = self.sock.recv(min(size, self.sizes[0]))
        self.sizes[0] -= len(data)
        if not self.sizes[0]:
            self.sizes.popleft()
        return data

    def __getattr__(self, name):
        return getattr(self.sock, name)


@contextmanager
def connected_client(chunks=()):
    local, peer = socket.socketpair()
    peer.settimeout(2)
    client = tc1c.TestClient()
    client.s = ReceiveChunks(local, chunks)
    client.sess = SESSION
    try:
        yield client, peer
    finally:
        client.close()
        peer.close()


class FrameReceptionTests(unittest.TestCase):
    def assert_frame_at_every_split(self, wire, expected):
        for split in range(1, len(wire)):
            with self.subTest(split=split), connected_client([split]) as (client, peer):
                peer.sendall(wire)
                self.assertEqual(client._recv(timeout=1), expected)
                self.assertEqual(client._buf, b'')
                self.assertEqual(client._pending, 0)

    def test_issue_8_type_descriptor_guid_is_not_parsed_as_values(self):
        for descriptor in (b'\xc0\x55', b'\xe0\x55'):
            with self.subTest(descriptor=descriptor.hex()):
                guid = b'\x9b\xff\xff' + bytes(13)
                frame = b'B' + descriptor + guid + TRAILER
                self.assert_frame_at_every_split(frame, frame)

    def test_guid_payload_cannot_end_the_frame(self):
        guid = TRAILER + bytes(12)
        expected = b'B\xe0\x55' + guid + TRAILER
        wire = b'B\xe0\x55' + ESCAPE + guid + TRAILER
        self.assert_frame_at_every_split(wire, expected)

    def test_escaped_trailer_and_escape_in_string(self):
        payload = b'left' + TRAILER + ESCAPE + b'right'
        expected = b'B\xfa\x11' + payload + TRAILER
        wire = b'B\xfa\x11left' + ESCAPE + TRAILER + ESCAPE + ESCAPE + b'right' + TRAILER
        self.assert_frame_at_every_split(wire, expected)

    def test_escape_followed_by_trailer_is_still_payload(self):
        expected = b'B\xfa\x08' + ESCAPE + TRAILER + TRAILER
        wire = b'B\xfa\x08' + ESCAPE + ESCAPE + ESCAPE + TRAILER + TRAILER
        self.assert_frame_at_every_split(wire, expected)

    def test_multiple_frames_arriving_together_are_read_separately(self):
        frames = [b'B\xfa\x03one' + TRAILER, b'C\xfa\x03two' + TRAILER,
                  b'B\xfa\x05three' + TRAILER]
        with connected_client() as (client, peer):
            peer.sendall(b''.join(frames))
            peer.shutdown(socket.SHUT_WR)
            for frame in frames:
                self.assertEqual(client._recv(timeout=1), frame)
            self.assertEqual(client._buf, b'')

    def test_bytewise_reception_with_fixed_width_payloads(self):
        frame = b'B\x8b\x95\x8d\x9b\xff\x8f\x9b\xff\xff\xff' + TRAILER
        with connected_client([1] * len(frame)) as (client, peer):
            peer.sendall(frame)
            self.assertEqual(client._recv(timeout=1), frame)

    def test_long_strings_have_eight_byte_lengths(self):
        for tag, text, encoding in ((b'\xfc', 'x' * 70000, 'latin1'),
                                    (b'\xf9', 'я' * 69999 + '😀', 'utf-16le')):
            payload = text.encode(encoding)
            count = len(payload) // (2 if encoding == 'utf-16le' else 1)
            value = tag + count.to_bytes(8, 'little') + payload
            frame = b'B' + value + TRAILER
            with self.subTest(tag=tag.hex()), connected_client([1] * 10) as (client, peer):
                failures = []

                def send():
                    try:
                        peer.sendall(frame)
                    except Exception as exc:
                        failures.append(exc)

                writer = threading.Thread(target=send, daemon=True)
                writer.start()
                try:
                    self.assertEqual(client._recv(timeout=2), frame)
                    kind = 'ustr' if encoding == 'utf-16le' else 'str'
                    self.assertEqual(tc1c.decode_stream(value), [(kind, text)])
                finally:
                    writer.join(timeout=3)
                self.assertFalse(writer.is_alive(), 'The fixture writer did not finish')
                self.assertEqual(failures, [])


class CommandRecoveryTests(unittest.TestCase):
    def command(self, client):
        return client.send_cmd(guids.GET_CURRENT_ERROR, None, middle=b'\xe0\x41',
                               pad=4, timeout=0.05)

    def read_request(self, peer):
        data = b''
        while not data.endswith(TRAILER):
            chunk = peer.recv(65536)
            self.assertTrue(chunk, 'Client closed before sending a complete request')
            data += chunk
        self.assertEqual(data[:1], b'A')
        return data

    def test_late_reply_is_drained_before_next_command(self):
        late = b'B\xfa\x04late' + TRAILER
        current = b'B\xfa\x03new' + TRAILER
        with connected_client() as (client, peer):
            peer.sendall(late[:5])
            with self.assertRaises(socket.timeout):
                self.command(client)
            self.read_request(peer)
            self.assertEqual(client._pending, 1)
            self.assertEqual(client._buf, late[:5])
            callbacks = []
            client._pending_reply_callback = callbacks.append
            peer.sendall(late[5:])
            failures = []

            def reply():
                try:
                    self.read_request(peer)
                    peer.sendall(current)
                except Exception as exc:
                    failures.append(exc)

            responder = threading.Thread(target=reply, daemon=True)
            responder.start()
            try:
                result = client.send_cmd(guids.GET_CURRENT_ERROR, None,
                                         middle=b'\xe0\x41', pad=4, timeout=1)
            finally:
                responder.join(timeout=3)
            self.assertFalse(responder.is_alive())
            self.assertEqual(failures, [])
            self.assertTrue(result['ok'])
            self.assertEqual(result['raw'], current)
            self.assertEqual(callbacks, [late])
            self.assertNotIn('_pending_reply_callback', vars(client))
            self.assertEqual(client._pending, 0)
            self.assertEqual(client._buf, b'')

    def test_missing_late_reply_prevents_sending_another_command(self):
        with connected_client() as (client, peer):
            client.RESYNC_TIMEOUT = 0.05
            with self.assertRaises(socket.timeout):
                self.command(client)
            self.read_request(peer)
            counter = client._counter
            with self.assertRaisesRegex(RuntimeError, 'connection desynchronised'):
                self.command(client)
            self.assertEqual(client._counter, counter)
            self.assertEqual(client._pending, 1)
            peer.settimeout(0.05)
            with self.assertRaises(socket.timeout):
                peer.recv(1)

    def test_eof_mid_frame_closes_connection_and_blocks_new_commands(self):
        with connected_client() as (client, peer):
            peer.sendall(b'B\xfa\x05ab')
            peer.shutdown(socket.SHUT_WR)
            with self.assertRaisesRegex(ConnectionError, 'before a complete response'):
                self.command(client)
            self.read_request(peer)
            self.assertIsNone(client.s)
            self.assertEqual(client._pending, 0)
            with self.assertRaisesRegex(ConnectionError, 'connection is closed'):
                self.command(client)
            self.assertEqual(peer.recv(1), b'')


if __name__ == '__main__':
    unittest.main()
