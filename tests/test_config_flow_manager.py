"""Exercise SMS steps through HA's actual flow manager, not direct method calls."""

import shutil
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from homeassistant import auth, loader
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from test_api import Response, Session, success
from test_setup import isolated_config_directory

from custom_components.bololo.const import DOMAIN


class SmsFlowManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_sms_request_and_code_exchange_finish_real_managed_flow(self):
        with (
            isolated_config_directory() as directory,
            patch("homeassistant.helpers.storage.Store._async_write_data", new=AsyncMock()),
        ):
            source = Path(__file__).resolve().parents[1] / "custom_components" / DOMAIN
            shutil.copytree(source, Path(directory) / "custom_components" / DOMAIN)
            hass = HomeAssistant(directory)
            hass.auth = await auth.auth_manager_from_config(hass, [], [])
            loader.async_setup(hass)
            hass.config_entries = ConfigEntries(hass, {})
            session = Session(
                success("test-code-token"),
                Response({"code": 610}),
                success({"captchaId": "test-challenge", "captchaImage": "iVBORw0KGgo="}),
                success("test-code-token-2"),
                success(None),
                Response({"code": 1000034}),
                success(
                    {
                        "uid": "flow-account",
                        "jwtAuthenticationDto": {"token": "test-session"},
                        "refreshToken": "test-refresh",
                        "createdAt": time.time(),
                        "expiredAt": time.time() + 3600,
                    }
                ),
                success([]),
            )
            try:
                with (
                    patch(
                        "custom_components.bololo.config_flow.async_get_clientsession",
                        return_value=session,
                    ),
                    patch(
                        "custom_components.bololo.async_setup_entry",
                        new=AsyncMock(return_value=True),
                    ),
                ):
                    result = await hass.config_entries.flow.async_init(
                        DOMAIN, context={"source": "user"}
                    )
                    self.assertEqual(result["step_id"], "user")
                    flow_id = result["flow_id"]
                    result = await hass.config_entries.flow.async_configure(
                        flow_id,
                        {
                            "username": "13800138000",
                            "country_code": "86",
                            "mobile_country_code": "CN",
                            "auth_method": "sms",
                            "connection_mode": "auto",
                        },
                    )
                    self.assertEqual(result["step_id"], "captcha")
                    self.assertTrue(
                        result["description_placeholders"]["captcha_image"].startswith(
                            "/api/bololo/captcha/"
                        )
                    )
                    self.assertEqual(len(session.calls), 3)
                    result = await hass.config_entries.flow.async_configure(
                        flow_id, {"captcha_code": "ABCD"}
                    )
                    self.assertEqual(result["step_id"], "sms")
                    self.assertEqual(len(session.calls), 5)
                    result = await hass.config_entries.flow.async_configure(
                        flow_id, {"code": "000000"}
                    )
                    self.assertEqual(result["errors"], {"base": "invalid_code"})
                    result = await hass.config_entries.flow.async_configure(
                        flow_id, {"code": "123456"}
                    )
                    self.assertEqual(result["type"], "create_entry")
                    self.assertEqual(result["result"].data["auth"]["token"], "test-session")
                    self.assertNotIn("123456", repr(dict(result["result"].data)))
                    self.assertEqual(len(hass.config_entries.async_entries(DOMAIN)), 1)
                    await hass.async_block_till_done()
            finally:
                await hass.async_stop(force=True)
