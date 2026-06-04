"""gateway_config (v14 §W5) — config-driven label/commands, dependency-minimal."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core import gateway_config as gw


class TestGatewayConfig(unittest.TestCase):
    def test_defaults_when_no_config(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = gw.load_gateway_config(root)
            self.assertEqual(gw.label(cfg), "ai.openclaw.gateway")
            self.assertEqual(gw.health_argv(cfg), ["openclaw", "health"])

    def test_json_override_changes_label(self) -> None:
        """Acceptance #8: a custom label is used, not the hardcoded default."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data").mkdir(parents=True)
            (root / "data" / "gateway.json").write_text(
                json.dumps({"gateway": {"label": "com.acme.openclaw"}}), encoding="utf-8"
            )
            cfg = gw.load_gateway_config(root)
            self.assertEqual(gw.label(cfg), "com.acme.openclaw")

    def test_restart_cmd_substitutes_label_and_uid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data").mkdir(parents=True)
            (root / "data" / "gateway.json").write_text(
                json.dumps({"label": "com.acme.gw", "restart_cmd": "launchctl kickstart -k gui/{uid}/{label}"}),
                encoding="utf-8",
            )
            cfg = gw.load_gateway_config(root)
            argv = gw.restart_argv(cfg)
            self.assertEqual(argv[0], "launchctl")
            self.assertIn("com.acme.gw", argv[-1])
            self.assertNotIn("{uid}", argv[-1])
            self.assertNotIn("{label}", argv[-1])

    def test_env_override_wins(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.dict("os.environ", {"DOORMAN_GATEWAY_LABEL": "com.env.gw"}):
                cfg = gw.load_gateway_config(root)
            self.assertEqual(gw.label(cfg), "com.env.gw")

    def test_watchdog_tuning_merges_partial(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data").mkdir(parents=True)
            (root / "data" / "gateway.json").write_text(
                json.dumps({"watchdog": {"max_restarts": 5}}), encoding="utf-8"
            )
            cfg = gw.load_gateway_config(root)
            tuning = gw.watchdog_tuning(cfg)
            self.assertEqual(tuning["max_restarts"], 5)
            self.assertEqual(tuning["interval_sec"], 60)  # default preserved

    def test_garbled_config_falls_back_to_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data").mkdir(parents=True)
            (root / "data" / "gateway.json").write_text("{not json", encoding="utf-8")
            cfg = gw.load_gateway_config(root)
            self.assertEqual(gw.label(cfg), "ai.openclaw.gateway")

    def test_install_id_is_stable_and_opaque(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a = gw.install_id(root)
            b = gw.install_id(root)
            self.assertEqual(a, b)
            self.assertTrue(a.isalnum())
            self.assertGreaterEqual(len(a), 16)

    def test_vendor_ping_disabled_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cfg = gw.load_gateway_config(root)
            self.assertFalse(gw.vendor_ping_config(cfg)["enabled"])


class TestPathResolution(unittest.TestCase):
    def test_augmented_path_includes_standard_dirs(self) -> None:
        path = gw.augmented_path()
        # /usr/bin and /bin always exist on macOS.
        self.assertIn("/usr/bin", path.split(":"))
        self.assertIn("/bin", path.split(":"))

    def test_augmented_env_overrides_path(self) -> None:
        env = gw.augmented_env()
        self.assertEqual(env["PATH"], gw.augmented_path())

    def test_resolve_argv_makes_launchctl_absolute(self) -> None:
        # launchctl resolves under the minimal default PATH already.
        argv = gw.resolve_argv(["launchctl", "list"])
        self.assertTrue(argv[0].endswith("/launchctl"))
        self.assertEqual(argv[1:], ["list"])

    def test_resolve_argv_finds_path_only_binary(self) -> None:
        """A binary only on the augmented PATH (e.g. Homebrew) still resolves —
        the core fix so `openclaw` is found under launchd."""
        with mock.patch.object(gw.shutil, "which", return_value="/opt/homebrew/bin/openclaw"):
            argv = gw.resolve_argv(["openclaw", "health"])
        self.assertEqual(argv, ["/opt/homebrew/bin/openclaw", "health"])

    def test_resolve_argv_unknown_returns_unchanged(self) -> None:
        with mock.patch.object(gw.shutil, "which", return_value=None):
            argv = gw.resolve_argv(["definitely-not-a-real-bin-xyz", "x"])
        self.assertEqual(argv, ["definitely-not-a-real-bin-xyz", "x"])

    def test_resolve_argv_empty(self) -> None:
        self.assertEqual(gw.resolve_argv([]), [])


if __name__ == "__main__":
    unittest.main()
