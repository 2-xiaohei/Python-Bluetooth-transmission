from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable


MAX_FILE_SIZE = 10 * 1024 * 1024
# Keep encrypted frames short enough that control messages are not starved on
# low-MTU or weak BLE links. Transfers remain resumable at every chunk.
FILE_CHUNK_SIZE = 1024
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


class FileTransferError(RuntimeError):
    """Raised when a file transfer request or persisted state is invalid."""


@dataclass(slots=True)
class IncomingTransfer:
    transfer_id: str
    peer_id: str
    file_name: str
    size: int
    sha256: str
    received: int = 0
    status: str = "pending"
    final_path: str | None = None


OfferCallback = Callable[[IncomingTransfer], None]
ProgressCallback = Callable[[IncomingTransfer], None]
CompleteCallback = Callable[[IncomingTransfer, Path], None]


class IncomingFileStore:
    """Persist incoming file data and resumable offsets without a database."""

    def __init__(
        self,
        directory: str | Path,
        *,
        on_offer: OfferCallback | None = None,
        on_progress: ProgressCallback | None = None,
        on_complete: CompleteCallback | None = None,
    ) -> None:
        self.directory = Path(directory).expanduser().resolve()
        self.state_directory = self.directory / ".transfers"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.state_directory.mkdir(parents=True, exist_ok=True)
        self.on_offer = on_offer
        self.on_progress = on_progress
        self.on_complete = on_complete
        self._transfers: dict[str, IncomingTransfer] = {}
        self._load()

    def offer(
        self,
        *,
        peer_id: str,
        transfer_id: str,
        file_name: str,
        size: int,
        sha256: str,
    ) -> dict[str, object]:
        transfer_id = _validate_transfer_id(transfer_id)
        file_name = _safe_file_name(file_name)
        size = _validate_size(size)
        sha256 = _validate_sha256(sha256)
        current = self._transfers.get(transfer_id)
        if current is not None:
            expected = (current.peer_id, current.file_name, current.size, current.sha256)
            received = (peer_id, file_name, size, sha256)
            if received != expected:
                raise FileTransferError("Transfer ID metadata does not match")
            return self._offer_response(current)

        transfer = IncomingTransfer(
            transfer_id=transfer_id,
            peer_id=peer_id,
            file_name=file_name,
            size=size,
            sha256=sha256,
        )
        self._transfers[transfer_id] = transfer
        self._save(transfer)
        if self.on_offer is not None:
            self.on_offer(transfer)
        return self._offer_response(transfer)

    def accept(self, reference: str | None = None) -> IncomingTransfer:
        transfer = self._resolve(reference, statuses={"pending"})
        transfer.status = "accepted"
        self._save(transfer)
        return transfer

    def reject(self, reference: str | None = None) -> IncomingTransfer:
        transfer = self._resolve(reference, statuses={"pending", "accepted"})
        transfer.status = "rejected"
        transfer.received = 0
        self._part_path(transfer.transfer_id).unlink(missing_ok=True)
        self._save(transfer)
        return transfer

    def write_chunk(
        self,
        *,
        peer_id: str,
        transfer_id: str,
        offset: int,
        data: bytes,
    ) -> int:
        transfer = self._get_for_peer(peer_id, transfer_id)
        if transfer.status != "accepted":
            raise FileTransferError("File transfer has not been accepted")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise FileTransferError("File chunk offset is invalid")
        if not data or len(data) > FILE_CHUNK_SIZE:
            raise FileTransferError("File chunk size is invalid")
        if offset < transfer.received:
            return transfer.received
        if offset != transfer.received:
            raise FileTransferError(
                f"Expected file offset {transfer.received}, received {offset}"
            )
        if offset + len(data) > transfer.size:
            raise FileTransferError("File chunk exceeds declared file size")

        part_path = self._part_path(transfer_id)
        mode = "r+b" if part_path.exists() else "w+b"
        with part_path.open(mode) as handle:
            handle.seek(offset)
            handle.write(data)
            handle.truncate(offset + len(data))
            handle.flush()
            os.fsync(handle.fileno())
        transfer.received = offset + len(data)
        self._save(transfer)
        if self.on_progress is not None:
            self.on_progress(transfer)
        return transfer.received

    def complete(self, *, peer_id: str, transfer_id: str) -> Path:
        transfer = self._get_for_peer(peer_id, transfer_id)
        if transfer.status == "complete" and transfer.final_path is not None:
            return Path(transfer.final_path)
        if transfer.status != "accepted":
            raise FileTransferError("File transfer has not been accepted")
        if transfer.received != transfer.size:
            raise FileTransferError(
                f"File is incomplete: {transfer.received}/{transfer.size} bytes"
            )
        part_path = self._part_path(transfer_id)
        if not part_path.exists() and transfer.size == 0:
            part_path.touch()
        actual_hash = _sha256_file(part_path)
        if actual_hash != transfer.sha256:
            transfer.received = 0
            part_path.unlink(missing_ok=True)
            self._save(transfer)
            raise FileTransferError("File SHA-256 verification failed")

        final_path = self._available_path(transfer.file_name)
        part_path.replace(final_path)
        transfer.status = "complete"
        transfer.final_path = str(final_path)
        self._save(transfer)
        if self.on_complete is not None:
            self.on_complete(transfer, final_path)
        return final_path

    def _offer_response(self, transfer: IncomingTransfer) -> dict[str, object]:
        return {
            "status": transfer.status,
            "next_offset": transfer.received,
            "file_name": (
                Path(transfer.final_path).name
                if transfer.final_path is not None
                else None
            ),
        }

    def _get_for_peer(
        self, peer_id: str, transfer_id: str
    ) -> IncomingTransfer:
        transfer_id = _validate_transfer_id(transfer_id)
        transfer = self._transfers.get(transfer_id)
        if transfer is None:
            raise FileTransferError("Unknown file transfer")
        if transfer.peer_id != peer_id:
            raise FileTransferError("File transfer belongs to another peer")
        return transfer

    def _resolve(
        self, reference: str | None, *, statuses: set[str]
    ) -> IncomingTransfer:
        candidates = [
            transfer
            for transfer in self._transfers.values()
            if transfer.status in statuses
            and (
                reference is None
                or transfer.transfer_id == reference
                or transfer.transfer_id.startswith(reference)
            )
        ]
        if not candidates:
            raise FileTransferError("No matching incoming file request")
        if len(candidates) > 1:
            raise FileTransferError("File request ID is ambiguous")
        return candidates[0]

    def _load(self) -> None:
        for state_path in self.state_directory.glob("*.json"):
            try:
                value = json.loads(state_path.read_text(encoding="utf-8"))
                transfer = IncomingTransfer(**value)
                _validate_transfer_id(transfer.transfer_id)
                transfer.file_name = _safe_file_name(transfer.file_name)
                transfer.size = _validate_size(transfer.size)
                transfer.sha256 = _validate_sha256(transfer.sha256)
                if transfer.status not in {
                    "pending",
                    "accepted",
                    "rejected",
                    "complete",
                }:
                    raise FileTransferError("Invalid persisted transfer status")
                if not 0 <= transfer.received <= transfer.size:
                    raise FileTransferError("Invalid persisted transfer offset")
            except (OSError, TypeError, ValueError, json.JSONDecodeError, FileTransferError):
                continue
            if transfer.status in {"pending", "accepted"}:
                part_path = self._part_path(transfer.transfer_id)
                actual_size = part_path.stat().st_size if part_path.exists() else 0
                if actual_size != transfer.received:
                    transfer.received = min(actual_size, transfer.received)
                    if part_path.exists():
                        with part_path.open("r+b") as handle:
                            handle.truncate(transfer.received)
                    self._save(transfer)
            self._transfers[transfer.transfer_id] = transfer

    def _save(self, transfer: IncomingTransfer) -> None:
        path = self.state_directory / f"{transfer.transfer_id}.json"
        temporary = path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(asdict(transfer), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)

    def _part_path(self, transfer_id: str) -> Path:
        return self.state_directory / f"{transfer_id}.part"

    def _available_path(self, file_name: str) -> Path:
        candidate = self.directory / file_name
        if not candidate.exists():
            return candidate
        path = Path(file_name)
        for number in range(1, 10_000):
            candidate = self.directory / f"{path.stem} ({number}){path.suffix}"
            if not candidate.exists():
                return candidate
        raise FileTransferError("Could not allocate a destination file name")


def validate_outgoing_file(path: str | Path) -> Path:
    result = Path(path).expanduser().resolve()
    if not result.is_file():
        raise FileTransferError(f"File does not exist: {result}")
    size = result.stat().st_size
    _validate_size(size)
    return result


def sha256_file(path: str | Path) -> str:
    return _sha256_file(Path(path))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(256 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _validate_transfer_id(value: str) -> str:
    if not isinstance(value, str):
        raise FileTransferError("File transfer ID is invalid")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise FileTransferError("File transfer ID is invalid") from exc
    if str(parsed) != value.lower():
        raise FileTransferError("File transfer ID is invalid")
    return str(parsed)


def _validate_size(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise FileTransferError("File size is invalid")
    if value > MAX_FILE_SIZE:
        raise FileTransferError(
            f"File exceeds the {MAX_FILE_SIZE // (1024 * 1024)} MiB limit"
        )
    return value


def _validate_sha256(value: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value.lower()) is None:
        raise FileTransferError("File SHA-256 is invalid")
    return value.lower()


def _safe_file_name(value: str) -> str:
    if not isinstance(value, str):
        raise FileTransferError("File name is invalid")
    name = value.replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = "".join(character for character in name if ord(character) >= 32)
    name = "".join(
        "_" if character in '<>:"|?*' else character for character in name
    ).rstrip(" .")
    if not name or name in {".", ".."}:
        raise FileTransferError("File name is invalid")
    if name.split(".", 1)[0].upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }:
        name = f"_{name}"
    if len(name.encode("utf-8")) > 240:
        raise FileTransferError("File name is too long")
    return name
