"""Serve short-lived captcha images through HA's authenticated HTTP interface."""

from __future__ import annotations

import base64
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later

_DATA_KEY = "bololo_captcha_images"
_TTL = 300
_MAX_IMAGES = 32
_MAX_IMAGE_BYTES = 256 * 1024
_PATH = "/api/bololo/captcha/"


@dataclass
class _Image:
    body: bytes
    content_type: str
    expires: float
    cancel: Callable[[], None]


class CaptchaImages(HomeAssistantView):
    """Require HA authentication; signed URLs grant access to one image only."""

    url = _PATH + "{image_id}"
    name = "api:bololo:captcha"
    requires_auth = True

    def __init__(self, hass):
        self.hass = hass
        self.images: dict[str, _Image] = {}

    def remove(self, image_id):
        if image := self.images.pop(image_id, None):
            image.cancel()

    def add(self, data_url):
        metadata, encoded = data_url.split(",", 1)
        content_type = metadata.removeprefix("data:").removesuffix(";base64")
        if content_type not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
            raise ValueError("Unsupported captcha image")
        if len(encoded) > (_MAX_IMAGE_BYTES + 2) // 3 * 4:
            raise ValueError("Captcha image is too large")
        body = base64.b64decode(encoded, validate=True)
        if not body or len(body) > _MAX_IMAGE_BYTES:
            raise ValueError("Invalid captcha image size")
        image_id = secrets.token_urlsafe(24)
        path = _PATH + image_id
        # Use the current HA request/WS user. Never expose an unauthenticated view.
        signed = async_sign_path(self.hass, path, timedelta(seconds=_TTL))
        while len(self.images) >= _MAX_IMAGES:
            self.remove(next(iter(self.images)))

        @callback
        def expire(_):
            self.remove(image_id)

        cancel = async_call_later(self.hass, _TTL, expire)
        self.images[image_id] = _Image(body, content_type, time.monotonic() + _TTL, cancel)
        return image_id, signed

    async def get(self, request, image_id):
        image = self.images.get(image_id)
        if image is None:
            raise web.HTTPNotFound()
        if time.monotonic() >= image.expires:
            self.remove(image_id)
            raise web.HTTPNotFound()
        return web.Response(
            body=image.body,
            content_type=image.content_type,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            },
        )


def async_captcha_images(hass):
    """Register on demand, including before the first config entry exists."""
    if (images := hass.data.get(_DATA_KEY)) is None:
        images = CaptchaImages(hass)
        hass.http.register_view(images)
        hass.data[_DATA_KEY] = images
    return images
