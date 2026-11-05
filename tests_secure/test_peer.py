import asyncio
import json
import sys
import tempfile
import types
import unittest
from enum import IntFlag
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from reliable_bluetooth.cli import main
from reliable_bluetooth.peer import (
    SERVICE_UUID,
    CentralPeerEndpoint,
    ConsoleInput,
    PeerApplication,
    PeerError,
    PeerSession,
    PeerStore,
    PeripheralPeerEndpoint,
)
from reliable_bluetooth.protocol import encode_frame
from reliable_bluetooth.security import SECURE_ENVELOPE_TYPE


class PeerSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_trust_reconnects_without_pairing_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store_a = PeerStore(root / "a.json", local_id="a")
            store_b = PeerStore(root / "b.json", local_id="b")

            async def pair_once(pair_code_a, pair_code_b, first_store, second_store):
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
                    pair_code=pair_code_a,
                    store=first_store,
                    send_message=send_a,
                )
                session_b = PeerSession(
                    local_id="b",
                    local_name="peer-b",
                    pair_code=pair_code_b,
                    store=second_store,
                    send_message=send_b,
                )
                await session_a.initiate_handshake()
                return session_a, session_b

            await pair_once("123456", "123456", store_a, store_b)
            self.assertIsNotNone(store_a.secret_for("b"))
            self.assertIsNotNone(store_b.secret_for("a"))

            reloaded_a = PeerStore(root / "a.json")
            reloaded_b = PeerStore(root / "b.json")
            session_a, session_b = await pair_once(
                None, None, reloaded_a, reloaded_b
            )
            self.assertTrue(session_a.connected.is_set())
            self.assertTrue(session_b.connected.is_set())

    async def test_incorrect_pairing_code_is_rejected(self) -> None:
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
        )
        session_b = PeerSession(
            local_id="b",
            local_name="peer-b",
            pair_code="654321",
            send_message=send_b,
        )

        with self.assertRaisesRegex(PeerError, "Incorrect pairing code"):
            await session_a.initiate_handshake()

    async def test_symmetric_handshake_send_ack_and_deduplication(self) -> None:
        received_by_b: list[tuple[str, str]] = []
        wire_a: list[dict] = []
        wire_b: list[dict] = []
        session_a = None
        session_b = None

        async def send_a(message):
            assert session_b is not None
            wire_a.append(message)
            await session_b.handle_message(message)

        async def send_b(message):
            assert session_a is not None
            wire_b.append(message)
            await session_a.handle_message(message)

        session_a = PeerSession(
            local_id="a",
            local_name="peer-a",
            pair_code="123456",
            send_message=send_a,
        )
        session_b = PeerSession(
            local_id="b",
            local_name="peer-b",
            pair_code="123456",
            send_message=send_b,
            on_text=lambda name, text: received_by_b.append((name, text)),
        )

        await session_a.initiate_handshake()
        self.assertTrue(session_a.connected.is_set())
        self.assertTrue(session_b.connected.is_set())
        self.assertEqual(session_a.peer_name, "peer-b")
        self.assertEqual(session_b.peer_name, "peer-a")
        wire_a.clear()
        wire_b.clear()

        result = await session_a.send_text("message-1", "hello")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(received_by_b, [("peer-a", "hello")])
        self.assertTrue(wire_a)
        self.assertTrue(wire_b)
        self.assertTrue(
            all(message.get("type") == SECURE_ENVELOPE_TYPE for message in wire_a + wire_b)
        )
        self.assertNotIn("hello", json.dumps(wire_a + wire_b))

        result = await session_a.send_text("message-1", "hello")
        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(received_by_b, [("peer-a", "hello")])

    async def test_missing_first_ack_retries_same_message_id(self) -> None:
        received: list[str] = []
        sent_ids: list[str] = []
        dropped_first_ack = True
        session_a = None
        session_b = None

        async def send_a(message):
            nonlocal sent_ids
            assert session_b is not None
            if message.get("type") == SECURE_ENVELOPE_TYPE:
                sent_ids.append("encrypted")
            await session_b.handle_message(message)

        async def send_b(message):
            nonlocal dropped_first_ack
            assert session_a is not None
            if (
                message.get("type") == SECURE_ENVELOPE_TYPE
                and dropped_first_ack
            ):
                dropped_first_ack = False
                return
            await session_a.handle_message(message)

        session_a = PeerSession(
            local_id="a",
            local_name="peer-a",
            pair_code="123456",
            send_message=send_a,
            ack_timeout=0.01,
        )
        session_b = PeerSession(
            local_id="b",
            local_name="peer-b",
            pair_code="123456",
            send_message=send_b,
            on_text=lambda _name, text: received.append(text),
        )
        await session_a.initiate_handshake()

        result = await session_a.send_text("fixed-id", "once")
        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(sent_ids, ["encrypted", "encrypted"])
        self.assertEqual(received, ["once"])

    async def test_encrypted_reconnect_request_discards_peer_session(self) -> None:
        session_a = None
        session_b = None
        wire_a: list[dict] = []

        async def send_a(message):
            assert session_b is not None
            wire_a.append(message)
            await session_b.handle_message(message)

        async def send_b(message):
            assert session_a is not None
            await session_a.handle_message(message)

        session_a = PeerSession(
            local_id="a",
            local_name="peer-a",
            pair_code="123456",
            send_message=send_a,
        )
        session_b = PeerSession(
            local_id="b",
            local_name="peer-b",
            pair_code="123456",
            send_message=send_b,
        )
        await session_a.initiate_handshake()
        wire_a.clear()

        await session_a.request_reconnect("message ACK timed out")

        self.assertFalse(session_b.connected.is_set())
        self.assertTrue(session_b.ended.is_set())
        self.assertEqual(len(wire_a), 1)
        self.assertEqual(wire_a[0].get("type"), SECURE_ENVELOPE_TYPE)
        self.assertNotIn("message ACK timed out", json.dumps(wire_a))

    async def test_message_deduplication_survives_reconnection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            store_a = PeerStore(root / "a.json", local_id="a")
            store_b = PeerStore(root / "b.json", local_id="b")
            received: list[str] = []

            async def connect(first_store, second_store, pair_code):
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
                    pair_code=pair_code,
                    store=first_store,
                    send_message=send_a,
                )
                session_b = PeerSession(
                    local_id="b",
                    local_name="peer-b",
                    pair_code=pair_code,
                    store=second_store,
                    send_message=send_b,
                    on_text=lambda _name, text: received.append(text),
                )
                await session_a.initiate_handshake()
                return session_a

            first = await connect(store_a, store_b, "123456")
            first_result = await first.send_text("persistent-id", "once")
            self.assertEqual(first_result["status"], "success")

            second = await connect(
                PeerStore(root / "a.json"),
                PeerStore(root / "b.json"),
                None,
            )
            second_result = await second.send_text("persistent-id", "once")

            self.assertEqual(second_result["status"], "duplicate")
            self.assertEqual(received, ["once"])

    async def test_text_processing_failure_returns_explicit_failure(self) -> None:
        session_a = None
        session_b = None

        async def send_a(message):
            assert session_b is not None
            await session_b.handle_message(message)

        async def send_b(message):
            assert session_a is not None
            await session_a.handle_message(message)

        def fail(_name, _text):
            raise RuntimeError("receiver could not store message")

        session_a = PeerSession(
            local_id="a",
            local_name="peer-a",
            pair_code="123456",
            send_message=send_a,
        )
        session_b = PeerSession(
            local_id="b",
            local_name="peer-b",
            pair_code="123456",
            send_message=send_b,
            on_text=fail,
        )
        await session_a.initiate_handshake()

        with self.assertRaisesRegex(PeerError, "receiver could not store"):
            await session_a.send_text("failed-message", "hello")

    async def test_rejected_duplicate_does_not_overwrite_saved_trust(self) -> None:
        store_a = PeerStore(None, local_id="a")
        store_b = PeerStore(None, local_id="b")
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
            store=store_a,
            send_message=send_a,
        )
        session_b = PeerSession(
            local_id="b",
            local_name="peer-b",
            pair_code="123456",
            store=store_b,
            send_message=send_b,
            on_ready=lambda _session: False,
        )

        with self.assertRaisesRegex(PeerError, "superseded"):
            await session_a.initiate_handshake()
        self.assertIsNone(store_b.secret_for("a"))


class PeerDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_scan_retries_until_peer_appears(self) -> None:
        device = SimpleNamespace(name="peer-b", address="peer-address")
        advertisement = SimpleNamespace(
            service_uuids=[SERVICE_UUID],
            local_name="peer-b",
            rssi=-40,
        )

        class FakeScanner:
            calls = 0

            @classmethod
            async def discover(cls, *, timeout: float, return_adv: bool):
                cls.calls += 1
                if cls.calls == 1:
                    return {}
                return {"peer-address": (device, advertisement)}

        fake_bleak = types.ModuleType("bleak")
        fake_bleak.BleakScanner = FakeScanner
        endpoint = CentralPeerEndpoint(
            PeerApplication(),
            name="peer-a",
            peer_id="a",
            store=PeerStore(None, local_id="a"),
        )
        with (
            patch.dict(sys.modules, {"bleak": fake_bleak}),
            patch("reliable_bluetooth.peer.asyncio.sleep", new=AsyncMock()),
        ):
            selected = await endpoint.choose_device(scan_timeout=0.01, auto=True)

        self.assertIs(selected, device)
        self.assertEqual(FakeScanner.calls, 2)

    async def test_incoming_pairing_cancels_selection_without_losing_input(
        self,
    ) -> None:
        device = SimpleNamespace(name="peer-b", address="peer-address")
        advertisement = SimpleNamespace(
            service_uuids=[SERVICE_UUID],
            local_name="peer-b",
            rssi=-40,
        )

        class FakeScanner:
            @classmethod
            async def discover(cls, *, timeout: float, return_adv: bool):
                return {"peer-address": (device, advertisement)}

        fake_bleak = types.ModuleType("bleak")
        fake_bleak.BleakScanner = FakeScanner
        console = ConsoleInput()
        app = PeerApplication("a", console=console)
        endpoint = CentralPeerEndpoint(
            app,
            name="peer-a",
            peer_id="a",
            store=PeerStore(None, local_id="a"),
            console=console,
        )
        with patch.dict(sys.modules, {"bleak": fake_bleak}):
            choosing = asyncio.create_task(
                endpoint.choose_device(scan_timeout=0.01, auto=False)
            )
            for _ in range(5):
                await asyncio.sleep(0)
            incoming = PeerSession(
                local_id="a",
                local_name="peer-a",
                pair_code="123456",
                send_message=AsyncMock(),
                direction="incoming",
            )
            incoming.peer_id = "b"
            incoming.peer_name = "peer-b"
            self.assertTrue(app.set_session(incoming))
            self.assertIsNone(await choosing)

        console.feed_line("next-chat-line")
        self.assertEqual(await console.read(), "next-chat-line")


class PeerCoordinationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def session(local_id: str, peer_id: str, direction: str) -> PeerSession:
        session = PeerSession(
            local_id=local_id,
            local_name=f"peer-{local_id}",
            pair_code="123456",
            send_message=AsyncMock(),
            direction=direction,
        )
        session.peer_id = peer_id
        session.peer_name = f"peer-{peer_id}"
        return session

    async def test_simultaneous_connections_converge_on_same_direction(self) -> None:
        app_a = PeerApplication("a")
        app_b = PeerApplication("b")
        a_incoming = self.session("a", "b", "incoming")
        b_outgoing = self.session("b", "a", "outgoing")
        self.assertTrue(app_a.set_session(a_incoming))
        self.assertTrue(app_b.set_session(b_outgoing))

        a_outgoing = self.session("a", "b", "outgoing")
        b_incoming = self.session("b", "a", "incoming")
        self.assertTrue(app_a.set_session(a_outgoing))
        self.assertTrue(app_b.set_session(b_incoming))

        self.assertIs(app_a.session, a_outgoing)
        self.assertIs(app_b.session, b_incoming)
        self.assertTrue(a_incoming.ended.is_set())
        self.assertTrue(b_outgoing.ended.is_set())

    async def test_cancelled_console_consumer_does_not_take_next_line(self) -> None:
        console = ConsoleInput()
        waiting = asyncio.create_task(console.read())
        await asyncio.sleep(0)
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)

        console.feed_line("hello")
        self.assertEqual(await console.read(), "hello")

    async def test_interactive_console_uses_redrawing_prompt(self) -> None:
        console = ConsoleInput()
        prompt_session = SimpleNamespace(prompt_async=AsyncMock(return_value="hello"))
        console._prompt_session = prompt_session
        console._prompt_lock = asyncio.Lock()

        self.assertEqual(await console.read("\033[36m> \033[0m"), "hello")
        prompt_session.prompt_async.assert_awaited_once()
        formatted_prompt = prompt_session.prompt_async.await_args.args[0]
        self.assertEqual(formatted_prompt.value, "\033[36m> \033[0m")

    async def test_unacknowledged_text_waits_for_fresh_session(self) -> None:
        app = PeerApplication("a")
        first = self.session("a", "b", "outgoing")
        first.on_reset = app.clear_session
        first.connected.set()
        first.accepted = True
        first.send_text = AsyncMock(
            side_effect=PeerError("Message fixed-id was not acknowledged")
        )
        first.request_reconnect = AsyncMock()
        self.assertTrue(app.set_session(first))

        sender = asyncio.create_task(app.sender_loop())
        sending = asyncio.create_task(app.send("hello after reconnect"))
        try:
            await asyncio.wait_for(first.ended.wait(), timeout=1.0)
            for _ in range(10):
                if app.session is None:
                    break
                await asyncio.sleep(0)
            self.assertIsNone(app.session)
            first.request_reconnect.assert_awaited_once()

            replacement = self.session("a", "b", "outgoing")
            replacement.connected.set()
            replacement.accepted = True
            observed: list[tuple[str, str]] = []

            async def deliver(message_id: str, text: str) -> dict:
                observed.append((message_id, text))
                return {
                    "action": "text",
                    "message_id": message_id,
                    "status": "success",
                    "attempts": 1,
                }

            replacement.send_text = AsyncMock(side_effect=deliver)
            self.assertTrue(app.set_session(replacement))

            result = await asyncio.wait_for(sending, timeout=1.0)
            self.assertEqual(result["status"], "success")
            self.assertEqual(len(observed), 1)
            self.assertEqual(observed[0][1], "hello after reconnect")
            first_id = first.send_text.await_args.args[0]
            self.assertEqual(observed[0][0], first_id)
        finally:
            app.stop.set()
            sender.cancel()
            sending.cancel()
            await asyncio.gather(sender, sending, return_exceptions=True)

    async def test_peripheral_keeps_state_until_handshake_confirmation(self) -> None:
        app = PeerApplication("b")
        endpoint = PeripheralPeerEndpoint(
            app,
            name="peer-b",
            peer_id="b",
            pair_code="123456",
            store=PeerStore(None, local_id="b"),
        )
        initiator = None

        async def send_to_peripheral(message):
            frame = encode_frame(message)
            for offset in range(0, len(frame), 7):
                await endpoint._handle_write(frame[offset : offset + 7])

        async def send_to_initiator(message):
            assert initiator is not None
            await initiator.handle_message(message)

        endpoint.session.send_message = send_to_initiator
        initiator = PeerSession(
            local_id="a",
            local_name="peer-a",
            pair_code=None,
            send_message=send_to_peripheral,
        )

        with self.assertRaisesRegex(PeerError, "requires a pairing code"):
            await initiator.initiate_handshake()

        initiator.pair_code = "123456"
        await initiator.initiate_handshake()

        self.assertTrue(initiator.connected.is_set())
        self.assertTrue(endpoint.session.connected.is_set())
        self.assertIs(app.session, endpoint.session)

    async def test_peripheral_endpoint_uses_cross_platform_server(self) -> None:
        class Properties(IntFlag):
            write_without_response = 1
            write = 2
            notify = 4

        class Permissions(IntFlag):
            readable = 1
            writeable = 2

        class FakeCharacteristic:
            value = bytearray()

        class FakeServer:
            instance = None

            def __init__(self, name, loop=None):
                self.name = name
                self.loop = loop
                self.added = []
                self.started = False
                self.characteristic = FakeCharacteristic()
                FakeServer.instance = self

            async def add_new_service(self, uuid):
                self.added.append(("service", uuid))

            async def add_new_characteristic(self, *args):
                self.added.append(("characteristic", *args))

            def get_characteristic(self, _uuid):
                return self.characteristic

            async def start(self, **_kwargs):
                self.started = True

            async def stop(self):
                self.started = False

            async def is_advertising(self):
                return self.started

            async def is_connected(self):
                return False

            def update_value(self, _service_uuid, _characteristic_uuid):
                return True

        fake_bless = types.ModuleType("bless")
        fake_bless.BlessServer = FakeServer
        fake_bless.GATTCharacteristicProperties = Properties
        fake_bless.GATTAttributePermissions = Permissions
        endpoint = PeripheralPeerEndpoint(
            PeerApplication("a"),
            name="peer-a",
            peer_id="a",
            pair_code="123456",
            store=PeerStore(None, local_id="a"),
        )
        with (
            patch.dict(sys.modules, {"bless": fake_bless}),
            patch("reliable_bluetooth.peer.sys.platform", "darwin"),
        ):
            await endpoint.start()
            self.assertTrue(FakeServer.instance.started)
            self.assertEqual(len(FakeServer.instance.added), 3)
            await endpoint.stop()


class PeerCliTests(unittest.TestCase):
    def test_peer_command_does_not_require_config_file(self) -> None:
        with patch("reliable_bluetooth.cli.run_peer", new=AsyncMock(return_value=0)):
            self.assertEqual(main(["peer", "--auto", "--send", "hello"]), 0)


if __name__ == "__main__":
    unittest.main()

