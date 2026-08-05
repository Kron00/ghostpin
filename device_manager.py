import asyncio
import atexit
import threading
import time

import requests as http_requests

from pymobiledevice3 import usbmux
from pymobiledevice3.exceptions import AlreadyMountedError
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
from pymobiledevice3.services.mobile_image_mounter import MobileImageMounterService, auto_mount


TUNNELD_PORT = 49151


class AsyncBridge:
    """Persistent asyncio event loop in a background thread."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro, timeout=30):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=timeout)


class DeviceManager:
    """Own one persistent RSD/DVT connection to the selected iPhone.

    iOS 17.4 and newer use pymobiledevice3's in-process userspace tunnel by
    default. The privileged tunneld route remains a compatibility fallback and
    is started only when opening the userspace RSD tunnel itself fails.
    """

    def __init__(self):
        self.rsd = None
        self.provider = None
        self.simulator = None
        self.userspace_tunnel = None
        self.bridge = AsyncBridge()
        self.device_info = {
            "name": None,
            "ios_version": None,
            "udid": None,
            "model": None,
            "connected": False,
            "connection_type": None,
            "tunnel_mode": None,
            "developer_mode": None,
            "ddi_mounted": None,
        }
        self._auto_reconnect = False
        self._reconnect_thread = None
        self._reconnect_callback = None
        atexit.register(self.shutdown)

    # ── Auto-reconnect ─────────────────────────────────────

    def enable_auto_reconnect(self, callback=None):
        """Start background auto-reconnect. callback(device_info) is called on reconnect."""
        self._auto_reconnect = True
        self._reconnect_callback = callback
        if not self._reconnect_thread or not self._reconnect_thread.is_alive():
            self._reconnect_thread = threading.Thread(target=self._reconnect_loop, daemon=True)
            self._reconnect_thread.start()
        return {"status": "Auto-reconnect enabled"}

    def disable_auto_reconnect(self):
        self._auto_reconnect = False
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            self._reconnect_thread.join(timeout=5)
        self._reconnect_thread = None
        return {"status": "Auto-reconnect disabled"}

    def _reconnect_loop(self):
        """Periodically check connection and reconnect if needed."""
        while self._auto_reconnect:
            if self.device_info.get("connected") and not self._is_connection_alive():
                print("[!] Device disconnected, attempting auto-reconnect...")
                self.device_info["connected"] = False
                try:
                    prefer_wifi = self.device_info.get("connection_type") == "WiFi"
                    udid = self.device_info.get("udid")
                    self.disconnect()
                    info = self.connect(udid=udid, prefer_wifi=prefer_wifi, retries=3, delay=2)
                    print(f"[+] Auto-reconnected ({info.get('connection_type', 'USB')})")
                    if self._reconnect_callback:
                        try:
                            self._reconnect_callback(info)
                        except Exception:
                            pass
                except Exception as exc:
                    print(f"[!] Auto-reconnect failed: {exc}")
            time.sleep(10)

    def _is_connection_alive(self):
        """Check the harmless remote lockdown channel, never location state."""
        if not self.rsd or not self.simulator:
            return False
        try:
            self.bridge.run(self.rsd.get_date(), timeout=10)
            return True
        except Exception:
            return False

    # ── Device discovery ───────────────────────────────────

    async def _discover_mux_devices(self):
        devices = {}
        for mux_device in await usbmux.list_devices():
            entry = devices.setdefault(
                mux_device.serial,
                {"udid": mux_device.serial, "connection_types": []},
            )
            connection = "USB" if mux_device.is_usb else "WiFi"
            if connection not in entry["connection_types"]:
                entry["connection_types"].append(connection)
        for entry in devices.values():
            entry["connection_types"].sort(key=lambda item: item != "USB")
        return list(devices.values())

    def _wait_for_device(self, udid=None, prefer_wifi=False, retries=15, delay=2):
        for attempt in range(retries):
            devices = self.bridge.run(self._discover_mux_devices(), timeout=10)
            if udid:
                devices = [device for device in devices if device["udid"] == udid]
            if devices:
                selected = devices[0]
                types = selected["connection_types"]
                connection_type = "WiFi" if prefer_wifi and "WiFi" in types else types[0]
                return selected["udid"], connection_type
            print(f"  [{attempt + 1}/{retries}] Waiting for a trusted iPhone...")
            time.sleep(delay)
        raise ConnectionError(
            "No trusted iPhone was found. Connect it by USB, unlock it, accept Trust, "
            "and enable Developer Mode."
        )

    def get_available_connections(self):
        connections = []
        for device in self.get_all_devices():
            for connection_type in device["connection_types"]:
                connections.append({
                    "udid": device["udid"],
                    "type": connection_type,
                    "address": None,
                    "port": None,
                })
        return connections

    def get_all_devices(self):
        try:
            return self.bridge.run(self._discover_mux_devices(), timeout=10)
        except Exception:
            return []

    # ── Tunnel and developer services ─────────────────────

    async def _open_userspace_tunnel(self, udid):
        tunnel = UserspaceRsdTunnel(serial=udid)
        try:
            rsd = await tunnel.aopen()
        except BaseException:
            await tunnel.aclose()
            raise
        self.userspace_tunnel = tunnel
        self.rsd = rsd

    def _get_tunneld_info(self, udid=None, prefer_wifi=False, retries=15, delay=2):
        for attempt in range(retries):
            try:
                response = http_requests.get(f"http://127.0.0.1:{TUNNELD_PORT}", timeout=5)
                data = response.json()
                choices = []
                for device_udid, entries in data.items():
                    if udid and device_udid != udid:
                        continue
                    for entry in entries:
                        interface = entry.get("interface", "")
                        connection = "USB" if "USB" in interface else "WiFi"
                        choices.append((
                            entry["tunnel-address"], entry["tunnel-port"], device_udid, connection
                        ))
                choices.sort(key=lambda item: item[3] != ("WiFi" if prefer_wifi else "USB"))
                if choices:
                    return choices[0]
            except Exception as exc:
                print(f"  [{attempt + 1}/{retries}] Waiting for legacy tunnel... ({exc})")
            time.sleep(delay)
        raise ConnectionError("The privileged tunneld fallback did not expose the selected iPhone.")

    async def _open_legacy_rsd(self, address, port):
        self.rsd = RemoteServiceDiscoveryService((address, port))
        await self.rsd.connect()

    async def _initialize_developer_services(self):
        properties = self.rsd.peer_info.get("Properties", {}) if self.rsd.peer_info else {}
        self.device_info["name"] = properties.get("DeviceName") or properties.get("DeviceClass", "iPhone")
        self.device_info["ios_version"] = (
            properties.get("HumanReadableProductVersionString")
            or properties.get("OSVersion")
            or "Unknown"
        )
        self.device_info["model"] = properties.get("ProductType", "Unknown")
        self.device_info["udid"] = properties.get("UniqueDeviceID", self.device_info.get("udid"))

        developer_mode = await self.rsd.get_developer_mode_status()
        self.device_info["developer_mode"] = bool(developer_mode)
        if not developer_mode:
            raise RuntimeError("Developer Mode is disabled on the selected iPhone.")

        mounter = MobileImageMounterService(lockdown=self.rsd)
        images = await mounter.copy_devices()
        ddi_mounted = any(
            image.get("DiskImageType") in {"Developer", "Personalized"}
            and image.get("IsMounted", True)
            for image in images
        )
        if not ddi_mounted:
            try:
                await auto_mount(self.rsd)
            except AlreadyMountedError:
                pass
            images = await mounter.copy_devices()
            ddi_mounted = any(
                image.get("DiskImageType") in {"Developer", "Personalized"}
                and image.get("IsMounted", True)
                for image in images
            )
        self.device_info["ddi_mounted"] = ddi_mounted
        if not ddi_mounted:
            raise RuntimeError("Developer Disk Image could not be mounted.")

        self.provider = DvtProvider(self.rsd)
        await self.provider.connect()
        self.simulator = LocationSimulation(self.provider)
        await self.simulator.connect()

    def connect(self, udid=None, prefer_wifi=False, retries=15, delay=2):
        """Connect userspace-first, falling back to privileged tunneld only on tunnel/RSD failure."""
        selected_udid, connection_type = self._wait_for_device(
            udid=udid, prefer_wifi=prefer_wifi, retries=retries, delay=delay
        )
        self.device_info["udid"] = selected_udid
        self.device_info["connection_type"] = connection_type

        try:
            self.bridge.run(self._open_userspace_tunnel(selected_udid), timeout=90)
            self.device_info["tunnel_mode"] = "userspace"
            print("[+] No-root userspace tunnel established")
        except Exception as userspace_error:
            print(f"[!] Userspace tunnel/RSD failed: {userspace_error}")
            print("[*] Falling back to privileged tunneld...")
            from tunnel_service import ensure_tunnel

            if not ensure_tunnel(timeout=30):
                raise ConnectionError(
                    f"Userspace tunnel failed and tunneld could not start: {userspace_error}"
                ) from userspace_error
            address, port, selected_udid, connection_type = self._get_tunneld_info(
                udid=selected_udid, prefer_wifi=prefer_wifi, retries=retries, delay=delay
            )
            self.bridge.run(self._open_legacy_rsd(address, port), timeout=60)
            self.device_info["udid"] = selected_udid
            self.device_info["connection_type"] = connection_type
            self.device_info["tunnel_mode"] = "tunneld"

        try:
            self.bridge.run(self._initialize_developer_services(), timeout=180)
        except Exception:
            self.disconnect()
            raise

        self.device_info["connected"] = True
        print(
            f"[+] Connected ({connection_type}, {self.device_info['tunnel_mode']}): "
            f"{self.device_info['name']} | iOS {self.device_info['ios_version']} | "
            f"{self.device_info['model']} | DDI mounted"
        )
        return dict(self.device_info)

    def disconnect(self):
        if self.provider:
            try:
                self.bridge.run(self.provider.close(), timeout=15)
            except Exception:
                pass
        self.provider = None
        self.simulator = None

        if self.userspace_tunnel:
            try:
                self.bridge.run(self.userspace_tunnel.aclose(), timeout=30)
            except Exception:
                pass
        elif self.rsd:
            try:
                self.bridge.run(self.rsd.close(), timeout=15)
            except Exception:
                pass
        self.userspace_tunnel = None
        self.rsd = None
        self.device_info["connected"] = False
        self.device_info["connection_type"] = None
        self.device_info["tunnel_mode"] = None

    def reconnect(self, udid=None, prefer_wifi=False, retries=15, delay=2):
        if udid is None:
            udid = self.device_info.get("udid")
        self.disconnect()
        return self.connect(udid=udid, prefer_wifi=prefer_wifi, retries=retries, delay=delay)

    def get_device_info(self):
        return dict(self.device_info)

    def get_tunnel_status(self):
        return {
            "running": bool(self.device_info.get("connected") and self.rsd),
            "mode": self.device_info.get("tunnel_mode"),
        }

    def shutdown(self):
        self._auto_reconnect = False
        self.disconnect()
