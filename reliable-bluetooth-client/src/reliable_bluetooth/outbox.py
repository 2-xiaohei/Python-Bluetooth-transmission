from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal


MessageState = Literal["pending", "sending", "acked", "failed"]
STATES: tuple[MessageState, ...] = ("pending", "sending", "acked", "failed")


class OutboxError(RuntimeError):
    """Raised when the text outbox cannot be read or updated."""


@dataclass(slots=True)
class OutboxMessage:
    message_id: str
    device_id: str
    command: str
    payload: dict[str, Any]
    created_at: float
    created_at_ns: int
    attempts: int = 0
    last_sent_at: float | None = None
    last_error: str | None = None

    def wire_message(self, session_id: str) -> dict[str, Any]:
        return {
            "type": "command",
            "message_id": self.message_id,
            "device_id": self.device_id,
            "session_id": session_id,
            "command": self.command,
            "payload": self.payload,
            "created_at": self.created_at,
        }


class TextOutbox:
    """A crash-recoverable queue backed only by UTF-8 JSON files."""

    def __init__(self, root: str | Path, *, retain_acked: bool = True):
        self.root = Path(root)
        self.retain_acked = retain_acked

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for state in STATES:
            (self.root / state).mkdir(exist_ok=True)
        (self.root / ".tmp").mkdir(exist_ok=True)

    def get_or_create_client_id(self) -> str:
        self.initialize()
        path = self.root / "client_id.txt"
        try:
            existing = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            existing = ""
        if existing:
            return existing

        candidate = str(uuid.uuid4())
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return path.read_text(encoding="utf-8").strip()
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(candidate + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return candidate

    def enqueue(
        self, *, device_id: str, command: str, payload: dict[str, Any]
    ) -> OutboxMessage:
        self.initialize()
        if not command.strip():
            raise OutboxError("Command cannot be empty")
        if not isinstance(payload, dict):
            raise OutboxError("Payload must be a JSON object")
        now_ns = time.time_ns()
        message = OutboxMessage(
            message_id=str(uuid.uuid4()),
            device_id=device_id,
            command=command.strip(),
            payload=payload,
            created_at=time.time(),
            created_at_ns=now_ns,
        )
        self._write_new("pending", message)
        return message

    def recover_inflight(self) -> int:
        self.initialize()
        recovered = 0
        for source in sorted((self.root / "sending").glob("*.json")):
            destination = self.root / "pending" / source.name
            try:
                source.replace(destination)
                recovered += 1
            except FileNotFoundError:
                continue
        return recovered

    def claim_next(self, device_id: str) -> OutboxMessage | None:
        self.initialize()
        for source in sorted((self.root / "pending").glob("*.json")):
            try:
                message = self._read(source)
            except FileNotFoundError:
                continue
            if message.device_id != device_id:
                continue
            destination = self.root / "sending" / source.name
            try:
                source.replace(destination)
            except FileNotFoundError:
                continue
            return message
        return None

    def record_attempt(self, message: OutboxMessage) -> OutboxMessage:
        message.attempts += 1
        message.last_sent_at = time.time()
        message.last_error = None
        self._replace("sending", message)
        return message

    def mark_pending(self, message_id: str, error: str | None = None) -> bool:
        return self._move_with_update(message_id, "sending", "pending", error)

    def mark_acked(self, message_id: str) -> bool:
        for source_state in ("sending", "pending"):
            source = self._find(source_state, message_id)
            if source is None:
                continue
            if self.retain_acked:
                source.replace(self.root / "acked" / source.name)
            else:
                source.unlink(missing_ok=True)
            return True
        return self._find("acked", message_id) is not None

    def mark_failed(self, message_id: str, error: str) -> bool:
        for source_state in ("sending", "pending"):
            if self._move_with_update(message_id, source_state, "failed", error):
                return True
        return False

    def mark_processed(self, message_ids: list[str]) -> int:
        marked = 0
        for message_id in message_ids:
            if isinstance(message_id, str) and self.mark_acked(message_id):
                marked += 1
        return marked

    def state_of(self, message_id: str) -> MessageState | None:
        for state in STATES:
            if self._find(state, message_id) is not None:
                return state
        return None

    def counts(self) -> dict[str, int]:
        self.initialize()
        return {
            state: sum(1 for _ in (self.root / state).glob("*.json"))
            for state in STATES
        }

    def list_messages(
        self, *, state: MessageState | None = None, limit: int = 20
    ) -> list[tuple[MessageState, OutboxMessage]]:
        self.initialize()
        items: list[tuple[MessageState, Path]] = []
        selected = (state,) if state else STATES
        for current_state in selected:
            items.extend(
                (current_state, path)
                for path in (self.root / current_state).glob("*.json")
            )
        items.sort(key=lambda item: item[1].name, reverse=True)
        result: list[tuple[MessageState, OutboxMessage]] = []
        for current_state, path in items[:limit]:
            try:
                result.append((current_state, self._read(path)))
            except FileNotFoundError:
                continue
        return result

    def _write_new(self, state: MessageState, message: OutboxMessage) -> Path:
        name = f"{message.created_at_ns:020d}_{message.message_id}.json"
        destination = self.root / state / name
        if destination.exists():
            raise OutboxError(f"Message already exists: {message.message_id}")
        self._atomic_write(destination, message)
        return destination

    def _replace(self, state: MessageState, message: OutboxMessage) -> None:
        path = self._find(state, message.message_id)
        if path is None:
            raise OutboxError(
                f"Cannot update {message.message_id}: no {state} file exists"
            )
        self._atomic_write(path, message)

    def _move_with_update(
        self,
        message_id: str,
        source_state: MessageState,
        target_state: MessageState,
        error: str | None,
    ) -> bool:
        source = self._find(source_state, message_id)
        if source is None:
            return False
        message = self._read(source)
        message.last_error = error
        self._atomic_write(source, message)
        try:
            source.replace(self.root / target_state / source.name)
        except FileNotFoundError:
            return False
        return True

    def _find(self, state: MessageState, message_id: str) -> Path | None:
        suffix = f"_{message_id}.json"
        for path in (self.root / state).glob("*.json"):
            if path.name.endswith(suffix):
                return path
        return None

    @staticmethod
    def _read(path: Path) -> OutboxMessage:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise TypeError("root value is not an object")
            message = OutboxMessage(**data)
            if not isinstance(message.payload, dict):
                raise TypeError("payload is not an object")
            return message
        except FileNotFoundError:
            raise
        except (json.JSONDecodeError, TypeError, OSError) as exc:
            raise OutboxError(f"Cannot read queue file {path}: {exc}") from exc

    def _atomic_write(self, destination: Path, message: OutboxMessage) -> None:
        self.initialize()
        temp = self.root / ".tmp" / f"{uuid.uuid4()}.tmp"
        try:
            with temp.open("x", encoding="utf-8") as handle:
                json.dump(asdict(message), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temp.replace(destination)
        except (OSError, TypeError, ValueError) as exc:
            temp.unlink(missing_ok=True)
            raise OutboxError(f"Cannot write queue file {destination}: {exc}") from exc

    def clear_acked(self) -> int:
        self.initialize()
        directory = self.root / "acked"
        count = sum(1 for _ in directory.glob("*.json"))
        if count:
            shutil.rmtree(directory)
            directory.mkdir()
        return count

