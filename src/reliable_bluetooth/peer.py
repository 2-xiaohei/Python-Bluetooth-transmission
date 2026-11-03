from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import socket
import sys
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .file_transfer import (
    FILE_CHUNK_SIZE,
    FileTransferError,
    IncomingFileStore,
    IncomingTransfer,
    sha256_file,
    validate_outgoing_file,
)
from .protocol import FrameDecoder, ProtocolError, encode_frame
from .terminal import panel, progress, prompt, status, style


logger = logging.getLogger(__name__)

SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000fff1-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"
MAX_REMEMBERED_MESSAGE_IDS = 2_048


class PeerError(RuntimeError):
    """Raised when a peer session cannot continue."""


class ActionRejected(PeerError):
    """Raised when the peer explicitly reports that an action failed."""


class PairingRequired(PeerError):
    """Raised when the selected peer has not been trusted before."""


class PairingRejected(PeerError):
    """Raised when a first-time pairing code or saved trust proof is invalid."""


class PeerStore:
    """Persist peer identity and derived shared secrets as UTF-8 JSON."""

    def __init__(self, path: str | Path | None, *, local_id: str | None = None):
        self.path = Path(path).expanduser().resolve() if path is not None else None
        self._data: dict[str, Any] = {}
        if self.path is not None and self.path.exists():
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise PeerError(f"Cannot read peer store {self.path}: {exc}") from exc
            if not isinstance(value, dict):
                raise PeerError(f"Peer store {self.path} must contain a JSON object")
            self._data = value
        stored_local_id = self._data.get("local_id")
        if not isinstance(stored_local_id, str) or not stored_local_id:
            stored_local_id = local_id or str(uuid.uuid4())
            self._data["local_id"] = stored_local_id
        self.local_id = stored_local_id
        trusted = self._data.get("trusted_peers")
        if not isinstance(trusted, dict):
            self._data["trusted_peers"] = {}
        self._save()

    @property
    def has_trusted_peers(self) -> bool:
        return bool(self._data["trusted_peers"])

    def secret_for(self, peer_id: str) -> str | None:
        record = self._data["trusted_peers"].get(peer_id)
        if not isinstance(record, dict):
            return None
        secret = record.get("secret")
        return secret if isinstance(secret, str) and secret else None

    def authentication_proofs(self, sender_id: str, nonce: str) -> dict[str, str]:
        proofs: dict[str, str] = {}
        for peer_id, record in self._data["trusted_peers"].items():
            if not isinstance(peer_id, str) or not isinstance(record, dict):
                continue
            secret = record.get("secret")
            if isinstance(secret, str) and secret:
                proofs[peer_id] = _trusted_proof(
                    secret, sender_id, peer_id, nonce
                )
        return proofs

    def trust(self, peer_id: str, peer_name: str, secret: str) -> None:
        current = self._data["trusted_peers"].get(peer_id)
        received_message_ids = (
            current.get("received_message_ids", [])
            if isinstance(current, dict)
            else []
        )
        self._data["trusted_peers"][peer_id] = {
            "name": peer_name,
            "secret": secret,
            "paired_at": time.time(),
            "received_message_ids": received_message_ids,
        }
        self._save()

    def has_received_message(self, peer_id: str, message_id: str) -> bool:
        record = self._data["trusted_peers"].get(peer_id)
        if not isinstance(record, dict):
            return False
        message_ids = record.get("received_message_ids")
        return isinstance(message_ids, list) and message_id in message_ids

    def remember_received_message(self, peer_id: str, message_id: str) -> None:
        record = self._data["trusted_peers"].get(peer_id)
        if not isinstance(record, dict):
            raise PeerError("Cannot remember a message from an untrusted peer")
        message_ids = record.setdefault("received_message_ids", [])
        if not isinstance(message_ids, list):
            message_ids = []
            record["received_message_ids"] = message_ids
        if message_id in message_ids:
            return
        message_ids.append(message_id)
        del message_ids[:-MAX_REMEMBERED_MESSAGE_IDS]
        self._save()

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(self._data, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.path)
            if os.name != "nt":
                self.path.chmod(0o600)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise PeerError(f"Cannot write peer store {self.path}: {exc}") from exc


SendMessage = Callable[[dict[str, Any]], Awaitable[None]]
SessionCallback = Callable[["PeerSession"], bool]
SessionResetCallback = Callable[["PeerSession"], None]
ReceiveText = Callable[[str, str], None]


class PeerSession:
    def __init__(
        self,
        *,
        local_id: str,
        local_name: str,
        pair_code: str | None,
        store: PeerStore | None = None,
        send_message: SendMessage,
        on_ready: SessionCallback | None = None,
        on_reset: SessionResetCallback | None = None,
        on_text: ReceiveText | None = None,
        incoming_files: IncomingFileStore | None = None,
        direction: str = "direct",
        ack_timeout: float = 3.0,
        send_attempts: int = 3,
    ) -> None:
        self.local_id = local_id
        self.local_name = local_name
        self.pair_code = pair_code
        self.store = store or PeerStore(None, local_id=local_id)
        self.send_message = send_message
        self.on_ready = on_ready
        self.on_reset = on_reset
        self.on_text = on_text or self._print_text
        self.incoming_files = incoming_files
        self.direction = direction
        self.ack_timeout = ack_timeout
        self.send_attempts = send_attempts
        self.peer_id: str | None = None
        self.peer_name: str | None = None
        self.accepted = False
        self.connected = asyncio.Event()
        self.ended = asyncio.Event()
        self._hello_future: asyncio.Future[dict[str, Any]] | None = None
        self._hello_result_future: asyncio.Future[dict[str, Any]] | None = None
        self._pending_handshake: dict[str, Any] | None = None
        self._completed_handshake: dict[str, Any] | None = None
        self._ack_waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._file_waiters: dict[
            tuple[str, str, str], asyncio.Future[dict[str, Any]]
        ] = {}
        self._seen_message_ids: set[str] = set()

    async def initiate_handshake(self) -> None:
        nonce = uuid.uuid4().hex
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._hello_future = future
        try:
            hello: dict[str, Any] = {
                "type": "peer_hello",
                "peer_id": self.local_id,
                "peer_name": self.local_name,
                "nonce": nonce,
                "trusted_auth": self.store.authentication_proofs(
                    self.local_id, nonce
                ),
            }
            if self.pair_code is not None:
                hello["pairing_proof"] = _pairing_proof(
                    self.pair_code, self.local_id, nonce
                )
            await self.send_message(hello)
            response = await asyncio.wait_for(future, timeout=8.0)
        except TimeoutError as exc:
            raise PeerError("Peer handshake timed out") from exc
        finally:
            self._hello_future = None
            if not future.done():
                future.cancel()
        if response.get("nonce") != nonce:
            raise PeerError("Peer handshake nonce mismatch")
        if response.get("status") != "accepted":
            raise PairingRejected(
                str(response.get("reason") or "Peer rejected the handshake")
            )
        peer_id = _required_message_text(response, "peer_id")
        peer_name = _required_message_text(response, "peer_name")
        if peer_id == self.local_id:
            raise PeerError("Cannot pair this device with itself")
        auth_mode = response.get("auth_mode")
        if auth_mode == "trusted":
            secret = self.store.secret_for(peer_id)
            if secret is None:
                raise PairingRejected("Peer used unknown saved trust")
        elif auth_mode == "code":
            if self.pair_code is None:
                raise PairingRequired("First connection requires a pairing code")
            secret = _derive_shared_secret(self.pair_code, self.local_id, peer_id)
        else:
            raise PairingRejected("Peer returned an invalid authentication mode")
        expected_proof = _trusted_proof(secret, peer_id, self.local_id, nonce)
        received_proof = _required_message_text(response, "auth_proof")
        if not hmac.compare_digest(received_proof, expected_proof):
            raise PairingRejected("Peer authentication proof was rejected")
        self.peer_id = peer_id
        self.peer_name = peer_name
        result_future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._hello_result_future = result_future
        try:
            await self.send_message(
                {
                    "type": "peer_hello_confirm",
                    "peer_id": self.local_id,
                    "nonce": nonce,
                    "confirmation_proof": _handshake_confirmation(
                        secret, self.local_id, peer_id, nonce
                    ),
                }
            )
            result = await asyncio.wait_for(result_future, timeout=8.0)
        except TimeoutError as exc:
            raise PeerError("Peer handshake confirmation timed out") from exc
        finally:
            self._hello_result_future = None
            if not result_future.done():
                result_future.cancel()
        if result.get("nonce") != nonce:
            raise PeerError("Peer handshake result nonce mismatch")
        if result.get("peer_id") != peer_id:
            raise PeerError("Peer handshake result identity mismatch")
        if result.get("confirmed_peer_id") != self.local_id:
            raise PeerError("Peer handshake result addressed another device")
        if result.get("status") != "success":
            raise PairingRejected(
                str(result.get("reason") or "Peer did not confirm the handshake")
            )
        self._mark_ready()
        if not self.accepted:
            raise PairingRejected("Peer connection was superseded")
        if auth_mode == "code":
            self.store.trust(peer_id, peer_name, secret)

    async def handle_message(self, message: dict[str, Any]) -> None:
        message_type = message.get("type")
        if message_type == "peer_hello":
            peer_id = _required_message_text(message, "peer_id")
            peer_name = _required_message_text(message, "peer_name")
            nonce = _required_message_text(message, "nonce")
            if peer_id == self.local_id:
                await self.send_message(
                    {
                        "type": "peer_pairing_rejected",
                        "nonce": nonce,
                        "status": "rejected",
                        "reason": "Cannot pair a device with itself",
                    }
                )
                return
            secret = self.store.secret_for(peer_id)
            auth_mode = "trusted"
            if secret is not None:
                trusted_auth = message.get("trusted_auth")
                received_proof = (
                    trusted_auth.get(self.local_id)
                    if isinstance(trusted_auth, dict)
                    else None
                )
                expected_proof = _trusted_proof(
                    secret, peer_id, self.local_id, nonce
                )
                if not isinstance(received_proof, str) or not hmac.compare_digest(
                    received_proof, expected_proof
                ):
                    await self.send_message(
                        {
                            "type": "peer_pairing_rejected",
                            "nonce": nonce,
                            "status": "rejected",
                            "reason": "Saved trust proof is invalid",
                        }
                    )
                    return
            elif self.pair_code is None or not isinstance(
                message.get("pairing_proof"), str
            ):
                await self.send_message(
                    {
                        "type": "peer_pairing_required",
                        "nonce": nonce,
                        "status": "pairing_required",
                        "reason": "First connection requires a pairing code",
                    }
                )
                return
            else:
                auth_mode = "code"
                expected_proof = _pairing_proof(
                    self.pair_code, peer_id, nonce
                )
                if not hmac.compare_digest(
                    message["pairing_proof"], expected_proof
                ):
                    await self.send_message(
                        {
                            "type": "peer_pairing_rejected",
                            "nonce": nonce,
                            "status": "rejected",
                            "reason": "Incorrect pairing code",
                        }
                    )
                    return
                secret = _derive_shared_secret(
                    self.pair_code, self.local_id, peer_id
                )
            self.peer_id = peer_id
            self.peer_name = peer_name
            self._pending_handshake = {
                "nonce": nonce,
                "peer_id": peer_id,
                "peer_name": peer_name,
                "secret": secret,
                "auth_mode": auth_mode,
            }
            await self.send_message(
                {
                    "type": "peer_hello_ack",
                    "peer_id": self.local_id,
                    "peer_name": self.local_name,
                    "nonce": nonce,
                    "status": "accepted",
                    "auth_mode": auth_mode,
                    "auth_proof": _trusted_proof(
                        secret, self.local_id, peer_id, nonce
                    ),
                }
            )
            return
        if message_type == "peer_hello_ack":
            if self._hello_future is not None and not self._hello_future.done():
                self._hello_future.set_result(message)
            return
        if message_type == "peer_hello_confirm":
            await self._handle_handshake_confirmation(message)
            return
        if message_type == "peer_hello_result":
            if (
                self._hello_result_future is not None
                and not self._hello_result_future.done()
            ):
                self._hello_result_future.set_result(message)
            return
        if message_type == "peer_pairing_rejected":
            if self._hello_future is not None and not self._hello_future.done():
                self._hello_future.set_exception(
                    PairingRejected("Incorrect pairing code or saved trust")
                )
            return
        if message_type == "peer_pairing_required":
            if self._hello_future is not None and not self._hello_future.done():
                self._hello_future.set_exception(
                    PairingRequired("First connection requires a pairing code")
                )
            return
        if message_type == "peer_message":
            message_id = _required_message_text(message, "message_id")
            text = _required_message_text(message, "text", allow_empty=True)
            status = "duplicate"
            reason: str | None = None
            if not self.connected.is_set() or self.peer_id is None:
                status = "failed"
                reason = "Text message requires an authenticated peer"
            elif (
                message_id not in self._seen_message_ids
                and not self.store.has_received_message(self.peer_id, message_id)
            ):
                try:
                    self.on_text(self.peer_name or "peer", text)
                    self.store.remember_received_message(
                        self.peer_id, message_id
                    )
                except Exception as exc:
                    logger.exception("Could not process peer text message")
                    status = "failed"
                    reason = str(exc) or type(exc).__name__
                else:
                    self._seen_message_ids.add(message_id)
                    status = "success"
            response = {
                "type": "peer_ack",
                "message_id": message_id,
                "status": status,
            }
            if reason is not None:
                response["reason"] = reason
            await self.send_message(response)
            return
        if message_type == "peer_ack":
            message_id = _required_message_text(message, "message_id")
            waiter = self._ack_waiters.get(message_id)
            if waiter is not None and not waiter.done():
                waiter.set_result(message)
            return
        if message_type == "peer_file_offer":
            await self._handle_file_offer(message)
            return
        if message_type == "peer_file_chunk":
            await self._handle_file_chunk(message)
            return
        if message_type == "peer_file_complete":
            await self._handle_file_complete(message)
            return
        file_ack_types = {
            "peer_file_offer_ack": "offer",
            "peer_file_chunk_ack": "chunk",
            "peer_file_complete_ack": "complete",
        }
        request_kind = file_ack_types.get(message_type)
        if request_kind is not None:
            transfer_id = _required_message_text(message, "transfer_id")
            request_id = _required_message_text(message, "request_id")
            waiter = self._file_waiters.get(
                (request_kind, transfer_id, request_id)
            )
            if waiter is not None and not waiter.done():
                waiter.set_result(message)
            return
        logger.warning("Ignoring unsupported peer message: %r", message)

    async def _handle_handshake_confirmation(
        self, message: dict[str, Any]
    ) -> None:
        nonce = _required_message_text(message, "nonce")
        peer_id = _required_message_text(message, "peer_id")
        completed = self._completed_handshake
        if (
            completed is not None
            and completed.get("nonce") == nonce
            and completed.get("confirmed_peer_id") == peer_id
        ):
            await self.send_message(dict(completed))
            return

        pending = self._pending_handshake
        response: dict[str, Any] = {
            "type": "peer_hello_result",
            "peer_id": self.local_id,
            "peer_name": self.local_name,
            "nonce": nonce,
            "confirmed_peer_id": peer_id,
        }
        if (
            pending is None
            or pending.get("nonce") != nonce
            or pending.get("peer_id") != peer_id
        ):
            response.update(
                status="rejected", reason="No matching handshake request"
            )
            await self.send_message(response)
            return

        secret = str(pending["secret"])
        expected = _handshake_confirmation(
            secret, peer_id, self.local_id, nonce
        )
        received = message.get("confirmation_proof")
        if not isinstance(received, str) or not hmac.compare_digest(
            received, expected
        ):
            response.update(
                status="rejected", reason="Handshake confirmation is invalid"
            )
            self._pending_handshake = None
            await self.send_message(response)
            return

        self.peer_id = peer_id
        self.peer_name = str(pending["peer_name"])
        auth_mode = pending["auth_mode"]
        self._mark_ready()
        if self.accepted:
            response["status"] = "success"
            if auth_mode == "code":
                self.store.trust(peer_id, self.peer_name, secret)
        else:
            response.update(
                status="rejected", reason="Peer connection was superseded"
            )
        self._pending_handshake = None
        self._completed_handshake = dict(response)
        await self.send_message(response)

    async def _handle_file_offer(self, message: dict[str, Any]) -> None:
        transfer_id = _required_message_text(message, "transfer_id")
        request_id = _required_message_text(message, "request_id")
        response: dict[str, Any] = {
            "type": "peer_file_offer_ack",
            "transfer_id": transfer_id,
            "request_id": request_id,
        }
        try:
            manager, peer_id = self._file_context()
            result = manager.offer(
                peer_id=peer_id,
                transfer_id=transfer_id,
                file_name=_required_message_text(message, "file_name"),
                size=message.get("size"),
                sha256=_required_message_text(message, "sha256"),
            )
            response.update(result)
        except (FileTransferError, PeerError) as exc:
            response.update(status="rejected", reason=str(exc), next_offset=0)
        await self.send_message(response)

    async def _handle_file_chunk(self, message: dict[str, Any]) -> None:
        transfer_id = _required_message_text(message, "transfer_id")
        request_id = _required_message_text(message, "request_id")
        response: dict[str, Any] = {
            "type": "peer_file_chunk_ack",
            "transfer_id": transfer_id,
            "request_id": request_id,
        }
        try:
            manager, peer_id = self._file_context()
            encoded = _required_message_text(message, "data")
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError) as exc:
                raise FileTransferError("File chunk encoding is invalid") from exc
            next_offset = manager.write_chunk(
                peer_id=peer_id,
                transfer_id=transfer_id,
                offset=message.get("offset"),
                data=data,
            )
            response.update(status="ok", next_offset=next_offset)
        except (FileTransferError, PeerError) as exc:
            response.update(status="error", reason=str(exc))
        await self.send_message(response)

    async def _handle_file_complete(self, message: dict[str, Any]) -> None:
        transfer_id = _required_message_text(message, "transfer_id")
        request_id = _required_message_text(message, "request_id")
        response: dict[str, Any] = {
            "type": "peer_file_complete_ack",
            "transfer_id": transfer_id,
            "request_id": request_id,
        }
        try:
            manager, peer_id = self._file_context()
            path = manager.complete(peer_id=peer_id, transfer_id=transfer_id)
            response.update(status="complete", file_name=path.name)
        except (FileTransferError, PeerError) as exc:
            response.update(status="error", reason=str(exc))
        await self.send_message(response)

    def _file_context(self) -> tuple[IncomingFileStore, str]:
        if not self.connected.is_set() or self.peer_id is None:
            raise PeerError("File transfer requires an authenticated peer")
        if self.incoming_files is None:
            raise FileTransferError("Incoming file storage is disabled")
        return self.incoming_files, self.peer_id

    def _mark_ready(self) -> None:
        self.connected.set()
        self.accepted = self.on_ready(self) if self.on_ready is not None else True
        if not self.accepted:
            self.connected.clear()
            return
        logger.info("Paired peer ready: %s", self.peer_name)

    @property
    def handshake_in_progress(self) -> bool:
        return self._pending_handshake is not None

    async def send_text(self, message_id: str, text: str) -> dict[str, Any]:
        if not self.connected.is_set():
            raise PeerError("Peer is not connected")
        for attempt in range(1, self.send_attempts + 1):
            future: asyncio.Future[dict[str, Any]] = (
                asyncio.get_running_loop().create_future()
            )
            self._ack_waiters[message_id] = future
            try:
                await self.send_message(
                    {
                        "type": "peer_message",
                        "message_id": message_id,
                        "text": text,
                        "sent_at": time.time(),
                    }
                )
                response = await asyncio.wait_for(future, timeout=self.ack_timeout)
                status = response.get("status")
                if status not in {"success", "duplicate"}:
                    raise ActionRejected(
                        str(response.get("reason") or "Peer rejected the text message")
                    )
                return {
                    "action": "text",
                    "message_id": message_id,
                    "status": status,
                    "attempts": attempt,
                }
            except TimeoutError:
                if attempt >= self.send_attempts:
                    raise PeerError(
                        f"Message {message_id} was not acknowledged"
                    )
            finally:
                current = self._ack_waiters.get(message_id)
                if current is future:
                    self._ack_waiters.pop(message_id, None)
                if not future.done():
                    future.cancel()
        raise PeerError(f"Message {message_id} was not acknowledged")

    async def offer_file(
        self,
        *,
        transfer_id: str,
        file_name: str,
        size: int,
        sha256: str,
    ) -> dict[str, Any]:
        return await self._file_request(
            "offer",
            {
                "type": "peer_file_offer",
                "transfer_id": transfer_id,
                "file_name": file_name,
                "size": size,
                "sha256": sha256,
            },
        )

    async def send_file_chunk(
        self, *, transfer_id: str, offset: int, data: bytes
    ) -> dict[str, Any]:
        return await self._file_request(
            "chunk",
            {
                "type": "peer_file_chunk",
                "transfer_id": transfer_id,
                "offset": offset,
                "data": base64.b64encode(data).decode("ascii"),
            },
        )

    async def complete_file(self, transfer_id: str) -> dict[str, Any]:
        return await self._file_request(
            "complete",
            {"type": "peer_file_complete", "transfer_id": transfer_id},
        )

    async def _file_request(
        self, request_kind: str, message: dict[str, Any]
    ) -> dict[str, Any]:
        if not self.connected.is_set():
            raise PeerError("Peer is not connected")
        transfer_id = _required_message_text(message, "transfer_id")
        request_id = str(uuid.uuid4())
        message = {**message, "request_id": request_id}
        key = (request_kind, transfer_id, request_id)
        for attempt in range(1, self.send_attempts + 1):
            future: asyncio.Future[dict[str, Any]] = (
                asyncio.get_running_loop().create_future()
            )
            self._file_waiters[key] = future
            try:
                await self.send_message(message)
                response = await asyncio.wait_for(
                    future, timeout=max(15.0, self.ack_timeout)
                )
                return {**response, "attempts": attempt}
            except TimeoutError:
                if attempt >= self.send_attempts:
                    raise PeerError(
                        f"File transfer {transfer_id} was not acknowledged"
                    )
            finally:
                current = self._file_waiters.get(key)
                if current is future:
                    self._file_waiters.pop(key, None)
                if not future.done():
                    future.cancel()
        raise PeerError(f"File transfer {transfer_id} was not acknowledged")

    def reset(self) -> None:
        was_connected = self.connected.is_set()
        self.connected.clear()
        self.accepted = False
        self.ended.set()
        self.peer_id = None
        self.peer_name = None
        if self._hello_future is not None and not self._hello_future.done():
            self._hello_future.set_exception(PeerError("Peer disconnected"))
        if (
            self._hello_result_future is not None
            and not self._hello_result_future.done()
        ):
            self._hello_result_future.set_exception(
                PeerError("Peer disconnected")
            )
        for future in self._ack_waiters.values():
            if not future.done():
                future.set_exception(PeerError("Peer disconnected"))
        self._ack_waiters.clear()
        for future in self._file_waiters.values():
            if not future.done():
                future.set_exception(PeerError("Peer disconnected"))
        self._file_waiters.clear()
        if was_connected and self.on_reset is not None:
            self.on_reset(self)

    @staticmethod
    def _print_text(peer_name: str, text: str) -> None:
        status("receive", f"{peer_name}: {text}", leading_newline=True)


@dataclass(slots=True)
class OutgoingText:
    message_id: str
    text: str
    completed: asyncio.Future[dict[str, Any]]


@dataclass(slots=True)
class OutgoingFile:
    transfer_id: str
    path: Path
    file_name: str
    size: int
    sha256: str
    modified_ns: int
    completed: asyncio.Future[dict[str, Any]]


class ConsoleInput:
    """Read stdin once and distribute complete lines through one async queue."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._thread = threading.Thread(
            target=self._read_stdin,
            name="peer-console-reader",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._closed.set()

    async def read(self, prompt: str = "") -> str:
        if prompt:
            print(prompt, end="", flush=True)
        value = await self.queue.get()
        if value is None:
            raise EOFError
        return value

    def feed_line(self, value: str | None) -> None:
        """Inject a line without stdin; useful for embedding and tests."""
        self.queue.put_nowait(value)

    def _read_stdin(self) -> None:
        while not self._closed.is_set():
            try:
                line = sys.stdin.readline()
            except (EOFError, KeyboardInterrupt, OSError):
                line = ""
            value = None if line == "" else line.rstrip("\r\n")
            loop = self._loop
            if loop is None or loop.is_closed():
                return
            loop.call_soon_threadsafe(self.queue.put_nowait, value)
            if value is None:
                return


class PeerApplication:
    def __init__(
        self,
        local_id: str = "",
        *,
        console: ConsoleInput | None = None,
        incoming_files: IncomingFileStore | None = None,
    ) -> None:
        self.local_id = local_id
        self.console = console or ConsoleInput()
        self.incoming_files = incoming_files
        self._incoming_progress: dict[str, int] = {}
        if self.incoming_files is not None:
            self.incoming_files.on_offer = self._on_file_offer
            self.incoming_files.on_progress = self._on_file_progress
            self.incoming_files.on_complete = self._on_file_complete
        self.stop = asyncio.Event()
        self.session_ready = asyncio.Event()
        self.session_available = asyncio.Event()
        self.session_available.set()
        self.session: PeerSession | None = None
        self.outgoing: asyncio.Queue[OutgoingText] = asyncio.Queue()
        self.outgoing_files: asyncio.Queue[OutgoingFile] = asyncio.Queue()

    def set_session(self, session: PeerSession) -> bool:
        current = self.session
        if current is session:
            return True
        if current is not None and not self._should_replace(current, session):
            logger.info(
                "Ignoring duplicate %s connection from %s",
                session.direction,
                session.peer_name,
            )
            return False
        self.session = session
        self.session_ready.set()
        self.session_available.clear()
        status("success", f"Handshake complete with {session.peer_name}")
        if current is not None:
            current.reset()
        return True

    def _should_replace(
        self, current: PeerSession, candidate: PeerSession
    ) -> bool:
        if not current.peer_id or current.peer_id != candidate.peer_id:
            return False
        if {current.direction, candidate.direction} != {"incoming", "outgoing"}:
            return False
        preferred = (
            "outgoing" if self.local_id < candidate.peer_id else "incoming"
        )
        return candidate.direction == preferred

    def clear_session(self, session: PeerSession) -> None:
        if self.session is session:
            self.session = None
            self.session_ready.clear()
            self.session_available.set()
            status("warning", "Peer disconnected; waiting for reconnection...")

    async def sender_loop(self) -> None:
        while not self.stop.is_set():
            item = await self.outgoing.get()
            try:
                while not self.stop.is_set():
                    await self.session_ready.wait()
                    session = self.session
                    if session is None:
                        continue
                    try:
                        result = await session.send_text(
                            item.message_id, item.text
                        )
                    except ActionRejected as exc:
                        if not item.completed.done():
                            item.completed.set_exception(exc)
                        break
                    except Exception as exc:
                        logger.warning("Send interrupted: %s", exc)
                        await asyncio.sleep(0.25)
                        continue
                    if not item.completed.done():
                        item.completed.set_result(result)
                    break
            finally:
                self.outgoing.task_done()

    async def send(self, text: str) -> dict[str, Any]:
        message_id = str(uuid.uuid4())
        completed: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        await self.outgoing.put(OutgoingText(message_id, text, completed))
        return await completed

    async def file_sender_loop(self) -> None:
        while not self.stop.is_set():
            item = await self.outgoing_files.get()
            try:
                while not self.stop.is_set():
                    await self.session_ready.wait()
                    session = self.session
                    if session is None:
                        continue
                    try:
                        result = await self._send_file_on_session(item, session)
                    except (FileTransferError, OSError) as exc:
                        if not item.completed.done():
                            item.completed.set_exception(exc)
                        break
                    except Exception as exc:
                        logger.warning("File send interrupted: %s", exc)
                        await asyncio.sleep(0.25)
                        continue
                    if not item.completed.done():
                        item.completed.set_result(result)
                    break
            finally:
                self.outgoing_files.task_done()

    async def queue_file(self, value: str) -> OutgoingFile:
        path = validate_outgoing_file(value)
        before = path.stat()
        digest = await asyncio.to_thread(sha256_file, path)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (
            after.st_size,
            after.st_mtime_ns,
        ):
            raise FileTransferError("File changed while its SHA-256 was calculated")
        item = OutgoingFile(
            transfer_id=str(uuid.uuid4()),
            path=path,
            file_name=path.name,
            size=after.st_size,
            sha256=digest,
            modified_ns=after.st_mtime_ns,
            completed=asyncio.get_running_loop().create_future(),
        )
        item.completed.add_done_callback(
            lambda future, outgoing=item: self._report_file_result(
                outgoing, future
            )
        )
        await self.outgoing_files.put(item)
        return item

    async def _send_file_on_session(
        self, item: OutgoingFile, session: PeerSession
    ) -> dict[str, Any]:
        stat = item.path.stat()
        if (stat.st_size, stat.st_mtime_ns) != (item.size, item.modified_ns):
            raise FileTransferError("Source file changed during transfer")

        while True:
            response = await session.offer_file(
                transfer_id=item.transfer_id,
                file_name=item.file_name,
                size=item.size,
                sha256=item.sha256,
            )
            status = response.get("status")
            if status == "pending":
                await asyncio.sleep(1.0)
                if self.session is not session:
                    raise PeerError("Peer connection changed")
                continue
            if status == "rejected":
                reason = response.get("reason") or "Peer rejected the file"
                raise FileTransferError(str(reason))
            if status == "complete":
                return response
            if status != "accepted":
                raise PeerError(f"Invalid file offer response: {status}")
            offset = _required_nonnegative_int(response, "next_offset")
            if offset > item.size:
                raise PeerError("Peer returned an invalid file offset")
            break

        last_percent = -1
        with item.path.open("rb") as handle:
            while offset < item.size:
                if self.session is not session:
                    raise PeerError("Peer connection changed")
                handle.seek(offset)
                data = handle.read(min(FILE_CHUNK_SIZE, item.size - offset))
                if not data:
                    raise FileTransferError("Source file ended unexpectedly")
                response = await session.send_file_chunk(
                    transfer_id=item.transfer_id,
                    offset=offset,
                    data=data,
                )
                if response.get("status") != "ok":
                    raise PeerError(
                        str(response.get("reason") or "File chunk was rejected")
                    )
                next_offset = _required_nonnegative_int(
                    response, "next_offset"
                )
                if not offset < next_offset <= item.size:
                    raise PeerError("Peer returned an invalid file offset")
                offset = next_offset
                percent = 100 if item.size == 0 else offset * 100 // item.size
                if percent // 10 != last_percent // 10:
                    progress(item.file_name, offset, item.size)
                last_percent = percent

        response = await session.complete_file(item.transfer_id)
        if response.get("status") != "complete":
            raise PeerError(
                str(response.get("reason") or "Peer could not verify the file")
            )
        return response

    @staticmethod
    def _report_file_result(
        item: OutgoingFile, future: asyncio.Future[dict[str, Any]]
    ) -> None:
        try:
            response = future.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            status(
                "error",
                f"File send failed [{item.transfer_id[:8]}]: {exc}",
            )
            return
        remote_name = response.get("file_name") or item.file_name
        status(
            "success",
            f"File delivered [{item.transfer_id[:8]}]: {remote_name}",
        )

    @staticmethod
    def _on_file_offer(transfer: IncomingTransfer) -> None:
        status(
            "receive",
            f"Incoming file [{transfer.transfer_id[:8]}]: "
            f"{transfer.file_name} ({transfer.size} bytes)",
            leading_newline=True,
        )
        status(
            "info",
            f"Use /accept {transfer.transfer_id[:8]} or "
            f"/reject {transfer.transfer_id[:8]}.",
        )

    def _on_file_progress(self, transfer: IncomingTransfer) -> None:
        percent = (
            100
            if transfer.size == 0
            else transfer.received * 100 // transfer.size
        )
        bucket = percent // 10
        if self._incoming_progress.get(transfer.transfer_id) == bucket:
            return
        self._incoming_progress[transfer.transfer_id] = bucket
        progress(transfer.file_name, transfer.received, transfer.size)

    def _on_file_complete(
        self, transfer: IncomingTransfer, path: Path
    ) -> None:
        self._incoming_progress.pop(transfer.transfer_id, None)
        status(
            "success",
            f"File received [{transfer.transfer_id[:8]}]: {path}",
        )

    def accept_file(self, reference: str | None) -> None:
        if self.incoming_files is None:
            raise FileTransferError("Incoming file storage is disabled")
        transfer = self.incoming_files.accept(reference)
        status(
            "success",
            f"Accepted file [{transfer.transfer_id[:8]}]: {transfer.file_name}",
        )

    def reject_file(self, reference: str | None) -> None:
        if self.incoming_files is None:
            raise FileTransferError("Incoming file storage is disabled")
        transfer = self.incoming_files.reject(reference)
        status(
            "success",
            f"Rejected file [{transfer.transfer_id[:8]}]: {transfer.file_name}",
        )

    async def user_loop(self, one_shot_text: str | None) -> None:
        await self.session_ready.wait()
        if one_shot_text is not None:
            result = await self.send(one_shot_text)
            status(
                "success",
                f"Text delivered id={result['message_id'][:8]} "
                f"status={result['status']} attempts={result['attempts']}",
            )
            await asyncio.sleep(1.0)
            self.stop.set()
            return

        status(
            "info",
            "Commands: /sendfile PATH  /accept ID  /reject ID  /quit",
        )
        while not self.stop.is_set():
            try:
                text = await self.console.read(prompt("> "))
            except (EOFError, KeyboardInterrupt):
                self.stop.set()
                return
            command, argument = _terminal_command(text)
            if command == "/quit":
                self.stop.set()
                return
            if command == "/sendfile":
                if not argument:
                    status("error", "File path is required.")
                    continue
                try:
                    item = await self.queue_file(argument)
                except (FileTransferError, OSError) as exc:
                    status("error", f"Cannot queue file: {exc}")
                    continue
                status(
                    "info",
                    f"Queued file [{item.transfer_id[:8]}]: "
                    f"{item.file_name} ({item.size} bytes)",
                )
                continue
            if command in {"/accept", "/reject"}:
                try:
                    if command == "/accept":
                        self.accept_file(argument or None)
                    else:
                        self.reject_file(argument or None)
                except FileTransferError as exc:
                    status("error", str(exc))
                continue
            if not text:
                continue
            try:
                result = await self.send(text)
            except PeerError as exc:
                status("error", f"Text was not delivered: {exc}")
                continue
            status(
                "success",
                f"Text delivered id={result['message_id'][:8]} "
                f"status={result['status']} attempts={result['attempts']}",
            )


class _WindowsPeripheralServer:
    """WinRT advertiser used because Bless does not support Python 3.14."""

    def __init__(self, name: str, *, loop: asyncio.AbstractEventLoop) -> None:
        self.name = name
        self.loop = loop
        self.provider: Any = None
        self.write_characteristic: Any = None
        self.notify_characteristic: Any = None
        self.write_token: Any = None
        self.subscribed_token: Any = None
        self.write_request_func: Callable[[Any, bytes], None] | None = None

    async def start(self) -> None:
        from winrt.windows.devices.bluetooth import BluetoothError
        from winrt.windows.devices.bluetooth.genericattributeprofile import (
            GattCharacteristicProperties,
            GattLocalCharacteristicParameters,
            GattProtectionLevel,
            GattServiceProvider,
            GattServiceProviderAdvertisementStatus,
            GattServiceProviderAdvertisingParameters,
        )

        result = await GattServiceProvider.create_async(uuid.UUID(SERVICE_UUID))
        if result.error != BluetoothError.SUCCESS:
            raise PeerError(f"Could not create peer service: {result.error}")
        self.provider = result.service_provider

        write_parameters = GattLocalCharacteristicParameters()
        write_parameters.characteristic_properties = (
            GattCharacteristicProperties.WRITE
            | GattCharacteristicProperties.WRITE_WITHOUT_RESPONSE
        )
        write_parameters.write_protection_level = GattProtectionLevel.PLAIN
        write_result = await self.provider.service.create_characteristic_async(
            uuid.UUID(WRITE_UUID), write_parameters
        )
        if write_result.error != BluetoothError.SUCCESS:
            raise PeerError(f"Could not create write channel: {write_result.error}")
        self.write_characteristic = write_result.characteristic

        notify_parameters = GattLocalCharacteristicParameters()
        notify_parameters.characteristic_properties = (
            GattCharacteristicProperties.NOTIFY
        )
        notify_parameters.read_protection_level = GattProtectionLevel.PLAIN
        notify_result = await self.provider.service.create_characteristic_async(
            uuid.UUID(NOTIFY_UUID), notify_parameters
        )
        if notify_result.error != BluetoothError.SUCCESS:
            raise PeerError(
                f"Could not create notification channel: {notify_result.error}"
            )
        self.notify_characteristic = notify_result.characteristic
        self.write_token = self.write_characteristic.add_write_requested(
            self._on_write_requested
        )
        self.subscribed_token = (
            self.notify_characteristic.add_subscribed_clients_changed(
                self._on_subscribed_clients_changed
            )
        )

        advertising = GattServiceProviderAdvertisingParameters()
        advertising.is_connectable = True
        advertising.is_discoverable = True
        self.provider.start_advertising(advertising)
        await asyncio.sleep(1.0)
        accepted = {
            GattServiceProviderAdvertisementStatus.STARTED,
            GattServiceProviderAdvertisementStatus.STARTED_WITHOUT_ALL_ADVERTISEMENT_DATA,
        }
        if self.provider.advertisement_status not in accepted:
            raise PeerError(
                f"Peer advertising failed: {self.provider.advertisement_status}"
            )

    async def stop(self) -> None:
        if self.write_characteristic is not None and self.write_token is not None:
            self.write_characteristic.remove_write_requested(self.write_token)
        if (
            self.notify_characteristic is not None
            and self.subscribed_token is not None
        ):
            self.notify_characteristic.remove_subscribed_clients_changed(
                self.subscribed_token
            )
        if self.provider is not None:
            self.provider.stop_advertising()

    async def is_connected(self) -> bool:
        return bool(
            self.notify_characteristic is not None
            and self.notify_characteristic.subscribed_clients
        )

    async def is_advertising(self) -> bool:
        if self.provider is None:
            return False
        from winrt.windows.devices.bluetooth.genericattributeprofile import (
            GattServiceProviderAdvertisementStatus,
        )

        return self.provider.advertisement_status in {
            GattServiceProviderAdvertisementStatus.STARTED,
            GattServiceProviderAdvertisementStatus.STARTED_WITHOUT_ALL_ADVERTISEMENT_DATA,
        }

    async def notify(self, data: bytes) -> None:
        from winrt.windows.devices.bluetooth.genericattributeprofile import (
            GattCommunicationStatus,
        )
        from winrt.windows.storage.streams import DataWriter

        if self.notify_characteristic is None:
            raise PeerError("Peer notification channel is not running")
        writer = DataWriter()
        try:
            writer.write_bytes(data)
            buffer = writer.detach_buffer()
            results = list(
                await self.notify_characteristic.notify_value_async(buffer)
            )
        finally:
            writer.close()
        if not results or any(
            result.status != GattCommunicationStatus.SUCCESS
            for result in results
        ):
            raise PeerError("Peer notification could not be delivered")

    def _on_subscribed_clients_changed(self, _sender: Any, _args: Any) -> None:
        return

    def _on_write_requested(self, _sender: Any, event_args: Any) -> None:
        deferral = event_args.get_deferral()
        asyncio.run_coroutine_threadsafe(
            self._handle_write(event_args, deferral), self.loop
        )

    async def _handle_write(self, event_args: Any, deferral: Any) -> None:
        try:
            request = await event_args.get_request_async()
            if request is None:
                return
            data = bytes(memoryview(request.value))
            request.respond()
            if self.write_request_func is not None:
                self.write_request_func(None, data)
        finally:
            deferral.complete()


class PeripheralPeerEndpoint:
    """Cross-platform BLE advertiser and incoming peer endpoint."""

    def __init__(
        self,
        app: PeerApplication,
        *,
        name: str,
        peer_id: str,
        pair_code: str,
        store: PeerStore,
    ) -> None:
        self.app = app
        self.name = name
        self.peer_id = peer_id
        self.pair_code = pair_code
        self.store = store
        self.loop: asyncio.AbstractEventLoop | None = None
        self.server: Any = None
        self.notify_characteristic: Any = None
        self.decoder = FrameDecoder()
        self.notify_lock = asyncio.Lock()
        self.receive_lock = asyncio.Lock()
        self.session = self._new_session()
        self._was_connected = False

    def _new_session(self) -> PeerSession:
        return PeerSession(
            local_id=self.peer_id,
            local_name=self.name,
            pair_code=self.pair_code,
            store=self.store,
            send_message=self._notify,
            on_ready=self.app.set_session,
            on_reset=self.app.clear_session,
            incoming_files=self.app.incoming_files,
            direction="incoming",
        )

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        advertised_name = f"RB-{self.peer_id[:6]}"
        if sys.platform == "win32":
            self.server = _WindowsPeripheralServer(
                advertised_name, loop=self.loop
            )
            self.server.write_request_func = self._on_write
            await self.server.start()
            if not await self.server.is_advertising():
                raise PeerError("Peer advertising failed")
            status(
                "success",
                f"Peer '{self.name}' is discoverable as {advertised_name}.",
            )
            return
        try:
            from bless import (
                BlessServer,
                GATTAttributePermissions,
                GATTCharacteristicProperties,
            )
        except ImportError as exc:
            raise PeerError("Bless is not installed") from exc
        self.server = await self._create_server(BlessServer, advertised_name)
        await self.server.add_new_service(SERVICE_UUID)
        await self.server.add_new_characteristic(
            SERVICE_UUID,
            WRITE_UUID,
            GATTCharacteristicProperties.write
            | GATTCharacteristicProperties.write_without_response,
            None,
            GATTAttributePermissions.writeable,
        )
        await self.server.add_new_characteristic(
            SERVICE_UUID,
            NOTIFY_UUID,
            GATTCharacteristicProperties.notify,
            None,
            GATTAttributePermissions.readable,
        )
        self.notify_characteristic = self.server.get_characteristic(NOTIFY_UUID)
        if self.notify_characteristic is None:
            raise PeerError("Could not create peer notification channel")
        self.server.write_request_func = self._on_write
        await self.server.start(prioritize_local_name=False)
        if not await self.server.is_advertising():
            raise PeerError("Peer advertising failed")
        status(
            "success",
            f"Peer '{self.name}' is discoverable as {advertised_name}.",
        )

    async def _create_server(self, server_type: Any, name: str) -> Any:
        """Keep a backend bug from blocking the asyncio thread indefinitely."""
        loop = asyncio.get_running_loop()
        created: asyncio.Future[Any] = loop.create_future()

        def finish(server: Any = None, error: BaseException | None = None) -> None:
            if created.done():
                return
            if error is None:
                created.set_result(server)
            else:
                created.set_exception(error)

        def construct() -> None:
            try:
                server = server_type(name, loop=loop)
            except BaseException as exc:
                loop.call_soon_threadsafe(finish, None, exc)
            else:
                loop.call_soon_threadsafe(finish, server, None)

        threading.Thread(
            target=construct,
            name="peer-bluetooth-advertiser-init",
            daemon=True,
        ).start()
        try:
            return await asyncio.wait_for(created, timeout=30.0)
        except TimeoutError as exc:
            raise PeerError(
                "Bluetooth advertiser initialization timed out; check that "
                "Bluetooth is enabled and this terminal has Bluetooth permission"
            ) from exc

    async def stop(self) -> None:
        self.session.reset()
        if self.server is not None:
            await self.server.stop()

    async def monitor(self) -> None:
        while not self.app.stop.is_set():
            connected = bool(
                self.server is not None and await self.server.is_connected()
            )
            if self._was_connected and not connected:
                self._reset_session()
            self._was_connected = connected
            await asyncio.sleep(0.25)

    def _on_write(self, _characteristic: Any, value: Any) -> None:
        if self.loop is None:
            return
        data = bytes(value)
        self.loop.call_soon_threadsafe(
            asyncio.create_task, self._handle_write(data)
        )

    async def _handle_write(self, data: bytes) -> None:
        try:
            async with self.receive_lock:
                for message in self.decoder.feed(data):
                    await self.session.handle_message(message)
                    if (
                        not self.session.accepted
                        and self.session.peer_id
                        and not self.session.handshake_in_progress
                    ):
                        self._reset_session()
        except ProtocolError:
            logger.exception("Invalid peer frame")
            self._reset_session()
        except Exception:
            logger.exception("Could not handle peer write")

    async def _notify(self, message: dict[str, Any]) -> None:
        if self.server is None:
            raise PeerError("Peer advertiser is not running")
        frame = encode_frame(message)
        async with self.notify_lock:
            for offset in range(0, len(frame), 20):
                chunk = frame[offset : offset + 20]
                if isinstance(self.server, _WindowsPeripheralServer):
                    await self.server.notify(chunk)
                    continue
                if self.notify_characteristic is None:
                    raise PeerError("Peer notification channel is not running")
                self.notify_characteristic.value = bytearray(
                    chunk
                )
                for attempt in range(100):
                    if self.server.update_value(SERVICE_UUID, NOTIFY_UUID):
                        break
                    if not await self.server.is_connected():
                        self._reset_session()
                        raise PeerError("Peer disconnected during notification")
                    await asyncio.sleep(0.02)
                else:
                    raise PeerError("Peer notification could not be delivered")

    def _reset_session(self) -> None:
        self.session.reset()
        self.decoder = FrameDecoder()
        self.session = self._new_session()


class CentralPeerEndpoint:
    def __init__(
        self,
        app: PeerApplication,
        *,
        name: str,
        peer_id: str,
        store: PeerStore,
        console: ConsoleInput | None = None,
        pair_code: str | None = None,
    ) -> None:
        self.app = app
        self.name = name
        self.peer_id = peer_id
        self.store = store
        self.console = console or app.console
        self.pair_code = pair_code

    async def choose_device(self, *, scan_timeout: float, auto: bool) -> Any | None:
        try:
            from bleak import BleakScanner
        except ImportError as exc:
            raise PeerError("Bleak is not installed") from exc

        while True:
            status("info", "Scanning for nearby peers...")
            try:
                completed, discovered = await self._unless_other_session(
                    BleakScanner.discover(
                        timeout=scan_timeout, return_adv=True
                    )
                )
                if not completed:
                    return None
            except Exception as exc:
                logger.warning("Bluetooth scan failed: %s", exc)
                status("warning", "Scan failed; retrying in 2 seconds...")
                await asyncio.sleep(2.0)
                continue
            candidates: list[tuple[Any, Any]] = []
            for device, advertisement in discovered.values():
                advertised = {
                    value.lower() for value in (advertisement.service_uuids or [])
                }
                if SERVICE_UUID.lower() in advertised:
                    candidates.append((device, advertisement))
            if candidates:
                break
            status("warning", "No compatible peer yet; continuing to wait...")
            await asyncio.sleep(1.0)
        candidates.sort(
            key=lambda item: item[1].rssi
            if item[1].rssi is not None
            else -999,
            reverse=True,
        )
        print(style("Nearby peers", "accent", bold=True), flush=True)
        for index, (device, advertisement) in enumerate(candidates, 1):
            display_name = device.name or advertisement.local_name or "unnamed peer"
            print(
                f"  {style(str(index) + '.', 'accent', bold=True)} "
                f"{display_name}  signal={advertisement.rssi}  id={device.address}",
                flush=True,
            )
        status(
            "info",
            "Select on one device only; leave the other device waiting.",
        )
        if auto:
            selected = 0
            status("info", f"Selected 1: {candidates[0][0].name or 'peer'}")
        else:
            while True:
                try:
                    completed, raw = await self._unless_other_session(
                        self.console.read(prompt("Select peer number: "))
                    )
                except EOFError:
                    self.app.stop.set()
                    return None
                if not completed:
                    status(
                        "success",
                        "Incoming peer authenticated; device selection cancelled.",
                    )
                    return None
                if raw.strip().lower() == "/quit":
                    self.app.stop.set()
                    return None
                try:
                    selected = int(raw) - 1
                except ValueError:
                    continue
                if 0 <= selected < len(candidates):
                    break
        return candidates[selected][0]

    async def run(self, *, scan_timeout: float, auto: bool) -> None:
        while not self.app.stop.is_set():
            await self.app.session_available.wait()
            if self.app.stop.is_set():
                return
            device = await self.choose_device(
                scan_timeout=scan_timeout, auto=auto
            )
            if device is None:
                if self.app.session is not None:
                    await self.app.session_available.wait()
                continue
            await self.connection_loop(device)

    async def connection_loop(self, device: Any) -> None:
        try:
            from bleak import BleakClient
        except ImportError as exc:
            raise PeerError("Bleak is not installed") from exc

        delay = 1.0
        while not self.app.stop.is_set() and self.app.session is None:
            disconnected = asyncio.Event()
            loop = asyncio.get_running_loop()

            def on_disconnected(_client: Any) -> None:
                loop.call_soon_threadsafe(disconnected.set)

            options: dict[str, Any] = {
                "disconnected_callback": on_disconnected,
                "timeout": 15.0,
            }
            client = BleakClient(device, **options)
            session: PeerSession | None = None
            try:
                status("info", "Connecting and authenticating peer...")
                completed, _ = await self._unless_other_session(client.connect())
                if not completed:
                    return
                decoder = FrameDecoder()
                send_lock = asyncio.Lock()

                async def send_message(message: dict[str, Any]) -> None:
                    frame = encode_frame(message)
                    mtu_size = getattr(client, "mtu_size", 23) or 23
                    chunk_size = max(20, mtu_size - 3)
                    async with send_lock:
                        for offset in range(0, len(frame), chunk_size):
                            await client.write_gatt_char(
                                WRITE_UUID,
                                frame[offset : offset + chunk_size],
                                response=True,
                            )

                session = PeerSession(
                    local_id=self.peer_id,
                    local_name=self.name,
                    pair_code=self.pair_code,
                    store=self.store,
                    send_message=send_message,
                    on_ready=self.app.set_session,
                    on_reset=self.app.clear_session,
                    incoming_files=self.app.incoming_files,
                    direction="outgoing",
                )

                def on_notification(_sender: Any, data: bytearray) -> None:
                    try:
                        messages = decoder.feed(data)
                    except ProtocolError:
                        logger.exception("Invalid notification frame")
                        return
                    for message in messages:
                        loop.call_soon_threadsafe(
                            asyncio.create_task, session.handle_message(message)
                        )

                await client.start_notify(NOTIFY_UUID, on_notification)
                while True:
                    try:
                        completed, _ = await self._unless_other_session(
                            session.initiate_handshake(), session=session
                        )
                        if not completed:
                            return
                        break
                    except (PairingRequired, PairingRejected) as exc:
                        self.pair_code = None
                        session.pair_code = None
                        status(
                            "warning", f"{exc}. Please enter the peer's code."
                        )
                        pair_code = await self._read_pair_code(session)
                        if pair_code is None:
                            return
                        self.pair_code = pair_code
                        session.pair_code = pair_code
                if self.app.session is not session:
                    return
                delay = 1.0
                stop_waiter = asyncio.create_task(self.app.stop.wait())
                disconnect_waiter = asyncio.create_task(disconnected.wait())
                superseded_waiter = asyncio.create_task(session.ended.wait())
                done, pending = await asyncio.wait(
                    (stop_waiter, disconnect_waiter, superseded_waiter),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                if stop_waiter in done or superseded_waiter in done:
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self.app.stop.is_set():
                    reason = str(exc).strip() or type(exc).__name__
                    logger.warning("Peer connection ended: %s", reason)
                    status(
                        "warning",
                        f"Connection attempt ended ({reason}). The peer may be "
                        "making its own outgoing connection.",
                    )
            finally:
                if session is not None:
                    session.reset()
                if client.is_connected:
                    try:
                        await asyncio.wait_for(client.disconnect(), timeout=5.0)
                    except Exception:
                        logger.debug("Could not close peer connection", exc_info=True)
            if not self.app.stop.is_set() and self.app.session is None:
                status("warning", f"Reconnecting in {delay:.0f} second(s)...")
                try:
                    await asyncio.wait_for(
                        self.app.session_ready.wait(), timeout=delay
                    )
                    return
                except TimeoutError:
                    pass
                delay = min(delay * 2, 15.0)

    async def _read_pair_code(self, session: PeerSession) -> str | None:
        while not self.app.stop.is_set():
            try:
                completed, raw = await self._unless_other_session(
                    self.console.read(
                        prompt(
                            "First connection: enter the six-digit code shown "
                            "on the peer: "
                        )
                    ),
                    session=session,
                )
            except EOFError:
                self.app.stop.set()
                return None
            if not completed:
                return None
            if raw.strip().lower() == "/quit":
                self.app.stop.set()
                return None
            try:
                return _validate_pair_code(raw)
            except PeerError as exc:
                status("error", str(exc))
        return None

    async def _unless_other_session(
        self,
        operation: Awaitable[Any],
        *,
        session: PeerSession | None = None,
    ) -> tuple[bool, Any]:
        task = asyncio.ensure_future(operation)
        active = asyncio.create_task(self.app.session_ready.wait())
        try:
            done, _pending = await asyncio.wait(
                (task, active), return_when=asyncio.FIRST_COMPLETED
            )
            if active in done:
                selected = self.app.session
                if selected is not None and selected is not session:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    return False, None
            return True, await task
        finally:
            if not active.done():
                active.cancel()
            await asyncio.gather(active, return_exceptions=True)


def _required_message_text(
    message: dict[str, Any], key: str, *, allow_empty: bool = False
) -> str:
    value = message.get(key)
    if not isinstance(value, str) or (not value and not allow_empty):
        raise PeerError(f"Peer message has invalid {key}")
    return value


def _required_nonnegative_int(message: dict[str, Any], key: str) -> int:
    value = message.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PeerError(f"Peer message has invalid {key}")
    return value


def _terminal_command(value: str) -> tuple[str, str]:
    command, separator, argument = value.strip().partition(" ")
    argument = argument.strip() if separator else ""
    if (
        len(argument) >= 2
        and argument[0] == argument[-1]
        and argument[0] in {"'", '"'}
    ):
        argument = argument[1:-1]
    return command.lower(), argument


def _pairing_proof(pair_code: str, peer_id: str, nonce: str) -> str:
    payload = f"reliable-bluetooth-peer:{peer_id}:{nonce}".encode("utf-8")
    return hmac.new(pair_code.encode("ascii"), payload, hashlib.sha256).hexdigest()


def _derive_shared_secret(pair_code: str, first_id: str, second_id: str) -> str:
    identities = "|".join(sorted((first_id, second_id))).encode("utf-8")
    salt = b"reliable-bluetooth-peer-secret-v1:" + identities
    return hashlib.pbkdf2_hmac(
        "sha256", pair_code.encode("ascii"), salt, 120_000
    ).hex()


def _trusted_proof(
    secret: str, sender_id: str, receiver_id: str, nonce: str
) -> str:
    payload = (
        f"reliable-bluetooth-trusted-v1:{sender_id}:{receiver_id}:{nonce}"
    ).encode("utf-8")
    return hmac.new(bytes.fromhex(secret), payload, hashlib.sha256).hexdigest()


def _handshake_confirmation(
    secret: str, sender_id: str, receiver_id: str, nonce: str
) -> str:
    payload = (
        f"reliable-bluetooth-handshake-confirm-v1:"
        f"{sender_id}:{receiver_id}:{nonce}"
    ).encode("utf-8")
    return hmac.new(bytes.fromhex(secret), payload, hashlib.sha256).hexdigest()


def _validate_pair_code(value: str) -> str:
    value = value.strip()
    if len(value) != 6 or not value.isascii() or not value.isdigit():
        raise PeerError("Pairing code must contain exactly six digits")
    return value


async def _run_application(
    app: PeerApplication,
    *,
    user_loop: Awaitable[None],
    background_loops: tuple[Awaitable[None], ...] = (),
) -> None:
    tasks = [
        asyncio.create_task(app.sender_loop(), name="peer-sender"),
        asyncio.create_task(user_loop, name="peer-user"),
        asyncio.create_task(app.file_sender_loop(), name="peer-file-sender"),
    ]
    tasks.extend(
        asyncio.create_task(loop, name=f"peer-background-{index}")
        for index, loop in enumerate(background_loops, 1)
    )
    try:
        done, _pending = await asyncio.wait(
            tasks[1:], return_when=asyncio.FIRST_COMPLETED
        )
        if tasks[1] in done:
            await tasks[1]
        else:
            for task in done:
                exception = task.exception()
                if exception is not None:
                    raise exception
    finally:
        app.stop.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_peer(args: argparse.Namespace) -> int:
    name = args.name or socket.gethostname()
    store = PeerStore(args.peer_store)
    incoming_files = IncomingFileStore(args.receive_dir)
    peer_id = store.local_id
    local_pair_code = _validate_pair_code(
        args.pair_code or f"{secrets.randbelow(1_000_000):06d}"
    )
    label = "Pairing code for new peers only" if store.has_trusted_peers else "Pairing code"
    panel(
        "Reliable Bluetooth",
        (
            ("Device", name),
            (label, local_pair_code),
            ("Mode", "advertising + continuous scanning"),
            ("Storage", "plain JSON files"),
        ),
    )

    console = ConsoleInput()
    app = PeerApplication(
        peer_id,
        console=console,
        incoming_files=incoming_files,
    )
    peripheral = PeripheralPeerEndpoint(
        app,
        name=name,
        peer_id=peer_id,
        pair_code=local_pair_code,
        store=store,
    )
    central = CentralPeerEndpoint(
        app,
        name=name,
        peer_id=peer_id,
        store=store,
        console=console,
        pair_code=(
            _validate_pair_code(args.pair_code)
            if args.pair_code is not None
            else None
        ),
    )
    console.start()
    try:
        await peripheral.start()
        await _run_application(
            app,
            user_loop=app.user_loop(args.send),
            background_loops=(
                peripheral.monitor(),
                central.run(
                    scan_timeout=args.scan_timeout,
                    auto=args.auto,
                ),
            ),
        )
        return 0
    finally:
        console.close()
        if peripheral.server is not None:
            await peripheral.stop()

