"""Check captcha delivery with HA's actual signed-request authentication."""

import base64
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from homeassistant import auth
from homeassistant.components.http.auth import async_setup_auth
from homeassistant.core import HomeAssistant

from custom_components.bololo.captcha import CaptchaImages

IMAGE = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jXcQAAAAASUVORK5CYII="


class CaptchaHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hass = HomeAssistant(str(Path(__file__).parent))
        self.disk_patch = patch(
            "homeassistant.helpers.storage.Store._async_write_data", new=AsyncMock()
        )
        self.disk_patch.start()
        self.hass.auth = await auth.auth_manager_from_config(self.hass, [], [])
        app = web.Application()
        await async_setup_auth(self.hass, app)
        self.images = CaptchaImages(self.hass)
        self.images.register(self.hass, app, app.router)
        self.client = TestClient(
            TestServer(app), connector=aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver())
        )
        await self.client.start_server()

    async def asyncTearDown(self):
        for identifier in list(self.images.images):
            self.images.remove(identifier)
        await self.client.close()
        await self.hass.async_stop(force=True)
        self.disk_patch.stop()

    async def test_signed_image_renders_without_auth_header_but_view_is_not_public(self):
        identifier, url = self.images.add(IMAGE)
        self.assertTrue(url.startswith("/api/bololo/captcha/"))
        self.assertNotIn("data:image", url)
        response = await self.client.get(url.split("?", 1)[0])
        self.assertEqual(response.status, 401)
        response = await self.client.get(url)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Content-Type"], "image/png")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(await response.read(), base64.b64decode(IMAGE.split(",", 1)[1]))
        response = await self.client.get(url.replace(identifier, "another-image"))
        self.assertEqual(response.status, 401)

    async def test_expired_removed_and_evicted_images_cannot_be_retrieved(self):
        identifier, url = self.images.add(IMAGE)
        self.images.images[identifier].expires = 0
        self.assertEqual((await self.client.get(url)).status, 404)
        self.assertNotIn(identifier, self.images.images)
        identifier, url = self.images.add(IMAGE)
        self.images.remove(identifier)
        self.assertEqual((await self.client.get(url)).status, 404)
        identifier, url = self.images.add(IMAGE)
        for _ in range(32):
            self.images.add(IMAGE)
        self.assertEqual(len(self.images.images), 32)
        self.assertNotIn(identifier, self.images.images)
        self.assertEqual((await self.client.get(url)).status, 404)

    async def test_rejects_active_content_and_oversized_images(self):
        for value in [
            "data:image/svg+xml;base64,PHN2Zz4=",
            "data:image/png;base64," + "A" * 400000,
        ]:
            with self.subTest(value=value[:25]), self.assertRaises(ValueError):
                self.images.add(value)
        self.assertEqual(self.images.images, {})
