# SPDX-License-Identifier: GPL-3.0-or-later
# Adapted from pymobiledevice3 10.7.1 userspace_tunnel.py (Dor H. and contributors).
# Modification: explicitly select the paired USB/Network lockdown transport.
"""Transport-selecting adapter for pymobiledevice3 10.7.1's userspace stack.

The upstream handle does not accept connection_type. Keep its singleton and
cleanup contract, but open lockdown explicitly on USB or Network. Revisit this
adapter when upgrading pymobiledevice3; do not monkeypatch its factories.
"""
from contextlib import AsyncExitStack

from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.remote import userspace_tunnel as upstream
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService


class TransportTunnel(upstream.UserspaceRsdTunnel):
    def __init__(self, serial, connection_type):
        super().__init__(serial=serial, autopair=False)
        self.connection_type = connection_type

    async def aopen(self):
        if self.rsd is not None:
            return self.rsd
        if upstream._active_tunnel is not None:
            raise RuntimeError("Another userspace tunnel is already active")
        stack = AsyncExitStack()
        upstream.tunnel_service.USE_USERSPACE_TUNNEL = True
        try:
            lockdown = await create_using_usbmux(
                serial=self.serial, autopair=False,
                connection_type="Network" if self.connection_type == "WiFi" else "USB",
            )
            stack.push_async_callback(lockdown.close)
            provider = await upstream.tunnel_service.CoreDeviceTunnelProxy.create(lockdown)
            stack.push_async_callback(provider.close)
            result = await stack.enter_async_context(provider.start_tcp_tunnel())
            self.tun = result.client.tun
            self.tun.set_peer(result.address)
            plane = await stack.enter_async_context(upstream.UserspaceDialPlane(self.tun, result.address))
            rsd = RemoteServiceDiscoveryService((result.address, result.port), open_connection=plane.dial)
            stack.push_async_callback(rsd.close)
            await rsd.connect()
        except BaseException:
            try:
                await stack.aclose()
            finally:
                upstream.tunnel_service.USE_USERSPACE_TUNNEL = False
                self.tun = None
            raise
        self._exit_stack = stack
        self.rsd = rsd
        upstream._active_tunnel = self
        upstream.USERSPACE_ACTIVE = True
        return rsd
