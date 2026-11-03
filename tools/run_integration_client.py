from __future__ import annotations

import argparse
import asyncio
import logging
import tempfile
from dataclasses import replace
from pathlib import Path

from reliable_bluetooth.config import StorageSettings, load_settings
from reliable_bluetooth.outbox import TextOutbox
from reliable_bluetooth.service import ReliableBluetoothService


async def run(config_path: str, timeout: float) -> int:
    settings = load_settings(config_path)
    with tempfile.TemporaryDirectory(prefix="reliable-bluetooth-integration-") as root:
        settings = replace(
            settings,
            storage=StorageSettings(directory=Path(root), retain_acked=True),
        )
        outbox = TextOutbox(settings.storage.directory)
        message = outbox.enqueue(
            device_id=settings.bluetooth.expected_device_id,
            command="integration_test",
            payload={"value": 42},
        )
        service = ReliableBluetoothService(settings, outbox=outbox)
        service_task = asyncio.create_task(service.run())
        try:
            async with asyncio.timeout(timeout):
                while True:
                    state = outbox.state_of(message.message_id)
                    if state == "acked":
                        stored = outbox.list_messages(state="acked", limit=1)[0][1]
                        print(
                            "INTEGRATION_OK "
                            f"message_id={message.message_id} attempts={stored.attempts}"
                        )
                        return 0
                    if state == "failed":
                        print(f"INTEGRATION_FAILED message_id={message.message_id}")
                        return 1
                    await asyncio.sleep(0.1)
        except TimeoutError:
            print(
                "INTEGRATION_TIMEOUT "
                f"message_id={message.message_id} state={outbox.state_of(message.message_id)}"
            )
            return 2
        finally:
            service.request_stop()
            try:
                await asyncio.wait_for(service_task, timeout=8.0)
            except TimeoutError:
                service_task.cancel()
                await asyncio.gather(service_task, return_exceptions=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one real BLE integration command")
    parser.add_argument("--config", default="config.windows-test.toml")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(run(args.config, args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())

