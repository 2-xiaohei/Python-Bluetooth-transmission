import unittest

from tools.windows_test_peripheral import TestResponder


class TestResponderTests(unittest.TestCase):
    def test_first_session_drops_ack_then_duplicate_is_accepted(self) -> None:
        responder = TestResponder(drop_first_session_ack=True)
        hello = {
            "type": "hello",
            "protocol_version": 1,
            "expected_device_id": "windows-test-device",
            "nonce": "nonce-1",
        }
        hello_ack = responder.handle(hello)
        assert hello_ack is not None
        self.assertEqual(hello_ack["nonce"], "nonce-1")

        command = {"type": "command", "message_id": "message-1"}
        self.assertIsNone(responder.handle(command))
        self.assertIsNone(responder.handle(command))

        responder.handle({**hello, "nonce": "nonce-2"})
        ack = responder.handle(command)
        self.assertEqual(
            ack,
            {
                "type": "ack",
                "message_id": "message-1",
                "status": "already_processed",
            },
        )


if __name__ == "__main__":
    unittest.main()

