import base64
import json
import unittest

from reliable_bluetooth.security import (
    EphemeralKey,
    IdentityKey,
    ReplayError,
    SecurityError,
    derive_session_channel,
    derive_trust_secret,
)


class KeyAgreementTests(unittest.TestCase):
    def test_first_pairing_derives_same_trust_secret(self) -> None:
        first = IdentityKey.generate()
        second = IdentityKey.generate()

        first_secret = derive_trust_secret(
            first,
            peer_public_text=second.public_text,
            pair_code="123456",
            local_id="a",
            peer_id="b",
        )
        second_secret = derive_trust_secret(
            second,
            peer_public_text=first.public_text,
            pair_code="123456",
            local_id="b",
            peer_id="a",
        )

        self.assertEqual(first_secret, second_secret)
        self.assertEqual(len(bytes.fromhex(first_secret)), 32)


class SecureChannelTests(unittest.TestCase):
    @staticmethod
    def channel_pair():
        first = EphemeralKey.generate()
        second = EphemeralKey.generate()
        secret = "42" * 32
        nonce = "12" * 16
        first_channel = derive_session_channel(
            first,
            peer_ephemeral_public=second.public_text,
            trust_secret=secret,
            nonce=nonce,
            local_id="a",
            peer_id="b",
        )
        second_channel = derive_session_channel(
            second,
            peer_ephemeral_public=first.public_text,
            trust_secret=secret,
            nonce=nonce,
            local_id="b",
            peer_id="a",
        )
        return first_channel, second_channel

    def test_text_and_file_payloads_are_not_visible_on_wire(self) -> None:
        sender, receiver = self.channel_pair()
        payloads = [
            {
                "type": "peer_message",
                "message_id": "message-1",
                "text": "confidential text",
            },
            {
                "type": "peer_file_chunk",
                "transfer_id": "transfer-1",
                "file_name": "secret-report.txt",
                "data": base64.b64encode(b"private file bytes").decode("ascii"),
            },
        ]

        for payload in payloads:
            envelope = sender.encrypt(payload)
            wire_text = json.dumps(envelope)
            self.assertNotIn("confidential text", wire_text)
            self.assertNotIn("secret-report.txt", wire_text)
            self.assertNotIn("private file bytes", wire_text)
            self.assertEqual(receiver.decrypt(envelope), payload)

    def test_tampering_and_replay_are_rejected(self) -> None:
        sender, receiver = self.channel_pair()
        envelope = sender.encrypt({"type": "peer_message", "text": "hello"})
        tampered = dict(envelope)
        ciphertext = bytearray(base64.b64decode(tampered["ciphertext"]))
        ciphertext[-1] ^= 1
        tampered["ciphertext"] = base64.b64encode(ciphertext).decode("ascii")

        with self.assertRaisesRegex(SecurityError, "authentication"):
            receiver.decrypt(tampered)

        self.assertEqual(receiver.decrypt(envelope)["text"], "hello")
        with self.assertRaises(ReplayError):
            receiver.decrypt(envelope)


if __name__ == "__main__":
    unittest.main()
