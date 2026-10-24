"""Entry-point routing and packaging assets."""

import os
import subprocess
import sys
import unittest

from vboxfront_app import app, cli


class CliRoutingTest(unittest.TestCase):
    def test_only_known_subcommands_and_help_leave_the_gui(self):
        self.assertTrue(cli.wants_cli(["vboxfront", "health"]))
        self.assertTrue(cli.wants_cli(["vboxfront", "--version"]))
        self.assertTrue(cli.wants_cli(["vboxfront", "-h"]))
        self.assertFalse(cli.wants_cli(["vboxfront"]))
        # Qt's own arguments still reach QApplication.
        self.assertFalse(cli.wants_cli(["vboxfront", "-style", "fusion"]))

    def test_cli_imports_without_qtwidgets(self):
        result = subprocess.run(
            [sys.executable, "-c",
             "import sys; import vboxfront_app.cli; assert 'PyQt6.QtWidgets' not in sys.modules"],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unusable_vboxmanage_is_exit_3(self):
        from unittest import mock
        with mock.patch.object(cli, "vboxmanage_problem", return_value="nope"), \
                mock.patch("sys.stderr"):
            self.assertEqual(cli.main(["health"]), 3)

    def test_the_icon_ships_beside_the_package(self):
        self.assertTrue(os.path.isfile(app.ICON_PATH))
        with open(app.ICON_PATH, encoding="utf-8") as handle:
            self.assertIn("<svg", handle.read())


if __name__ == "__main__":
    unittest.main()
