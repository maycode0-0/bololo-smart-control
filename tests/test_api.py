"""Wire-contract tests using APK-observed request and response formats."""

import asyncio
import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp

_SPEC = importlib.util.spec_from_file_location(
    "bololo_api", Path(__file__).resolve().parents[1] / "custom_components/bololo/api.py"
)
api = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(api)


class Response:
    def __init__(self, payload=None, status=200, exception=None):
        self.payload = payload
        self.status = status
        self.exception = exception

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self, **kwargs):
        await asyncio.sleep(0)
        if self.exception:
            raise self.exception
        return self.payload


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def success(data):
    return Response({"code": 200, "data": data})


def login_data():
    return {
        "uid": "account-1",
        "jwtAuthenticationDto": {"token": "new-token"},
        "refreshToken": "refresh-1",
        "createdAt": 100,
        "expiredAt": 500,
    }


DEVICE = {"mac": "AA:BB", "productKey": "product-1", "sno": "serial-1"}


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_observed_envelope_and_token_location(self):
        session = Session(success(login_data()))
        client = api.BololoClient(session, "phone", "password", "86")
        await client.async_login()
        self.assertEqual(client.uid, "account-1")
        self.assertEqual(client.token, "new-token")
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, api.BASE_URL + "app/smart_home/login/pwd")
        self.assertEqual(
            kwargs["json"],
            {
                "appKey": api.APP_ID,
                "data": {
                    "account": "phone",
                    "password": "password",
                    "mobileCountryCode": "86",
                    "lang": "zh",
                    "refreshToken": True,
                },
                "version": "1.0",
            },
        )
        self.assertEqual(kwargs["headers"]["X-Gizwits-Application-Id"], api.APP_ID)
        self.assertEqual(kwargs["headers"]["Version"], "1.0")
        self.assertNotIn("Authorization", kwargs["headers"])
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["timeout"].total, 20)

    async def test_get_devices_uses_raw_authorization_and_array(self):
        session = Session(success([DEVICE]))
        client = api.BololoClient(session, token="token")
        self.assertEqual(await client.async_get_devices(), [DEVICE])
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "GET")
        self.assertTrue(url.endswith("app/smartHome/v2/users/devices"))
        self.assertEqual(kwargs["headers"]["Authorization"], "token")
        self.assertNotIn("json", kwargs)

    async def test_status_response_is_flat_and_request_data_is_object(self):
        attrs = {"Power": True, "Temperature": 38, "status_time": 12345}
        session = Session(success(attrs))
        client = api.BololoClient(session, token="token")
        self.assertEqual(await client.async_get_status(DEVICE), attrs)
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("bololo/app/device/status/product-1/AA%3ABB"))
        self.assertEqual(kwargs["json"]["data"], {})

    async def test_control_data_is_json_string_inside_envelope(self):
        session = Session(success(None))
        client = api.BololoClient(session, token="token")
        attrs = {"Power": False, "Set_Temp": 40}
        await client.async_control(DEVICE, attrs)
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("bololo/app/device/control/product-1/AA%3ABB"))
        self.assertIsInstance(kwargs["json"]["data"], str)
        self.assertEqual(json.loads(kwargs["json"]["data"]), attrs)
        self.assertEqual(kwargs["json"]["appKey"], api.APP_ID)

    async def test_refresh_at_half_lifetime_uses_snake_case_response(self):
        session = Session(
            success(login_data()),
            success(
                {
                    "token": "refreshed",
                    "refresh_token": "refresh-2",
                    "created_at": 350,
                    "expired_at": 1000,
                    "uid": "account-1",
                }
            ),
            success([DEVICE]),
        )
        client = api.BololoClient(session, "phone", "password")
        await client.async_login()
        with patch.object(api.time, "time", return_value=350):
            await client.async_get_devices()
        _, url, kwargs = session.calls[1]
        self.assertTrue(url.endswith("app/user/refresh_token"))
        self.assertEqual(
            kwargs["json"]["data"],
            {"clientId": api.APP_ID, "refresh": True, "refresh_token": "refresh-1"},
        )
        self.assertEqual(session.calls[2][2]["headers"]["Authorization"], "refreshed")
        self.assertEqual(client.token, "refreshed")

    async def test_expired_read_reauthenticates_and_retries_once(self):
        session = Session(Response({"code": 505}), success(login_data()), success([DEVICE]))
        client = api.BololoClient(session, "phone", "password", token="expired")
        self.assertEqual(await client.async_get_devices(), [DEVICE])
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(session.calls[2][2]["headers"]["Authorization"], "new-token")

    async def test_token_only_auth_failure_is_not_silently_empty_devices(self):
        session = Session(Response({"code": 526}))
        client = api.BololoClient(session, token="expired")
        with self.assertRaises(api.BololoAuthError):
            await client.async_get_devices()
        self.assertEqual(len(session.calls), 1)

    async def test_control_auth_rejection_is_never_replayed(self):
        session = Session(Response({"code": 505}))
        client = api.BololoClient(session, "phone", "password", token="expired")
        with self.assertRaises(api.BololoAuthError):
            await client.async_control(DEVICE, {"Power": True})
        self.assertEqual(len(session.calls), 1)

    async def test_control_timeout_is_never_replayed(self):
        session = Session(asyncio.TimeoutError())
        client = api.BololoClient(session, token="token")
        with self.assertRaises(api.BololoConnectionError):
            await client.async_control(DEVICE, {"Power": True})
        self.assertEqual(len(session.calls), 1)

    async def test_concurrent_authentication_logs_in_only_once(self):
        session = Session(success(login_data()))
        client = api.BololoClient(session, "phone", "password")
        with patch.object(api.time, "time", return_value=150):
            await asyncio.gather(*(client.async_ensure_authenticated() for _ in range(10)))
        self.assertEqual(len(session.calls), 1)

    async def test_bad_password_is_auth_error_without_server_message(self):
        session = Session(Response({"code": 1000033, "message": "password sensitive-token"}))
        client = api.BololoClient(session, "phone", "password")
        with self.assertRaises(api.BololoAuthError) as context:
            await client.async_login()
        self.assertNotIn("sensitive", str(context.exception))

    async def test_unknown_login_failure_does_not_mislabel_credentials(self):
        session = Session(Response({"code": 500, "message": "temporary failure"}))
        client = api.BololoClient(session, "phone", "password")
        with self.assertRaises(api.BololoError) as context:
            await client.async_login()
        self.assertNotIsInstance(context.exception, api.BololoAuthError)

    async def test_read_reauthentication_only_retries_once(self):
        session = Session(Response({"code": 505}), success(login_data()), Response({"code": 526}))
        client = api.BololoClient(session, "phone", "password", token="expired")
        with self.assertRaises(api.BololoAuthError):
            await client.async_get_devices()
        self.assertEqual(len(session.calls), 3)

    async def test_rejected_refresh_falls_back_to_account_login(self):
        session = Session(
            success(login_data()), Response({"code": 526}), success(login_data()), success([DEVICE])
        )
        client = api.BololoClient(session, "phone", "password")
        await client.async_login()
        with patch.object(api.time, "time", return_value=350):
            self.assertEqual(await client.async_get_devices(), [DEVICE])
        self.assertTrue(session.calls[1][1].endswith("app/user/refresh_token"))
        self.assertTrue(session.calls[2][1].endswith("app/smart_home/login/pwd"))
        self.assertEqual(len(session.calls), 4)

    async def test_cloud_http_and_json_errors(self):
        cases = [
            (Response(status=503), api.BololoConnectionError),
            (Response(status=401), api.BololoAuthError),
            (Response(status=302), api.BololoError),
            (Response(exception=ValueError("invalid JSON")), api.BololoError),
            (Response([]), api.BololoError),
            (aiohttp.ClientConnectionError(), api.BololoConnectionError),
        ]
        for response, exception_type in cases:
            with self.subTest(exception_type=exception_type):
                session = Session(response)
                client = api.BololoClient(session, token="token")
                with self.assertRaises(exception_type):
                    await client.async_get_devices()

    async def test_invalid_list_and_status_are_rejected(self):
        session = Session(success({"unexpected": []}), success([]))
        client = api.BololoClient(session, token="token")
        with self.assertRaises(api.BololoError):
            await client.async_get_devices()
        with self.assertRaises(api.BololoError):
            await client.async_get_status(DEVICE)

    async def test_invalid_device_and_empty_control_do_not_send(self):
        session = Session()
        client = api.BololoClient(session, token="token")
        with self.assertRaises(api.BololoError):
            await client.async_get_status({"did": "1"})
        with self.assertRaises(api.BololoError):
            await client.async_control(DEVICE, {})
        self.assertEqual(session.calls, [])


class SmsApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_sms_code_token_and_v2_wire_format(self):
        session = Session(success("code-token"), success(None))
        client = api.BololoClient(session, username="13800138000")
        await client.async_request_sms()
        self.assertEqual([call[0] for call in session.calls], ["GET", "POST"])
        self.assertTrue(session.calls[0][1].endswith("app/user/codeToken"))
        self.assertNotIn("json", session.calls[0][2])
        self.assertTrue(session.calls[1][1].endswith("app/user/v2/mobileCode"))
        self.assertEqual(
            session.calls[1][2]["json"]["data"],
            {"mobile": "+8613800138000", "lang": "zh", "token": "code-token"},
        )
        for _, _, kwargs in session.calls:
            self.assertNotIn("Authorization", kwargs["headers"])
            self.assertFalse(kwargs["allow_redirects"])

    async def test_missing_code_token_uses_legacy_route_without_guessing_token(self):
        session = Session(success(None), success(None))
        await api.BololoClient(session, username="13800138000").async_request_sms()
        self.assertTrue(session.calls[1][1].endswith("app/user/mobileCode"))
        self.assertNotIn("token", session.calls[1][2]["json"]["data"])

    async def test_captcha_is_passed_as_headers_and_never_as_sms_body(self):
        session = Session(success("code-token"), success(None))
        await api.BololoClient(session, username="13800138000").async_request_sms(
            "image-id", "ABCD"
        )
        kwargs = session.calls[1][2]
        self.assertEqual(kwargs["headers"]["X-Gizwits-Captcha-Id"], "image-id")
        self.assertEqual(kwargs["headers"]["X-Gizwits-Captcha-Code"], "ABCD")
        self.assertNotIn("captchaCode", kwargs["json"]["data"])

    async def test_sms_login_stores_session_without_storing_code_or_password(self):
        saved = AsyncMock()
        session = Session(success(login_data()))
        client = api.BololoClient(
            session, username="13800138000", mobile_country_code="CN", on_auth_update=saved
        )
        await client.async_login_sms("012345")
        self.assertTrue(session.calls[0][1].endswith("app/user/loginByMobile"))
        self.assertEqual(
            session.calls[0][2]["json"]["data"],
            {
                "mobile": "+8613800138000",
                "code": "012345",
                "homeName": "我的家",
                "mobileCountryCode": "CN",
                "lang": "zh",
            },
        )
        self.assertEqual(client.auth["refresh_token"], "refresh-1")
        saved.assert_awaited_once_with(client.auth)
        self.assertNotIn("012345", repr(vars(client)))
        copy = client.auth
        copy["token"] = "changed"
        self.assertEqual(client.token, "new-token")

    async def test_sms_restart_refreshes_saved_session_without_sms_or_password(self):
        session = Session(
            success(
                {
                    "token": "rotated",
                    "refresh_token": "next",
                    "created_at": 400,
                    "expired_at": 1200,
                    "uid": "account-1",
                }
            ),
            success([DEVICE]),
        )
        saved = AsyncMock()
        client = api.BololoClient(
            session,
            auth={
                "token": "old",
                "refresh_token": "refresh-1",
                "created_at": 100,
                "expired_at": 500,
                "uid": "account-1",
            },
            on_auth_update=saved,
        )
        with patch.object(api.time, "time", return_value=400):
            self.assertEqual(await client.async_get_devices(), [DEVICE])
        self.assertEqual(len(session.calls), 2)
        self.assertTrue(session.calls[0][1].endswith("app/user/refresh_token"))
        self.assertEqual(session.calls[1][2]["headers"]["Authorization"], "rotated")
        saved.assert_awaited_once()

    async def test_rejected_sms_session_requires_user_reauth_and_sends_no_code(self):
        session = Session(Response({"code": 526}))
        client = api.BololoClient(
            session,
            auth={
                "token": "old",
                "refresh_token": "refresh-1",
                "created_at": 100,
                "expired_at": 500,
            },
        )
        with patch.object(api.time, "time", return_value=400):
            with self.assertRaises(api.BololoAuthError):
                await client.async_get_devices()
        self.assertEqual(len(session.calls), 1)
        self.assertTrue(session.calls[0][1].endswith("refresh_token"))

    async def test_persistence_failure_does_not_expose_rotated_token(self):
        saved = AsyncMock(side_effect=api.BololoConnectionError("disk failed"))
        session = Session(success({"token": "rotated", "refresh_token": "next"}))
        client = api.BololoClient(
            session,
            auth={"token": "old", "refresh_token": "refresh-1", "expired_at": 100},
            on_auth_update=saved,
        )
        with self.assertRaises(api.BololoConnectionError):
            await client.async_get_devices()
        self.assertEqual(client.token, "old")
        self.assertEqual(len(session.calls), 1)

    async def test_sms_errors_are_classified_and_redacted(self):
        for code, expected in [
            (610, api.BololoCaptchaRequired),
            (612, api.BololoCaptchaRequired),
            (1000018, api.BololoCaptchaRequired),
            (8000005, api.BololoRateLimitError),
            (53, api.BololoRateLimitError),
            (1000034, api.BololoAuthError),
            (1000035, api.BololoAuthError),
        ]:
            session = Session(Response({"code": code, "message": "private-otp"}))
            with self.subTest(code=code), self.assertRaises(expected) as error:
                await api.BololoClient(session, username="13800138000").async_login_sms("code")
            self.assertNotIn("private-otp", str(error.exception))

    async def test_captcha_image_alias_and_unsafe_content(self):
        session = Session(
            success({"id": "challenge", "imageBase64": "aW1hZ2U="}),
            success({"id": "challenge", "image": "https://untrusted/image) [link](bad"}),
        )
        client = api.BololoClient(session)
        self.assertEqual(
            await client.async_get_captcha(),
            {"id": "challenge", "image": "data:image/jpeg;base64,aW1hZ2U="},
        )
        with self.assertRaises(api.BololoError):
            await client.async_get_captcha()


if __name__ == "__main__":
    unittest.main()
