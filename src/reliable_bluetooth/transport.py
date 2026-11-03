from __future__ import annotations

import asyncio
import inspect
import logging
import sys
from collections.abc import Mapping
from typing import Any, Protocol

from .config import BluetoothSettings, ProtocolSettings
from .protocol import FrameDecoder, ProtocolError, encode_frame


logger = logging.getLogger(__name__)


class TransportError(RuntimeError):
    """Base error for Bluetooth transport failures."""


class DeviceNotFoundError(TransportError):
    """Raised when scanning does not find the configured device."""


class TransportDisconnectedError(TransportError):
    """Raised when an operation is attempted on a disconnected link."""


class MessageTransport(Protocol):
    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def send_message(self, message: Mapping[str, Any]) -> None: ...

    async def receive(self) -> dict[str, Any]: ...

    async def wait_disconnected(self) -> None: ...


class BleakMessageTransport:
    """BLE GATT transport that frames JSON over write/notify characteristics."""

    def __init__(
        self, bluetooth: BluetoothSettings, protocol: ProtocolSettings
    ) -> None:
        self.settings = bluetooth
        self.protocol = protocol
        self._client: Any = None
        self._decoder = FrameDecoder(
            version=protocol.version, max_frame_size=protocol.max_frame_size
        )
        self._incoming: asyncio.Queue[dict[str, Any] | BaseException] = (
            asyncio.Queue()
        )
        self._disconnected = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    async def connect(self) -> None:
        try:
            from bleak import BleakClient, BleakScanner
        except ImportError as exc:
            raise TransportError(
                "The 'bleak' package is required. Install the project with: pip install -e ."
            ) from exc

        self._loop = asyncio.get_running_loop()
        self._disconnected.clear()
        device = None
        if self.settings.target_address:
            logger.info("Scanning for Bluetooth address %s", self.settings.target_address)
            device = await BleakScanner.find_device_by_address(
                self.settings.target_address, timeout=self.settings.scan_timeout
            )
        if device is None and (
            self.settings.target_name or self.settings.service_uuid
        ):
            logger.info("Scanning for configured BLE device")

            def matches(candidate: Any, advertisement: Any) -> bool:
                if self.settings.target_name:
                    names = {
                        getattr(candidate, "name", None),
                        getattr(advertisement, "local_name", None),
                    }
                    if self.settings.target_name not in names:
                        return False
                if self.settings.service_uuid:
                    advertised = {
                        value.lower()
                        for value in (getattr(advertisement, "service_uuids", None) or [])
                    }
                    if self.settings.service_uuid.lower() not in advertised:
                        return False
                return True

            device = await BleakScanner.find_device_by_filter(
                matches, timeout=self.settings.scan_timeout
            )
        if device is None:
            raise DeviceNotFoundError("Configured BLE device was not found")

        logger.info(
            "Connecting to %s (%s)",
            getattr(device, "name", None) or "unnamed device",
            getattr(device, "address", "unknown address"),
        )
        pair_during_connect = (
            self.settings.pair
            and sys.platform != "darwin"
            and "pair" in inspect.signature(BleakClient).parameters
        )
        client_options: dict[str, Any] = {
            "disconnected_callback": self._on_disconnected,
            "timeout": self.settings.connect_timeout,
        }
        if pair_during_connect:
            client_options["pair"] = True
            logger.info("Requesting operating-system pairing during BLE connection")
        self._client = BleakClient(device, **client_options)
        await self._client.connect()
        if not self._client.is_connected:
            raise TransportError("BLE client did not enter the connected state")

        if self.settings.pair and sys.platform != "darwin":
            if not pair_during_connect:
                pair_method = getattr(self._client, "pair", None)
                if pair_method is None:
                    raise TransportError("This Bleak backend does not support pairing")
                logger.info("Requesting operating-system Bluetooth pairing")
                paired = await pair_method()
                if paired is False:
                    raise TransportError("Operating-system Bluetooth pairing failed")
        elif self.settings.pair:
            logger.info(
                "macOS performs pairing automatically when a protected characteristic is accessed"
            )

        await self._client.start_notify(
            self.settings.notify_characteristic_uuid, self._on_notification
        )
        logger.info("BLE notification subscription is active")

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            if client.is_connected:
                try:
                    await asyncio.wait_for(
                        client.stop_notify(
                            self.settings.notify_characteristic_uuid
                        ),
                        timeout=5.0,
                    )
                except Exception:
                    logger.debug("Could not stop BLE notifications", exc_info=True)
                try:
                    await asyncio.wait_for(client.disconnect(), timeout=5.0)
                except Exception:
                    logger.debug("Could not disconnect BLE client", exc_info=True)
        except Exception:
            logger.debug("Error while closing BLE transport", exc_info=True)
        finally:
            self._disconnected.set()

    async def send_message(self, message: Mapping[str, Any]) -> None:
        client = self._client
        if client is None or not client.is_connected:
            raise TransportDisconnectedError("BLE link is not connected")
        frame = encode_frame(
            message,
            version=self.protocol.version,
            max_frame_size=self.protocol.max_frame_size,
        )
        mtu_size = getattr(client, "mtu_size", 23) or 23
        chunk_size = min(self.settings.write_chunk_size, max(20, mtu_size - 3))
        async with self._send_lock:
            for offset in range(0, len(frame), chunk_size):
                if not client.is_connected:
                    raise TransportDisconnectedError(
                        "BLE link disconnected during a write"
                    )
                await client.write_gatt_char(
                    self.settings.write_characteristic_uuid,
                    frame[offset : offset + chunk_size],
                    response=self.settings.write_with_response,
                )
                if self.settings.chunk_delay and offset + chunk_size < len(frame):
                    await asyncio.sleep(self.settings.chunk_delay)

    async def receive(self) -> dict[str, Any]:
        item = await self._incoming.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def wait_disconnected(self) -> None:
        await self._disconnected.wait()

    def _on_disconnected(self, _client: Any) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._disconnected.set)

    def _on_notification(self, _sender: Any, data: bytearray) -> None:
        if self._loop is None:
            return
        try:
            messages: list[dict[str, Any] | BaseException] = self._decoder.feed(data)
        except ProtocolError as exc:
            messages = [exc]
        for message in messages:
            self._loop.call_soon_threadsafe(self._incoming.put_nowait, message)


async def discover_devices(timeout: float = 10.0) -> list[dict[str, Any]]:
    """Return JSON-serializable BLE scan results."""
    try:
        from bleak import BleakScanner
    except ImportError as exc:
        raise TransportError(
            "The 'bleak' package is required. Install the project with: pip install -e ."
        ) from exc

    try:
        discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
    except Exception as exc:
        raise TransportError(f"BLE scan failed: {exc}") from exc
    results: list[dict[str, Any]] = []
    values = discovered.values() if isinstance(discovered, dict) else discovered
    for item in values:
        if isinstance(item, tuple):
            device, advertisement = item
        else:
            device, advertisement = item, None
        manufacturer_data = getattr(advertisement, "manufacturer_data", {}) or {}
        results.append(
            {
                "name": getattr(device, "name", None)
                or getattr(advertisement, "local_name", None),
                "address": getattr(device, "address", None),
                "rssi": getattr(advertisement, "rssi", None),
                "service_uuids": getattr(advertisement, "service_uuids", None) or [],
                "manufacturer_data": {
                    str(key): bytes(value).hex()
                    for key, value in manufacturer_data.items()
                },
            }
        )
    results.sort(key=lambda value: (value["name"] or "", value["address"] or ""))
    return results

