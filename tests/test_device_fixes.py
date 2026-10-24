"""Regression checks for device startup, packaging, and native lifecycle."""

import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import app as app_module
import main_app
import tunnel_service
from device_manager import DeviceManager


ROOT = pathlib.Path(__file__).resolve().parents[1]


class PlatformContractTests(unittest.TestCase):
    def test_release_requirements_match_the_arm64_macos_11_bundle(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        build_script = (ROOT / "build.sh").read_text(encoding="utf-8")

        self.assertIn("- macOS 11.0 or newer on Apple silicon", readme)
        self.assertRegex(
            build_script,
            r"<key>LSMinimumSystemVersion</key>\s*<string>11\.0</string>",
        )


class TunneldOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, tunnel_service, "_tunneld_owned", False)
        tunnel_service._tunneld_owned = False

    def test_preexisting_tunneld_is_never_stopped(self):
        with patch.object(tunnel_service, "is_tunneld_running", return_value=True), \
                patch.object(tunnel_service, "start_tunneld_with_admin") as start, \
                patch.object(tunnel_service.http_requests, "get") as request:
            self.assertTrue(tunnel_service.ensure_tunnel(timeout=1))
            self.assertFalse(tunnel_service.stop_owned_tunnel())

        start.assert_not_called()
        request.assert_not_called()

    def test_tunneld_started_by_ghostpin_is_stopped_through_http(self):
        response = Mock(status_code=200)
        with patch.object(
            tunnel_service, "is_tunneld_running", side_effect=[False, True]
        ), patch.object(
            tunnel_service, "start_tunneld_with_admin", return_value=True
        ), patch.object(
            tunnel_service.http_requests, "get", return_value=response
        ) as request:
            self.assertTrue(tunnel_service.ensure_tunnel(timeout=1))
            self.assertTrue(tunnel_service.stop_owned_tunnel())

        request.assert_called_once_with(
            f"{tunnel_service.TUNNELD_URL}/shutdown", timeout=2
        )

    def test_device_shutdown_releases_an_owned_tunneld(self):
        with patch("device_manager.atexit.register"), patch(
            "device_manager.AsyncBridge"
        ):
            manager = DeviceManager()

        with patch.object(manager, "disconnect") as disconnect, patch.object(
            tunnel_service, "stop_owned_tunnel"
        ) as stop:
            manager.shutdown()

        disconnect.assert_called_once()
        stop.assert_called_once()


class StartScriptTests(unittest.TestCase):
    def test_invalid_existing_venv_is_preserved_and_recreated(self):
        source = (ROOT / "start.sh").read_text(encoding="utf-8")
        source = source.replace(
            '"$VENV_DIR/bin/python3" -m pip install --upgrade -q -r requirements.txt',
            ":",
        ).replace('exec "$VENV_DIR/bin/python3" app.py', ":")

        for broken_kind in ("missing-target", "unsupported-version"):
            with self.subTest(broken_kind=broken_kind), tempfile.TemporaryDirectory() as tmp:
                temp_root = pathlib.Path(tmp)
                script = temp_root / "start.sh"
                script.write_text(source, encoding="utf-8")

                host_bin = temp_root / "host-bin"
                host_bin.mkdir()
                (host_bin / "python3").symlink_to(sys.executable)

                venv_python = temp_root / ".venv" / "bin" / "python3"
                venv_python.parent.mkdir(parents=True)
                if broken_kind == "missing-target":
                    venv_python.symlink_to(temp_root / "removed-python")
                else:
                    venv_python.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
                    venv_python.chmod(0o755)

                env = os.environ.copy()
                env["PATH"] = f"{host_bin}:/usr/bin:/bin"
                result = subprocess.run(
                    ["bash", str(script)],
                    capture_output=True,
                    text=True,
                    env=env,
                    timeout=30,
                )

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                backups = list(temp_root.glob(".venv.broken-*"))
                self.assertEqual(len(backups), 1)
                self.assertTrue(
                    os.path.lexists(backups[0] / "bin" / "python3"),
                    "the unusable venv was not preserved",
                )
                supported = subprocess.run(
                    [
                        str(temp_root / ".venv" / "bin" / "python3"),
                        "-c",
                        "import sys; raise SystemExit(sys.version_info < (3, 10))",
                    ],
                    check=False,
                )
                self.assertEqual(supported.returncode, 0)


class BuildScriptTests(unittest.TestCase):
    def test_plain_python3_is_a_version_checked_fallback(self):
        source = (ROOT / "build.sh").read_text(encoding="utf-8")
        selection = source[source.index('PYTHON=""'):source.index('echo "    Using')]
        loop_line = next(line for line in selection.splitlines() if line.startswith("for p in "))

        self.assertTrue(loop_line.endswith(" python3; do"), loop_line)
        self.assertIn("sys.version_info < (3, 10)", selection)

        selection = selection.replace(loop_line, "for p in python3; do", 1)
        with tempfile.TemporaryDirectory() as tmp:
            host_bin = pathlib.Path(tmp) / "bin"
            host_bin.mkdir()
            plain_python = host_bin / "python3"
            plain_python.symlink_to(sys.executable)
            env = os.environ.copy()
            env["PATH"] = f"{host_bin}:/usr/bin:/bin"
            result = subprocess.run(
                ["bash", "-c", f'set -euo pipefail\n{selection}\nprintf "%s" "$PYTHON"'],
                capture_output=True,
                text=True,
                env=env,
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, str(plain_python))

    def test_dependencies_use_the_working_venv_python_not_the_pip_wrapper(self):
        source = (ROOT / "build.sh").read_text(encoding="utf-8")
        install_line = next(
            line for line in source.splitlines() if "install --no-cache-dir" in line
        )

        with tempfile.TemporaryDirectory() as tmp:
            temp_root = pathlib.Path(tmp)
            arg_log = temp_root / "args"
            vpython = temp_root / "python3"
            vpython.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$@" > "$GHOSTPIN_TEST_ARG_LOG"\n',
                encoding="utf-8",
            )
            vpython.chmod(0o755)
            env = os.environ.copy()
            env["GHOSTPIN_TEST_ARG_LOG"] = str(arg_log)
            command = (
                "set -euo pipefail\n"
                f'VPYTHON="{vpython}"\n'
                f'VPIP="{temp_root / "missing-pip"}"\n'
                f"{install_line}\n"
            )
            result = subprocess.run(
                ["bash", "-c", command], capture_output=True, text=True, env=env
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(
                arg_log.read_text(encoding="utf-8").splitlines()[:3],
                ["-m", "pip", "install"],
            )


class DeviceErrorTests(unittest.TestCase):
    def test_set_location_tells_a_disconnected_user_to_use_device_options(self):
        service = Mock(simulator=None, bridge=None)
        manager = Mock()
        manager.device_info = {"connected": False}
        with patch.object(app_module, "device_mgr", manager), patch.object(
            app_module, "loc_svc", service
        ):
            response = app_module.app.test_client().post(
                "/api/location/set", json={"lat": 40, "lon": -105}
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.get_json(),
            {"error": "Connect an iPhone from Device options, then try again."},
        )


class NativeApiTests(unittest.TestCase):
    def test_native_bridge_does_not_expose_the_unused_gpx_writer(self):
        self.assertFalse(hasattr(main_app.NativeApi, "save_gpx"))


if __name__ == "__main__":
    unittest.main()
