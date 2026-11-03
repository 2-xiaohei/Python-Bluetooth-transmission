import io
import re
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from reliable_bluetooth.terminal import progress, status


class TerminalOutputTests(unittest.TestCase):
    def test_status_lines_start_with_timestamp(self) -> None:
        output = io.StringIO()
        with (
            patch("reliable_bluetooth.terminal._color_enabled", return_value=False),
            redirect_stdout(output),
        ):
            status("success", "Message delivered")

        self.assertRegex(
            output.getvalue(),
            r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] "
            r"\[ OK \] Message delivered\n$",
        )

    def test_progress_lines_start_with_timestamp(self) -> None:
        output = io.StringIO()
        with (
            patch("reliable_bluetooth.terminal._color_enabled", return_value=False),
            redirect_stdout(output),
        ):
            progress("report.zip", 5, 10, width=4)

        self.assertTrue(
            re.fullmatch(
                r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] "
                r"\[FILE\] report\.zip \[##--\] +50%  5/10 bytes\n",
                output.getvalue(),
            )
        )


if __name__ == "__main__":
    unittest.main()
