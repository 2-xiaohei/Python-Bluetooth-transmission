import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from reliable_bluetooth.cli import main


CONFIG = """
[bluetooth]
target_name = "Test Device"
write_characteristic_uuid = "write"
notify_characteristic_uuid = "notify"
expected_device_id = "device-001"

[storage]
directory = "queue"
"""


class CliTests(unittest.TestCase):
    def test_send_and_status_use_text_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = root / "config.toml"
            config.write_text(CONFIG, encoding="utf-8")

            send_output = io.StringIO()
            with contextlib.redirect_stdout(send_output):
                result = main(
                    [
                        "--config",
                        str(config),
                        "send",
                        "set_value",
                        "--payload",
                        '{"value":12}',
                    ]
                )
            self.assertEqual(result, 0)
            message_id = send_output.getvalue().strip()
            self.assertTrue(message_id)

            status_output = io.StringIO()
            with contextlib.redirect_stdout(status_output):
                result = main(["--config", str(config), "status"])
            self.assertEqual(result, 0)
            status = json.loads(status_output.getvalue())
            self.assertEqual(status["counts"]["pending"], 1)
            self.assertEqual(status["messages"][0]["message_id"], message_id)
            self.assertEqual(status["messages"][0]["payload"], {"value": 12})


if __name__ == "__main__":
    unittest.main()

