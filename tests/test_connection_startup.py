import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

import app as app_module
from device_manager import DeviceManager, DvtProvider
from main_app import start_backend
from transport_tunnel import TransportTunnel, upstream
from updater import check_release


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        with patch('device_manager.atexit.register'), patch('device_manager.AsyncBridge'):
            self.manager = DeviceManager()

    def test_wifi_selects_wifi_device_instead_of_first_usb_device(self):
        self.manager.bridge.run.return_value = [
            {'udid': 'usb', 'connection_types': ['USB']},
            {'udid': 'wifi', 'connection_types': ['USB', 'WiFi']},
        ]
        with patch.object(self.manager, '_discover_mux_devices', Mock(return_value=None)):
            self.assertEqual(self.manager._wait_for_device(prefer_wifi=True, retries=1), ('wifi', 'WiFi'))

    def test_wifi_does_not_silently_use_usb(self):
        self.manager.bridge.run.return_value = [{'udid': 'usb', 'connection_types': ['USB']}]
        with patch.object(self.manager, '_discover_mux_devices', Mock(return_value=None)), patch('device_manager.time.sleep') as sleep:
            with self.assertRaisesRegex(ConnectionError, 'Wi-Fi'):
                self.manager._wait_for_device(prefer_wifi=True, retries=1)
            sleep.assert_not_called()

    def test_failure_cleans_session_and_reports_error(self):
        with patch.object(self.manager, '_connect_locked', side_effect=RuntimeError('failed')), patch.object(self.manager, '_disconnect_locked') as cleanup:
            with self.assertRaises(ConnectionError):
                self.manager.connect()
            cleanup.assert_called_once()
            self.assertFalse(self.manager.device_info['connecting'])
            self.assertEqual(self.manager.device_info['error'], 'failed')

    def test_duplicate_request_returns_immediately(self):
        with app_module._state_lock:
            with app_module.app.test_client() as client:
                response = client.post('/api/device/connect', json={})
            self.assertEqual(response.status_code, 409)

    def test_auto_reconnect_retries_a_closed_wifi_channel(self):
        manager = self.manager
        manager._auto_reconnect = True
        manager._reconnect_target = ('wifi-phone', True)
        manager.device_info.update(connected=False, connecting=False)

        def finish_iteration(*args):
            manager._auto_reconnect = False

        with patch.object(manager, 'disconnect'), patch.object(manager, 'connect',
                return_value={'connection_type': 'WiFi'}) as connect, patch(
                'device_manager.time.sleep', finish_iteration):
            manager._reconnect_loop()
        connect.assert_called_once_with(udid='wifi-phone', prefer_wifi=True, retries=3, delay=2)

    def test_switch_stops_all_writers_and_detaches_failed_session(self):
        manager = Mock()
        manager.device_info = {'connected': True}
        manager.reconnect.side_effect = RuntimeError('offline')
        service = Mock()
        with patch.object(app_module, 'device_mgr', manager), patch.object(app_module, 'loc_svc', service):
            response = app_module.app.test_client().post('/api/device/switch', json={'wifi': True})
        self.assertEqual(response.status_code, 500)
        service.stop_route.assert_called_once()
        service.stop_wander.assert_called_once()
        service.joystick_stop.assert_called_once()
        service.attach_device.assert_called_once_with(None, None)

    def test_native_server_starts_without_connecting_phone(self):
        old_origins = app_module._ALLOWED_ORIGINS
        with patch('main_app.DeviceManager') as factory, patch.object(app_module, 'device_mgr', None):
            server = start_backend()
            try:
                factory.return_value.connect.assert_not_called()
                import requests
                response = requests.get(f'http://127.0.0.1:{server.port}/', timeout=2)
                self.assertEqual(response.status_code, 200)
            finally:
                server.shutdown()
                server.server_close()
                app_module._ALLOWED_ORIGINS = old_origins


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_wifi_lockdown_and_cleanup_on_failure(self):
        lockdown = Mock(close=AsyncMock())
        with patch('transport_tunnel.create_using_usbmux', AsyncMock(return_value=lockdown)) as create, patch.object(upstream.tunnel_service.CoreDeviceTunnelProxy, 'create', AsyncMock(side_effect=RuntimeError('no service'))):
            tunnel = TransportTunnel('test-phone', 'WiFi')
            with self.assertRaisesRegex(RuntimeError, 'no service'):
                await tunnel.aopen()
            create.assert_awaited_once_with(serial='test-phone', autopair=False, connection_type='Network')
            lockdown.close.assert_awaited_once()
            self.assertFalse(upstream.tunnel_service.USE_USERSPACE_TUNNEL)

    async def test_cancelled_open_closes_lockdown(self):
        lockdown = Mock(close=AsyncMock())
        with patch('transport_tunnel.create_using_usbmux', AsyncMock(return_value=lockdown)), patch.object(upstream.tunnel_service.CoreDeviceTunnelProxy, 'create', AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await TransportTunnel('test-phone', 'USB').aopen()
            lockdown.close.assert_awaited_once()
            self.assertFalse(upstream.tunnel_service.USE_USERSPACE_TUNNEL)


class DeveloperReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_advertised_dvt_skips_slow_image_mounter(self):
        with patch('device_manager.atexit.register'), patch('device_manager.AsyncBridge'):
            manager = DeviceManager()
        manager.rsd = Mock(peer_info={"Properties": {}, "Services": {DvtProvider.RSD_SERVICE_NAME: {}}},
                           get_developer_mode_status=AsyncMock(return_value=True))
        provider = Mock(connect=AsyncMock())
        simulator = Mock(connect=AsyncMock())
        with patch('device_manager.MobileImageMounterService') as mounter, patch('device_manager.DvtProvider', return_value=provider) as provider_class, patch('device_manager.LocationSimulation', return_value=simulator):
            provider_class.RSD_SERVICE_NAME = DvtProvider.RSD_SERVICE_NAME
            await manager._initialize_developer_services()
            mounter.assert_not_called()
            provider.connect.assert_awaited_once()
            simulator.connect.assert_awaited_once()
            self.assertTrue(manager.device_info['ddi_mounted'])
            manager.device_info['connected'] = True
            simulator.service.on_closed('connection closed')
            self.assertFalse(manager.device_info['connected'])
            self.assertIn('Reconnect', manager.device_info['error'])
            manager._generation += 1
            manager.device_info['connected'] = True
            simulator.service.on_closed('old session closing')
            self.assertTrue(manager.device_info['connected'])


class UpdateTests(unittest.TestCase):
    def test_native_check_uses_local_api_and_rejects_foreign_origins(self):
        handler = Mock(return_value={"handled": True})
        with patch.object(app_module, '_native_update_check', handler):
            client = app_module.app.test_client()
            self.assertEqual(client.post('/api/update/check').json, {"handled": True})
            self.assertEqual(client.post('/api/update/check', headers={"Origin": "https://example.com"}).status_code, 403)
            handler.assert_called_once()

    def test_browser_build_falls_back_to_release_check(self):
        with patch.object(app_module, '_native_update_check', None):
            self.assertEqual(app_module.app.test_client().post('/api/update/check').json, {"handled": False})

    def check(self, tag, **kwargs):
        response = Mock(status_code=200)
        response.json.return_value = dict(tag_name=tag, **kwargs)
        with patch('updater.requests.get', return_value=response):
            return check_release()

    def test_numeric_version_comparison(self):
        self.assertTrue(self.check('v2.10.0')['available'])
        self.assertFalse(self.check('v2.1.9')['available'])
        self.assertFalse(self.check('v2.2.0')['available'])

    def test_prereleases_and_invalid_tags_are_not_offered(self):
        for tag in ('v3.0.0-beta', 'malformed'):
            with self.assertRaises(ValueError):
                self.check(tag)
        with self.assertRaises(ValueError):
            self.check('v3.0.0', prerelease=True)

    def test_network_failure_returns_actionable_error(self):
        with patch('app.check_release', side_effect=TimeoutError):
            response = app_module.app.test_client().get('/api/update')
        self.assertEqual(response.status_code, 502)
        self.assertIn('try again', response.json['error'])
