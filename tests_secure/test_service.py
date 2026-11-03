import asyncio
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from reliable_bluetooth.config import (
    AppSettings,
    BluetoothSettings,
    ProtocolSettings,
    ReconnectSettings,
    Settings,
    StorageSettings,
)
from reliable_bluetooth.outbox import TextOutbox
from reliable_bluetooth.service import ReliableBluetoothService


class FakeTransport:
    def __init__(
        self, *, disconnect_on_command: bool = False, ack_delay: float = 0.0
    ) -> None:
        self.disconnect_on_command = disconnect_on_command
        self.ack_delay = ack_delay
        self.incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.disconnected = asyncio.Event()
        self.commands: list[dict[str, Any]] = []

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def send_message(self, message: Mapping[str, Any]) -> None:
        value = dict(message)
        if value["type"] == "hello":
            self.incoming.put_nowait(
                {
                    "type": "hello_ack",
                    "protocol_version": value["protocol_version"],
                    "device_id": "device-001",
                    "session_id": "session-test",
                    "nonce": value["nonce"],
                    "processed_message_ids": [],
                }
            )
        elif value["type"] == "command":
            self.commands.append(value)
            if self.disconnect_on_command:
                self.disconnected.set()
            else:
                ack = {
                    "type": "ack",
                    "message_id": value["message_id"],
                    "status": "success",
                }
                if self.ack_delay:
                    asyncio.get_running_loop().call_later(
                        self.ack_delay, self.incoming.put_nowait, ack
                    )
                else:
                    self.incoming.put_nowait(ack)
        elif value["type"] == "ping":
            self.incoming.put_nowait(
                {"type": "pong", "ping_id": value["ping_id"]}
            )

    async def receive(self) -> dict[str, Any]:
        return await self.incoming.get()

    async def wait_disconnected(self) -> None:
        await self.disconnected.wait()


class HangingConnectTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.close_called = False

    async def connect(self) -> None:
        await asyncio.Event().wait()

    async def close(self) -> None:
        self.close_called = True


def make_settings(directory: Path, *, send_attempts: int = 1) -> Settings:
    return Settings(
        bluetooth=BluetoothSettings(
            target_name="test",
            write_characteristic_uuid="write",
            notify_characteristic_uuid="notify",
            expected_device_id="device-001",
            pair=False,
        ),
        protocol=ProtocolSettings(
            handshake_timeout=0.1,
            ack_timeout=0.05,
            max_send_attempts_per_connection=send_attempts,
            heartbeat_interval=5.0,
        ),
        reconnect=ReconnectSettings(
            initial_delay=0.01,
            max_delay=0.01,
            jitter=0.0,
            stable_reset_after=10.0,
        ),
        storage=StorageSettings(directory=directory),
        app=AppSettings(log_level="ERROR"),
    )


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_cancels_an_in_progress_connection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            transport = HangingConnectTransport()
            service = ReliableBluetoothService(
                make_settings(directory),
                outbox=TextOutbox(directory),
                transport_factory=lambda: transport,
            )
            service_task = asyncio.create_task(service.run())
            await asyncio.sleep(0.01)
            service.request_stop()
            await asyncio.wait_for(service_task, timeout=0.2)
            self.assertTrue(transport.close_called)

    async def test_disconnect_causes_reconnect_and_resend(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            settings = make_settings(directory)
            outbox = TextOutbox(directory)
            message = outbox.enqueue(
                device_id="device-001", command="set_value", payload={"value": 9}
            )
            first = FakeTransport(disconnect_on_command=True)
            second = FakeTransport()
            transports = iter((first, second))
            service = ReliableBluetoothService(
                settings,
                outbox=outbox,
                transport_factory=lambda: next(transports),
            )

            service_task = asyncio.create_task(service.run())
            try:
                for _ in range(200):
                    if outbox.counts()["acked"] == 1:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(outbox.counts()["acked"], 1)
            finally:
                service.request_stop()
                await asyncio.wait_for(service_task, timeout=1.0)

            self.assertEqual(first.commands[0]["message_id"], message.message_id)
            self.assertEqual(second.commands[0]["message_id"], message.message_id)

    async def test_late_ack_during_retry_delay_does_not_resend(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            settings = make_settings(directory, send_attempts=2)
            outbox = TextOutbox(directory)
            outbox.enqueue(device_id="device-001", command="work", payload={})
            transport = FakeTransport(ack_delay=0.08)
            service = ReliableBluetoothService(
                settings,
                outbox=outbox,
                transport_factory=lambda: transport,
            )

            service_task = asyncio.create_task(service.run())
            try:
                for _ in range(200):
                    if outbox.counts()["acked"] == 1:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(outbox.counts()["acked"], 1)
                await asyncio.sleep(0.3)
            finally:
                service.request_stop()
                await asyncio.wait_for(service_task, timeout=1.0)

            self.assertEqual(len(transport.commands), 1)


if __name__ == "__main__":
    unittest.main()

