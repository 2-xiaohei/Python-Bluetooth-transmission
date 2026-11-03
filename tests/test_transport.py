import sys
import types
import unittest
from unittest.mock import patch

from reliable_bluetooth.config import BluetoothSettings, ProtocolSettings
from reliable_bluetooth.protocol import encode_frame
from reliable_bluetooth.transport import (
    BleakMessageTransport,
    TransportError,
    discover_devices,
)


class FakeDevice:
    name = "Test Device"
    address = "AA:BB:CC:DD:EE:FF"


class FakeAdvertisement:
    local_name = "Test Device"
    service_uuids = ["service"]


class FakeScanner:
    @staticmethod
    async def find_device_by_address(_address: str, *, timeout: float):
        return None

    @staticmethod
    async def find_device_by_filter(callback, *, timeout: float):
        device = FakeDevice()
        if callback(device, FakeAdvertisement()):
            return device
        return None


class FakeClient:
    last_instance = None

    def __init__(
        self,
        device,
        *,
        disconnected_callback,
        timeout: float,
        pair: bool = False,
    ) -> None:
        self.device = device
        self.disconnected_callback = disconnected_callback
        self.timeout = timeout
        self.pair_requested = pair
        self.is_connected = False
        self.mtu_size = 23
        self.writes: list[bytes] = []
        self.notification_callback = None
        FakeClient.last_instance = self

    async def connect(self) -> None:
        self.is_connected = True

    async def start_notify(self, _uuid: str, callback) -> None:
        self.notification_callback = callback

    async def stop_notify(self, _uuid: str) -> None:
        return None

    async def disconnect(self) -> None:
        self.is_connected = False

    async def write_gatt_char(
        self, _uuid: str, data: bytes, *, response: bool
    ) -> None:
        self.writes.append(bytes(data))


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_pair_chunk_write_and_notification_decode(self) -> None:
        fake_bleak = types.ModuleType("bleak")
        fake_bleak.BleakClient = FakeClient
        fake_bleak.BleakScanner = FakeScanner
        bluetooth = BluetoothSettings(
            target_name="Test Device",
            service_uuid="service",
            write_characteristic_uuid="write",
            notify_characteristic_uuid="notify",
            expected_device_id="device-001",
            pair=True,
            write_chunk_size=20,
            chunk_delay=0,
        )
        protocol = ProtocolSettings()
        transport = BleakMessageTransport(bluetooth, protocol)

        with (
            patch.dict(sys.modules, {"bleak": fake_bleak}),
            patch("reliable_bluetooth.transport.sys.platform", "linux"),
        ):
            await transport.connect()
            client = FakeClient.last_instance
            assert client is not None
            self.assertTrue(client.pair_requested)

            outgoing = {"type": "command", "payload": {"value": "x" * 40}}
            await transport.send_message(outgoing)
            self.assertGreater(len(client.writes), 1)

            incoming = {"type": "ack", "message_id": "id-1", "status": "success"}
            frame = encode_frame(incoming)
            assert client.notification_callback is not None
            client.notification_callback("notify", bytearray(frame[:5]))
            client.notification_callback("notify", bytearray(frame[5:]))
            self.assertEqual(await transport.receive(), incoming)

            await transport.close()

    async def test_scan_backend_error_is_wrapped(self) -> None:
        class FailedScanner:
            @staticmethod
            async def discover(*, timeout: float, return_adv: bool):
                raise RuntimeError("Bluetooth device is turned off")

        fake_bleak = types.ModuleType("bleak")
        fake_bleak.BleakScanner = FailedScanner
        with patch.dict(sys.modules, {"bleak": fake_bleak}):
            with self.assertRaisesRegex(TransportError, "turned off"):
                await discover_devices(0.01)


if __name__ == "__main__":
    unittest.main()

