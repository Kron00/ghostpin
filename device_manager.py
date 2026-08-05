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


def mask_udid(udid):
    """Shorten a UDID for display. Terminal output and the UI end up in
    screenshots attached to bug reports; a full UDID identifies the device."""
    if not udid:
        return "unknown"
    text = str(udid)
    if len(text) <= 12:
        return "…"
    return f"{text[:6]}…{text[-4:]}"


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
        try:
            return future.result(timeout=timeout)
        except BaseException:
            # Waiting on the result is not the same as stopping the work. A
            # timed-out coroutine keeps running on the loop and can complete
            # later, overwriting state the caller has already torn down and
            # replaced — so cancel it before propagating.
            future.cancel()
            raise


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
        self._pre_reconnect_callback = None
        # One lock owns the whole session lifecycle: connect, disconnect,
        # reconnect. Without it the background reconnect thread can tear down
        # the provider while a request thread is mid-connect. Re-entrant
        # because connect() calls disconnect() on its own failure path.
        # Callbacks are always invoked *outside* this lock: they take the
        # app-level state lock, and holding both in opposite orders deadlocks.
        self._session_lock = threading.RLock()
        self._generation = 0
        atexit.register(self.shutdown)

    # ── Auto-reconnect ─────────────────────────────────────

    def enable_auto_reconnect(self, callback=None, pre_callback=None):
        """Start background auto-reconnect.

        pre_callback() runs before the session is torn down, so the caller can
        stop anything still writing to the old DVT channel. callback(info) runs
        after a successful reconnect.
        """
        self._auto_reconnect = True
        self._reconnect_callback = callback
        self._pre_reconnect_callback = pre_callback
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

                # Stop the old session's writers before anything is closed
                # under them, and do it outside the session lock.
                if self._pre_reconnect_callback:
                    try:
                        self._pre_reconnect_callback()
                    except Exception:
                        pass

                info = None
                try:
                    with self._session_lock:
                        prefer_wifi = self.device_info.get("connection_type") == "WiFi"
                        udid = self.device_info.get("udid")
                        self.disconnect()
                        info = self.connect(
                            udid=udid, prefer_wifi=prefer_wifi, retries=3, delay=2
                        )
                    print(f"[+] Auto-reconnected ({info.get('connection_type', 'USB')})")
                except Exception as exc:
                    print(f"[!] Auto-reconnect failed: {exc}")

                if info and self._reconnect_callback:
                    try:
                        self._reconnect_callback(info)
                    except Exception:
                        pass
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

    async def _open_userspace_tunnel(self, udid, generation):
        tunnel = UserspaceRsdTunnel(serial=udid)
        try:
            rsd = await tunnel.aopen()
        except BaseException:
            await tunnel.aclose()
            raise
        # If this attempt timed out and the caller has already moved on to the
        # fallback, publishing here would overwrite the live session and leave
        # cleanup closing the wrong one. Discard our own work instead.
        if generation != self._generation:
            await tunnel.aclose()
            raise ConnectionError("Userspace tunnel completed after the attempt was abandoned")
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

        # The tunneld fallback takes an address from an unauthenticated local
        # HTTP endpoint, so confirm the device that answered is the one we
        # selected before driving its location.
        peer_udid = properties.get("UniqueDeviceID")
        expected_udid = self.device_info.get("udid")
        if peer_udid and expected_udid and peer_udid != expected_udid:
            raise RuntimeError(
                "Connected device identity does not match the selected device "
                f"(expected {mask_udid(expected_udid)}, got {mask_udid(peer_udid)})."
            )
        self.device_info["udid"] = peer_udid or expected_udid

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
        with self._session_lock:
            return self._connect_locked(udid, prefer_wifi, retries, delay)

    def _connect_locked(self, udid, prefer_wifi, retries, delay):
        selected_udid, connection_type = self._wait_for_device(
            udid=udid, prefer_wifi=prefer_wifi, retries=retries, delay=delay
        )
        self.device_info["udid"] = selected_udid
        self.device_info["connection_type"] = connection_type

        self._generation += 1
        generation = self._generation

        try:
            self.bridge.run(
                self._open_userspace_tunnel(selected_udid, generation), timeout=90
            )
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
            f"iOS {self.device_info['ios_version']} | {self.device_info['model']} | "
            f"{mask_udid(self.device_info['udid'])} | DDI mounted"
        )
        return dict(self.device_info)

    def disconnect(self):
        with self._session_lock:
            self._disconnect_locked()

    def _disconnect_locked(self):
        # Anything still in flight for this session must not land on the next.
        self._generation += 1
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
        with self._session_lock:
            if udid is None:
                udid = self.device_info.get("udid")
            self._disconnect_locked()
            return self._connect_locked(udid, prefer_wifi, retries, delay)

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
