from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from pathlib import Path
from typing import Any

from reliable_bluetooth.protocol import FrameDecoder, ProtocolError, encode_frame


logger = logging.getLogger("windows-test-peripheral")

SERVICE_UUID = uuid.UUID("0000fff0-0000-1000-8000-00805f9b34fb")
WRITE_UUID = uuid.UUID("0000fff1-0000-1000-8000-00805f9b34fb")
NOTIFY_UUID = uuid.UUID("0000fff2-0000-1000-8000-00805f9b34fb")


class TestResponder:
    def __init__(self, *, drop_first_session_ack: bool = False) -> None:
        self.drop_first_session_ack = drop_first_session_ack
        self.session_number = 0
        self.session_id = ""
        self.processed_ids: set[str] = set()
        self.drop_message_id: str | None = None

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        message_type = message.get("type")
        if message_type == "hello":
            self.session_number += 1
            self.session_id = uuid.uuid4().hex
            logger.info("Handshake session %d", self.session_number)
            return {
                "type": "hello_ack",
                "protocol_version": message.get("protocol_version"),
                "device_id": message.get("expected_device_id"),
                "session_id": self.session_id,
                "nonce": message.get("nonce"),
                # Keep this empty so the client must resend after reconnect.
                "processed_message_ids": [],
            }
        if message_type == "command":
            message_id = message.get("message_id")
            if not isinstance(message_id, str):
                return None
            duplicate = message_id in self.processed_ids
            self.processed_ids.add(message_id)
            if self.drop_first_session_ack and self.drop_message_id is None:
                self.drop_message_id = message_id
            if self.session_number == 1 and message_id == self.drop_message_id:
                logger.info("Dropping ACK for %s in first session", message_id)
                return None
            logger.info(
                "Acknowledging command %s (%s)",
                message_id,
                "duplicate" if duplicate else "new",
            )
            return {
                "type": "ack",
                "message_id": message_id,
                "status": "already_processed" if duplicate else "success",
            }
        if message_type == "ping":
            return {"type": "pong", "ping_id": message.get("ping_id")}
        logger.warning("Ignoring unsupported message: %r", message)
        return None


class WindowsTestPeripheral:
    def __init__(self, *, drop_first_session_ack: bool = False) -> None:
        self.responder = TestResponder(
            drop_first_session_ack=drop_first_session_ack
        )
        self.decoder = FrameDecoder()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.provider: Any = None
        self.write_characteristic: Any = None
        self.notify_characteristic: Any = None
        self.write_event_token: Any = None

    async def start(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("The test peripheral requires Windows")
        from winrt.windows.devices.bluetooth import BluetoothError
        from winrt.windows.devices.bluetooth.genericattributeprofile import (
            GattCharacteristicProperties,
            GattLocalCharacteristicParameters,
            GattProtectionLevel,
            GattServiceProvider,
            GattServiceProviderAdvertisementStatus,
            GattServiceProviderAdvertisingParameters,
        )

        self.loop = asyncio.get_running_loop()
        provider_result = await GattServiceProvider.create_async(SERVICE_UUID)
        if provider_result.error != BluetoothError.SUCCESS:
            raise RuntimeError(
                f"Could not create GATT service provider: {provider_result.error}"
            )
        self.provider = provider_result.service_provider

        write_parameters = GattLocalCharacteristicParameters()
        write_parameters.characteristic_properties = (
            GattCharacteristicProperties.WRITE
            | GattCharacteristicProperties.WRITE_WITHOUT_RESPONSE
        )
        write_parameters.write_protection_level = GattProtectionLevel.PLAIN
        write_result = await self.provider.service.create_characteristic_async(
            WRITE_UUID, write_parameters
        )
        if write_result.error != BluetoothError.SUCCESS:
            raise RuntimeError(
                f"Could not create write characteristic: {write_result.error}"
            )
        self.write_characteristic = write_result.characteristic

        notify_parameters = GattLocalCharacteristicParameters()
        notify_parameters.characteristic_properties = (
            GattCharacteristicProperties.NOTIFY
        )
        notify_parameters.read_protection_level = GattProtectionLevel.PLAIN
        notify_result = await self.provider.service.create_characteristic_async(
            NOTIFY_UUID, notify_parameters
        )
        if notify_result.error != BluetoothError.SUCCESS:
            raise RuntimeError(
                f"Could not create notify characteristic: {notify_result.error}"
            )
        self.notify_characteristic = notify_result.characteristic
        self.write_event_token = self.write_characteristic.add_write_requested(
            self._on_write_requested
        )

        advertising = GattServiceProviderAdvertisingParameters()
        advertising.is_connectable = True
        advertising.is_discoverable = True
        self.provider.start_advertising(advertising)
        await asyncio.sleep(1.0)
        status = self.provider.advertisement_status
        accepted_statuses = {
            GattServiceProviderAdvertisementStatus.STARTED,
            GattServiceProviderAdvertisementStatus.STARTED_WITHOUT_ALL_ADVERTISEMENT_DATA,
        }
        if status not in accepted_statuses:
            raise RuntimeError(f"GATT advertising did not start: {status}")
        logger.info("READY service_uuid=%s status=%s", SERVICE_UUID, status)

    async def stop(self) -> None:
        if self.write_characteristic is not None and self.write_event_token is not None:
            self.write_characteristic.remove_write_requested(self.write_event_token)
            self.write_event_token = None
        if self.provider is not None:
            self.provider.stop_advertising()
            self.provider = None

    def _on_write_requested(self, _sender: Any, event_args: Any) -> None:
        if self.loop is None:
            return
        deferral = event_args.get_deferral()
        asyncio.run_coroutine_threadsafe(
            self._handle_write_requested(event_args, deferral), self.loop
        )

    async def _handle_write_requested(
        self, event_args: Any, deferral: Any
    ) -> None:
        try:
            request = await event_args.get_request_async()
            if request is None:
                return
            data = bytes(memoryview(request.value))
            request.respond()
            try:
                messages = self.decoder.feed(data)
            except ProtocolError:
                logger.exception("Rejected an invalid incoming frame")
                return
            for message in messages:
                response = self.responder.handle(message)
                if response is not None:
                    await self._notify(response)
        except Exception:
            logger.exception("Failed to process GATT write")
        finally:
            deferral.complete()

    async def _notify(self, message: dict[str, Any]) -> None:
        from winrt.windows.devices.bluetooth.genericattributeprofile import (
            GattCommunicationStatus,
        )
        from winrt.windows.storage.streams import DataWriter

        frame = encode_frame(message)
        for offset in range(0, len(frame), 20):
            writer = DataWriter()
            try:
                writer.write_bytes(frame[offset : offset + 20])
                buffer = writer.detach_buffer()
                results = list(
                    await self.notify_characteristic.notify_value_async(buffer)
                )
            finally:
                writer.close()
            failed = [
                result
                for result in results
                if result.status != GattCommunicationStatus.SUCCESS
            ]
            if not results or failed:
                statuses = [str(result.status) for result in results]
                raise RuntimeError(f"GATT notification failed: {statuses}")
            await asyncio.sleep(0.01)


async def run(args: argparse.Namespace) -> None:
    peripheral = WindowsTestPeripheral(
        drop_first_session_ack=args.drop_first_session_ack
    )
    await peripheral.start()
    try:
        await asyncio.sleep(args.duration)
    finally:
        await peripheral.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="Temporary Windows BLE test peripheral")
    parser.add_argument("--duration", type=float, default=120.0)
    parser.add_argument("--drop-first-session-ack", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        logger.error("%s: %s", type(exc).__name__, exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

