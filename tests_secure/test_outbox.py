import json
import tempfile
import unittest
from pathlib import Path

from reliable_bluetooth.outbox import TextOutbox


class TextOutboxTests(unittest.TestCase):
    def test_message_lifecycle_is_plain_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            outbox = TextOutbox(temporary_directory)
            message = outbox.enqueue(
                device_id="device-001", command="set_value", payload={"value": 7}
            )
            claimed = outbox.claim_next("device-001")
            self.assertIsNotNone(claimed)
            assert claimed is not None
            self.assertEqual(claimed.message_id, message.message_id)

            outbox.record_attempt(claimed)
            sending_files = list((Path(temporary_directory) / "sending").glob("*.json"))
            self.assertEqual(len(sending_files), 1)
            stored = json.loads(sending_files[0].read_text(encoding="utf-8"))
            self.assertEqual(stored["payload"], {"value": 7})
            self.assertEqual(stored["attempts"], 1)

            self.assertTrue(outbox.mark_acked(message.message_id))
            self.assertEqual(
                outbox.counts(),
                {"pending": 0, "sending": 0, "acked": 1, "failed": 0},
            )

    def test_inflight_messages_are_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            outbox = TextOutbox(temporary_directory)
            outbox.enqueue(device_id="device-001", command="work", payload={})
            outbox.claim_next("device-001")

            self.assertEqual(outbox.recover_inflight(), 1)
            self.assertEqual(outbox.counts()["pending"], 1)
            self.assertEqual(outbox.counts()["sending"], 0)

    def test_client_id_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            outbox = TextOutbox(temporary_directory)
            first = outbox.get_or_create_client_id()
            second = TextOutbox(temporary_directory).get_or_create_client_id()
            self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()

