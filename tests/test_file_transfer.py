import asyncio
import hashlib
import tempfile
import unittest
import uuid
from pathlib import Path

from reliable_bluetooth.file_transfer import (
    FILE_CHUNK_SIZE,
    MAX_FILE_SIZE,
    FileTransferError,
    IncomingFileStore,
    validate_outgoing_file,
)
from reliable_bluetooth.peer import OutgoingFile, PeerApplication, PeerSession


class IncomingFileStoreTests(unittest.TestCase):
    def test_accept_persist_resume_deduplicate_and_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            payload = b"first block" + b"second block"
            transfer_id = str(uuid.uuid4())
            offers = []
            store = IncomingFileStore(root, on_offer=offers.append)

            response = store.offer(
                peer_id="peer-a",
                transfer_id=transfer_id,
                file_name="../report.txt",
                size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            )
            self.assertEqual(response["status"], "pending")
            self.assertEqual(offers[0].file_name, "report.txt")
            store.accept(transfer_id[:8])
            first = payload[:11]
            self.assertEqual(
                store.write_chunk(
                    peer_id="peer-a",
                    transfer_id=transfer_id,
                    offset=0,
                    data=first,
                ),
                len(first),
            )
            self.assertEqual(
                store.write_chunk(
                    peer_id="peer-a",
                    transfer_id=transfer_id,
                    offset=0,
                    data=first,
                ),
                len(first),
            )

            reloaded = IncomingFileStore(root)
            response = reloaded.offer(
                peer_id="peer-a",
                transfer_id=transfer_id,
                file_name="report.txt",
                size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            )
            self.assertEqual(response["status"], "accepted")
            self.assertEqual(response["next_offset"], len(first))
            reloaded.write_chunk(
                peer_id="peer-a",
                transfer_id=transfer_id,
                offset=len(first),
                data=payload[len(first) :],
            )
            final_path = reloaded.complete(
                peer_id="peer-a", transfer_id=transfer_id
            )
            self.assertEqual(final_path.read_bytes(), payload)
            self.assertTrue((root / ".transfers" / f"{transfer_id}.json").is_file())

    def test_reject_and_ten_mebibyte_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store = IncomingFileStore(root)
            transfer_id = str(uuid.uuid4())
            with self.assertRaisesRegex(FileTransferError, "10 MiB"):
                store.offer(
                    peer_id="peer-a",
                    transfer_id=transfer_id,
                    file_name="too-large.bin",
                    size=MAX_FILE_SIZE + 1,
                    sha256="0" * 64,
                )

            large_file = root / "too-large.bin"
            with large_file.open("wb") as handle:
                handle.truncate(MAX_FILE_SIZE + 1)
            with self.assertRaisesRegex(FileTransferError, "10 MiB"):
                validate_outgoing_file(large_file)

    def test_checksum_failure_resets_resume_offset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            transfer_id = str(uuid.uuid4())
            store = IncomingFileStore(root)
            store.offer(
                peer_id="peer-a",
                transfer_id=transfer_id,
                file_name="bad.bin",
                size=3,
                sha256=hashlib.sha256(b"good").hexdigest(),
            )
            store.accept(transfer_id)
            store.write_chunk(
                peer_id="peer-a",
                transfer_id=transfer_id,
                offset=0,
                data=b"bad",
            )
            with self.assertRaisesRegex(FileTransferError, "SHA-256"):
                store.complete(peer_id="peer-a", transfer_id=transfer_id)
            response = store.offer(
                peer_id="peer-a",
                transfer_id=transfer_id,
                file_name="bad.bin",
                size=3,
                sha256=hashlib.sha256(b"good").hexdigest(),
            )
            self.assertEqual(response["next_offset"], 0)


class FileProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_file_ack_is_matched_by_unique_request_id(self) -> None:
        observed_request_ids = []
        session = None

        async def send(message):
            assert session is not None
            request_id = message["request_id"]
            observed_request_ids.append(request_id)
            await session.handle_message(
                {
                    "type": "peer_file_offer_ack",
                    "transfer_id": message["transfer_id"],
                    "request_id": "late-ack-for-another-action",
                    "status": "accepted",
                    "next_offset": 99,
                }
            )
            await session.handle_message(
                {
                    "type": "peer_file_offer_ack",
                    "transfer_id": message["transfer_id"],
                    "request_id": request_id,
                    "status": "accepted",
                    "next_offset": 0,
                }
            )

        session = PeerSession(
            local_id="a",
            local_name="peer-a",
            pair_code="123456",
            send_message=send,
        )
        session.connected.set()

        response = await session.offer_file(
            transfer_id=str(uuid.uuid4()),
            file_name="report.txt",
            size=10,
            sha256="0" * 64,
        )

        self.assertEqual(response["next_offset"], 0)
        self.assertEqual(response["attempts"], 1)
        self.assertEqual(response["request_id"], observed_request_ids[0])

    async def test_file_protocol_transfers_multiple_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source.bin"
            payload = bytes(range(256)) * 40
            source.write_bytes(payload)
            incoming = None

            def accept_offer(transfer):
                assert incoming is not None
                incoming.accept(transfer.transfer_id)

            incoming = IncomingFileStore(
                root / "received", on_offer=accept_offer
            )
            app = PeerApplication("a")
            session_a = None
            session_b = None

            async def send_a(message):
                assert session_b is not None
                await session_b.handle_message(message)

            async def send_b(message):
                assert session_a is not None
                await session_a.handle_message(message)

            session_a = PeerSession(
                local_id="a",
                local_name="peer-a",
                pair_code="123456",
                send_message=send_a,
                on_ready=app.set_session,
            )
            session_b = PeerSession(
                local_id="b",
                local_name="peer-b",
                pair_code="123456",
                send_message=send_b,
                incoming_files=incoming,
            )
            await session_a.initiate_handshake()
            completed = asyncio.get_running_loop().create_future()
            item = OutgoingFile(
                transfer_id=str(uuid.uuid4()),
                path=source,
                file_name=source.name,
                size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                modified_ns=source.stat().st_mtime_ns,
                completed=completed,
            )

            response = await app._send_file_on_session(item, session_a)

            self.assertEqual(response["status"], "complete")
            self.assertEqual((root / "received" / "source.bin").read_bytes(), payload)
            self.assertGreater(len(payload), FILE_CHUNK_SIZE * 2)


if __name__ == "__main__":
    unittest.main()
