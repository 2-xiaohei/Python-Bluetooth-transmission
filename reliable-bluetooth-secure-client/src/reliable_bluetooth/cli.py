from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import ConfigError, Settings, load_settings
from .outbox import STATES, OutboxError, TextOutbox
from .peer import PeerError, run_peer
from .service import ReliableBluetoothService
from .transport import TransportError, discover_devices


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reliable-bluetooth",
        description="Encrypted reliable BLE peer client without a graphical interface",
    )
    parser.add_argument(
        "--config", default="config.toml", help="TOML configuration path"
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    scan = subparsers.add_parser("scan", help="Scan nearby BLE devices")
    scan.add_argument("--timeout", type=float, help="Override scan duration")

    subparsers.add_parser("run", help="Run the connection and resend service")

    send = subparsers.add_parser("send", help="Append a command to the text outbox")
    send.add_argument("command", help="Application command name")
    send.add_argument(
        "--payload", default="{}", help="JSON object containing command parameters"
    )

    status = subparsers.add_parser("status", help="Show text outbox status")
    status.add_argument("--state", choices=STATES)
    status.add_argument("--limit", type=int, default=20)

    peer = subparsers.add_parser(
        "peer", help="Scan, select, pair, and exchange point-to-point messages"
    )
    peer.add_argument("--name", help="Name shown to the selected peer")
    peer.add_argument(
        "--scan-timeout",
        type=float,
        default=8.0,
        help="Duration of each scan cycle; scanning continues until a peer appears",
    )
    peer.add_argument(
        "--auto", action="store_true", help="Select the strongest compatible peer"
    )
    peer.add_argument("--send", help="Send one message and exit")
    peer.add_argument(
        "--pair-code",
        help=(
            "Use this six-digit code for local display and the first outgoing "
            "pairing (mainly for automation)"
        ),
    )
    peer.add_argument(
        "--peer-store",
        default="data/secure_peer_store.json",
        help=(
            "Private JSON key store containing this device's identity key and "
            "trusted peer secrets; never share it"
        ),
    )
    peer.add_argument(
        "--receive-dir",
        default="received_files_secure",
        help="Directory for accepted incoming files and transfer state",
    )
    return parser


def _configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.app.log_level, None)
    if not isinstance(level, int):
        raise ConfigError(f"Invalid app.log_level: {settings.app.log_level}")
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _outbox(settings: Settings) -> TextOutbox:
    return TextOutbox(
        settings.storage.directory, retain_acked=settings.storage.retain_acked
    )


async def _run_service(settings: Settings) -> None:
    service = ReliableBluetoothService(settings)
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, service.request_stop)
        except (NotImplementedError, RuntimeError):
            pass
    await service.run()


def _parse_payload(raw: str) -> dict[str, Any]:
    if raw.startswith("@"):
        raw = Path(raw[1:]).read_text(encoding="utf-8")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Payload must be a JSON object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.action == "peer":
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                stream=sys.stdout,
            )
            try:
                return asyncio.run(run_peer(args))
            except KeyboardInterrupt:
                return 0
        settings = load_settings(args.config)
        _configure_logging(settings)
        if args.action == "scan":
            timeout = args.timeout or settings.bluetooth.scan_timeout
            devices = asyncio.run(discover_devices(timeout))
            print(json.dumps(devices, ensure_ascii=False, indent=2))
            return 0
        if args.action == "run":
            try:
                asyncio.run(_run_service(settings))
            except KeyboardInterrupt:
                pass
            return 0
        if args.action == "send":
            payload = _parse_payload(args.payload)
            message = _outbox(settings).enqueue(
                device_id=settings.bluetooth.expected_device_id,
                command=args.command,
                payload=payload,
            )
            print(message.message_id)
            return 0
        if args.action == "status":
            outbox = _outbox(settings)
            result = {
                "directory": str(settings.storage.directory),
                "counts": outbox.counts(),
                "messages": [
                    {"state": state, **asdict(message)}
                    for state, message in outbox.list_messages(
                        state=args.state, limit=max(0, args.limit)
                    )
                ],
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
    except (
        ConfigError,
        OutboxError,
        PeerError,
        TransportError,
        ValueError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    parser.error(f"Unknown action: {args.action}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

