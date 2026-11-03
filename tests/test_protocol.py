import unittest

from reliable_bluetooth.protocol import FrameDecoder, ProtocolError, encode_frame


class ProtocolTests(unittest.TestCase):
    def test_fragmented_and_concatenated_frames(self) -> None:
        first = {"type": "hello", "value": "你好"}
        second = {"type": "ack", "message_id": "abc", "status": "success"}
        stream = encode_frame(first) + encode_frame(second)
        decoder = FrameDecoder()

        messages = []
        for offset in range(0, len(stream), 3):
            messages.extend(decoder.feed(stream[offset : offset + 3]))

        self.assertEqual(messages, [first, second])

    def test_checksum_failure_clears_decoder(self) -> None:
        frame = bytearray(encode_frame({"type": "ping"}))
        frame[-1] ^= 0xFF
        decoder = FrameDecoder()

        with self.assertRaisesRegex(ProtocolError, "checksum"):
            decoder.feed(frame)

        self.assertEqual(decoder.feed(encode_frame({"type": "pong"})), [{"type": "pong"}])

    def test_frame_size_limit(self) -> None:
        with self.assertRaisesRegex(ProtocolError, "limit"):
            encode_frame({"data": "x" * 100}, max_frame_size=10)


if __name__ == "__main__":
    unittest.main()

