from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when the application configuration is invalid."""


@dataclass(frozen=True, slots=True)
class BluetoothSettings:
    write_characteristic_uuid: str
    notify_characteristic_uuid: str
    expected_device_id: str
    target_address: str | None = None
    target_name: str | None = None
    service_uuid: str | None = None
    pair: bool = True
    scan_timeout: float = 10.0
    connect_timeout: float = 15.0
    write_with_response: bool = True
    write_chunk_size: int = 180
    chunk_delay: float = 0.01


@dataclass(frozen=True, slots=True)
class ProtocolSettings:
    version: int = 1
    handshake_timeout: float = 8.0
    ack_timeout: float = 3.0
    max_send_attempts_per_connection: int = 3
    heartbeat_interval: float = 10.0
    heartbeat_failures: int = 3
    max_frame_size: int = 1_048_576


@dataclass(frozen=True, slots=True)
class ReconnectSettings:
    initial_delay: float = 1.0
    max_delay: float = 30.0
    multiplier: float = 2.0
    jitter: float = 0.2
    stable_reset_after: float = 60.0


@dataclass(frozen=True, slots=True)
class StorageSettings:
    directory: Path
    retain_acked: bool = True


@dataclass(frozen=True, slots=True)
class AppSettings:
    log_level: str = "INFO"


@dataclass(frozen=True, slots=True)
class Settings:
    bluetooth: BluetoothSettings
    protocol: ProtocolSettings
    reconnect: ReconnectSettings
    storage: StorageSettings
    app: AppSettings


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a TOML table")
    return value


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError("Bluetooth identifiers must be strings")
    value = value.strip()
    return value or None


def _required_text(section: dict[str, Any], key: str) -> str:
    value = _optional_text(section.get(key))
    if value is None:
        raise ConfigError(f"bluetooth.{key} is required")
    return value


def _positive(value: float | int, name: str) -> None:
    if value <= 0:
        raise ConfigError(f"{name} must be greater than zero")


def load_settings(path: str | Path) -> Settings:
    config_path = Path(path).expanduser().resolve()
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Configuration file not found: {config_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {config_path}: {exc}") from exc

    bluetooth_data = _section(data, "bluetooth")
    protocol_data = _section(data, "protocol")
    reconnect_data = _section(data, "reconnect")
    storage_data = _section(data, "storage")
    app_data = _section(data, "app")

    bluetooth = BluetoothSettings(
        target_address=_optional_text(bluetooth_data.get("target_address")),
        target_name=_optional_text(bluetooth_data.get("target_name")),
        service_uuid=_optional_text(bluetooth_data.get("service_uuid")),
        write_characteristic_uuid=_required_text(
            bluetooth_data, "write_characteristic_uuid"
        ),
        notify_characteristic_uuid=_required_text(
            bluetooth_data, "notify_characteristic_uuid"
        ),
        expected_device_id=_required_text(bluetooth_data, "expected_device_id"),
        pair=bool(bluetooth_data.get("pair", True)),
        scan_timeout=float(bluetooth_data.get("scan_timeout", 10.0)),
        connect_timeout=float(bluetooth_data.get("connect_timeout", 15.0)),
        write_with_response=bool(
            bluetooth_data.get("write_with_response", True)
        ),
        write_chunk_size=int(bluetooth_data.get("write_chunk_size", 180)),
        chunk_delay=float(bluetooth_data.get("chunk_delay", 0.01)),
    )
    if not any(
        (bluetooth.target_address, bluetooth.target_name, bluetooth.service_uuid)
    ):
        raise ConfigError(
            "Set at least one of bluetooth.target_address, target_name, or service_uuid"
        )

    protocol = ProtocolSettings(
        version=int(protocol_data.get("version", 1)),
        handshake_timeout=float(protocol_data.get("handshake_timeout", 8.0)),
        ack_timeout=float(protocol_data.get("ack_timeout", 3.0)),
        max_send_attempts_per_connection=int(
            protocol_data.get("max_send_attempts_per_connection", 3)
        ),
        heartbeat_interval=float(protocol_data.get("heartbeat_interval", 10.0)),
        heartbeat_failures=int(protocol_data.get("heartbeat_failures", 3)),
        max_frame_size=int(protocol_data.get("max_frame_size", 1_048_576)),
    )
    reconnect = ReconnectSettings(
        initial_delay=float(reconnect_data.get("initial_delay", 1.0)),
        max_delay=float(reconnect_data.get("max_delay", 30.0)),
        multiplier=float(reconnect_data.get("multiplier", 2.0)),
        jitter=float(reconnect_data.get("jitter", 0.2)),
        stable_reset_after=float(reconnect_data.get("stable_reset_after", 60.0)),
    )

    storage_value = storage_data.get("directory", "data/outbox")
    if not isinstance(storage_value, str) or not storage_value.strip():
        raise ConfigError("storage.directory must be a non-empty path")
    storage_path = Path(storage_value).expanduser()
    if not storage_path.is_absolute():
        storage_path = config_path.parent / storage_path
    storage = StorageSettings(
        directory=storage_path.resolve(),
        retain_acked=bool(storage_data.get("retain_acked", True)),
    )
    app = AppSettings(log_level=str(app_data.get("log_level", "INFO")).upper())

    for value, name in (
        (bluetooth.scan_timeout, "bluetooth.scan_timeout"),
        (bluetooth.connect_timeout, "bluetooth.connect_timeout"),
        (bluetooth.write_chunk_size, "bluetooth.write_chunk_size"),
        (protocol.version, "protocol.version"),
        (protocol.handshake_timeout, "protocol.handshake_timeout"),
        (protocol.ack_timeout, "protocol.ack_timeout"),
        (
            protocol.max_send_attempts_per_connection,
            "protocol.max_send_attempts_per_connection",
        ),
        (protocol.heartbeat_interval, "protocol.heartbeat_interval"),
        (protocol.heartbeat_failures, "protocol.heartbeat_failures"),
        (protocol.max_frame_size, "protocol.max_frame_size"),
        (reconnect.initial_delay, "reconnect.initial_delay"),
        (reconnect.max_delay, "reconnect.max_delay"),
        (reconnect.multiplier, "reconnect.multiplier"),
        (reconnect.stable_reset_after, "reconnect.stable_reset_after"),
    ):
        _positive(value, name)
    if bluetooth.chunk_delay < 0:
        raise ConfigError("bluetooth.chunk_delay cannot be negative")
    if not 0 <= reconnect.jitter <= 1:
        raise ConfigError("reconnect.jitter must be between 0 and 1")
    if reconnect.initial_delay > reconnect.max_delay:
        raise ConfigError("reconnect.initial_delay cannot exceed max_delay")

    return Settings(bluetooth, protocol, reconnect, storage, app)

