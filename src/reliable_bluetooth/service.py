from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, TypeVar

from .config import Settings
from .outbox import OutboxMessage, TextOutbox
from .transport import BleakMessageTransport, MessageTransport


logger = logging.getLogger(__name__)

SUCCESS_ACKS = {"success", "ok", "already_processed", "duplicate"}
RETRYABLE_ACKS = {"busy", "retry", "temporary_error"}
PERMANENT_ACKS = {"rejected", "invalid", "unauthorized", "unsupported", "failed"}


class ConnectionState(StrEnum):
    STOPPED = "stopped"
    CONNECTING = "connecting"
    HANDSHAKING = "handshaking"
    READY = "ready"
    RECONNECT_WAIT = "reconnect_wait"


class ServiceError(RuntimeError):
    """Base error for a failed communication session."""


class HandshakeError(ServiceError):
    """Raised when the device rejects or violates the application handshake."""


class LinkUnhealthyError(ServiceError):
    """Raised when ACK or heartbeat failures require a reconnect."""


TransportFactory = Callable[[], MessageTransport]
ResultT = TypeVar("ResultT")


class ReliableBluetoothService:
    def __init__(
        self,
        settings: Settings,
        *,
        outbox: TextOutbox | None = None,
        transport_factory: TransportFactory | None = None,
    ) -> None:
        self.settings = settings
        self.outbox = outbox or TextOutbox(
            settings.storage.directory,
            retain_acked=settings.storage.retain_acked,
        )
        self.transport_factory = transport_factory or (
            lambda: BleakMessageTransport(settings.bluetooth, settings.protocol)
        )
        self.state = ConnectionState.STOPPED
        self._stop = asyncio.Event()
        self._ack_waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._ping_waiters: dict[str, asyncio.Future[None]] = {}
        self._last_activity = 0.0
        self._transport: MessageTransport | None = None

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        self.outbox.initialize()
        recovered = self.outbox.recover_inflight()
        if recovered:
            logger.warning("Recovered %d unacknowledged message(s)", recovered)
        client_id = self.outbox.get_or_create_client_id()
        failure_count = 0
        try:
            while not self._stop.is_set():
                session_started = time.monotonic()
                transport = self.transport_factory()
                self._transport = transport
                try:
                    self._set_state(ConnectionState.CONNECTING)
                    await self._await_unless_stopped(transport.connect())
                    self._set_state(ConnectionState.HANDSHAKING)
                    session_id = await self._await_unless_stopped(
                        self._handshake(transport, client_id)
                    )
                    self._last_activity = time.monotonic()
                    self._set_state(ConnectionState.READY)
                    await self._run_ready_session(transport, session_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not self._stop.is_set():
                        logger.warning(
                            "Bluetooth session ended: %s: %s",
                            type(exc).__name__,
                            exc,
                        )
                finally:
                    self._cancel_waiters()
                    await transport.close()
                    self._transport = None
                    self.outbox.recover_inflight()

                if self._stop.is_set():
                    break
                duration = time.monotonic() - session_started
                if duration >= self.settings.reconnect.stable_reset_after:
                    failure_count = 0
                else:
                    failure_count += 1
                delay = self._reconnect_delay(failure_count)
                self._set_state(ConnectionState.RECONNECT_WAIT)
                logger.info("Reconnecting in %.2f seconds", delay)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except TimeoutError:
                    pass
        finally:
            self._set_state(ConnectionState.STOPPED)

    async def _handshake(
        self, transport: MessageTransport, client_id: str
    ) -> str:
        hello_nonce = uuid.uuid4().hex
        await transport.send_message(
            {
                "type": "hello",
                "protocol_version": self.settings.protocol.version,
                "client_id": client_id,
                "expected_device_id": self.settings.bluetooth.expected_device_id,
                "nonce": hello_nonce,
            }
        )
        try:
            response = await asyncio.wait_for(
                transport.receive(), timeout=self.settings.protocol.handshake_timeout
            )
        except TimeoutError as exc:
            raise HandshakeError("Timed out waiting for hello_ack") from exc
        if response.get("type") != "hello_ack":
            raise HandshakeError(
                f"Expected hello_ack, received {response.get('type')!r}"
            )
        if response.get("nonce") != hello_nonce:
            raise HandshakeError("hello_ack nonce does not match this connection")
        if response.get("device_id") != self.settings.bluetooth.expected_device_id:
            raise HandshakeError(
                f"Unexpected device_id {response.get('device_id')!r}"
            )
        if response.get("protocol_version") != self.settings.protocol.version:
            raise HandshakeError(
                f"Unsupported device protocol version {response.get('protocol_version')!r}"
            )
        session_id = response.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise HandshakeError("hello_ack did not contain a valid session_id")
        processed = response.get("processed_message_ids", [])
        if not isinstance(processed, list) or not all(
            isinstance(value, str) for value in processed
        ):
            raise HandshakeError("processed_message_ids must be a list of strings")
        if processed:
            marked = self.outbox.mark_processed(processed)
            logger.info("Handshake reconciled %d processed message(s)", marked)
        logger.info("Application handshake completed for session %s", session_id)
        return session_id

    async def _run_ready_session(
        self, transport: MessageTransport, session_id: str
    ) -> None:
        tasks = {
            asyncio.create_task(
                self._receiver_loop(transport), name="bluetooth-receiver"
            ): "receiver",
            asyncio.create_task(
                self._sender_loop(transport, session_id), name="bluetooth-sender"
            ): "sender",
            asyncio.create_task(
                self._heartbeat_loop(transport, session_id), name="bluetooth-heartbeat"
            ): "heartbeat",
            asyncio.create_task(
                transport.wait_disconnected(), name="bluetooth-disconnect"
            ): "disconnect",
            asyncio.create_task(self._stop.wait(), name="bluetooth-stop"): "stop",
        }
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        if any(tasks[task] == "stop" for task in done):
            return
        for task in done:
            role = tasks[task]
            if role == "disconnect":
                raise LinkUnhealthyError("BLE link disconnected")
            if task.cancelled():
                continue
            error = task.exception()
            if error is not None:
                raise error
            raise LinkUnhealthyError(f"{role} task stopped unexpectedly")

    async def _receiver_loop(self, transport: MessageTransport) -> None:
        while True:
            message = await transport.receive()
            self._last_activity = time.monotonic()
            message_type = message.get("type")
            if message_type == "ack":
                message_id = message.get("message_id")
                status = message.get("status")
                if not isinstance(message_id, str) or not isinstance(status, str):
                    logger.warning("Ignoring malformed ACK: %r", message)
                    continue
                if status in SUCCESS_ACKS:
                    self.outbox.mark_acked(message_id)
                waiter = self._ack_waiters.get(message_id)
                if waiter is not None and not waiter.done():
                    waiter.set_result(message)
            elif message_type == "pong":
                ping_id = message.get("ping_id")
                waiter = self._ping_waiters.get(ping_id)
                if waiter is not None and not waiter.done():
                    waiter.set_result(None)
            elif message_type == "ping":
                await transport.send_message(
                    {
                        "type": "pong",
                        "ping_id": message.get("ping_id"),
                        "session_id": message.get("session_id"),
                    }
                )
                self._last_activity = time.monotonic()
            elif message_type == "event":
                logger.info("Device event: %r", message)
            else:
                logger.warning("Ignoring unknown device message: %r", message)

    async def _sender_loop(
        self, transport: MessageTransport, session_id: str
    ) -> None:
        device_id = self.settings.bluetooth.expected_device_id
        while True:
            message = self.outbox.claim_next(device_id)
            if message is None:
                await asyncio.sleep(0.25)
                continue
            await self._deliver_message(transport, session_id, message)

    async def _deliver_message(
        self,
        transport: MessageTransport,
        session_id: str,
        message: OutboxMessage,
    ) -> None:
        attempts = self.settings.protocol.max_send_attempts_per_connection
        for attempt_index in range(attempts):
            if self.outbox.state_of(message.message_id) == "acked":
                logger.info("Message %s received a late ACK", message.message_id)
                return
            self.outbox.record_attempt(message)
            future: asyncio.Future[dict[str, Any]] = (
                asyncio.get_running_loop().create_future()
            )
            self._ack_waiters[message.message_id] = future
            try:
                await transport.send_message(message.wire_message(session_id))
                self._last_activity = time.monotonic()
                ack = await asyncio.wait_for(
                    asyncio.shield(future), timeout=self.settings.protocol.ack_timeout
                )
            except TimeoutError:
                if attempt_index + 1 >= attempts:
                    error = f"ACK timeout after {attempts} attempt(s)"
                    self.outbox.mark_pending(message.message_id, error)
                    raise LinkUnhealthyError(error)
                await asyncio.sleep(min(0.25 * (2**attempt_index), 2.0))
                continue
            finally:
                current = self._ack_waiters.get(message.message_id)
                if current is future:
                    self._ack_waiters.pop(message.message_id, None)
                if not future.done():
                    future.cancel()

            status = str(ack.get("status"))
            error = str(ack.get("error") or status)
            if status in SUCCESS_ACKS:
                self.outbox.mark_acked(message.message_id)
                logger.info("Message %s acknowledged", message.message_id)
                return
            if status in PERMANENT_ACKS:
                self.outbox.mark_failed(message.message_id, error)
                logger.error("Message %s rejected: %s", message.message_id, error)
                return
            if status in RETRYABLE_ACKS and attempt_index + 1 < attempts:
                await asyncio.sleep(min(0.25 * (2**attempt_index), 2.0))
                continue
            self.outbox.mark_pending(message.message_id, error)
            raise LinkUnhealthyError(
                f"Message {message.message_id} was not accepted: {error}"
            )

    async def _heartbeat_loop(
        self, transport: MessageTransport, session_id: str
    ) -> None:
        failures = 0
        interval = self.settings.protocol.heartbeat_interval
        while True:
            await asyncio.sleep(min(interval, 1.0))
            if time.monotonic() - self._last_activity < interval:
                continue
            ping_id = uuid.uuid4().hex
            future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._ping_waiters[ping_id] = future
            try:
                await transport.send_message(
                    {
                        "type": "ping",
                        "ping_id": ping_id,
                        "session_id": session_id,
                    }
                )
                await asyncio.wait_for(
                    future, timeout=self.settings.protocol.ack_timeout
                )
                self._last_activity = time.monotonic()
                failures = 0
            except TimeoutError:
                failures += 1
                if failures >= self.settings.protocol.heartbeat_failures:
                    raise LinkUnhealthyError(
                        f"Device missed {failures} consecutive heartbeat(s)"
                    )
            finally:
                self._ping_waiters.pop(ping_id, None)

    def _cancel_waiters(self) -> None:
        for future in (*self._ack_waiters.values(), *self._ping_waiters.values()):
            if not future.done():
                future.cancel()
        self._ack_waiters.clear()
        self._ping_waiters.clear()

    async def _await_unless_stopped(
        self, awaitable: Awaitable[ResultT]
    ) -> ResultT:
        operation = asyncio.ensure_future(awaitable)
        stop_waiter = asyncio.create_task(self._stop.wait())
        done, _pending = await asyncio.wait(
            (operation, stop_waiter), return_when=asyncio.FIRST_COMPLETED
        )
        if stop_waiter in done:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            raise ServiceError("Service is stopping")
        stop_waiter.cancel()
        await asyncio.gather(stop_waiter, return_exceptions=True)
        return await operation

    def _reconnect_delay(self, failure_count: int) -> float:
        reconnect = self.settings.reconnect
        base = min(
            reconnect.initial_delay * reconnect.multiplier ** max(0, failure_count - 1),
            reconnect.max_delay,
        )
        return max(0.0, base * random.uniform(1 - reconnect.jitter, 1 + reconnect.jitter))

    def _set_state(self, state: ConnectionState) -> None:
        if self.state != state:
            self.state = state
            logger.info("Connection state: %s", state)

