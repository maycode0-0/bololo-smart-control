"""Persist rotating sessions and keep obsolete runtimes from writing secrets."""

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_hybrid_integration as hybrid_tests
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.util.file import WriteError

from custom_components.bololo import (
    _AuthSession,
    _AuthStore,
    _session_fingerprint,
    async_unload_entry,
)
from custom_components.bololo.api import BololoAuthError, BololoConnectionError

AUTH = {
    "token": "bootstrap-token",
    "refresh_token": "refresh-token",
    "uid": "account-1",
    "created_at": 10.0,
    "expired_at": 100.0,
}
ROTATED = {**AUTH, "token": "rotated-token", "refresh_token": "rotated-refresh"}


class AuthSessionTests(unittest.IsolatedAsyncioTestCase):
    def make_session(self, stored=None):
        entry = SimpleNamespace(
            unique_id="account-1",
            data={
                "username": "13800000000",
                "country_code": "86",
                "auth_method": "sms",
                "auth": dict(AUTH),
            },
        )
        store = SimpleNamespace(async_load=AsyncMock(return_value=stored), async_save=AsyncMock())
        return _AuthSession(entry, store), entry, store

    async def test_matching_store_restores_latest_rotation(self):
        session, entry, store = self.make_session()
        store.async_load.return_value = {
            "session_fingerprint": _session_fingerprint(entry),
            "auth": ROTATED,
        }
        restored = await session.async_load()
        self.assertEqual(restored, ROTATED)
        restored["token"] = "caller-mutation"
        self.assertEqual((await session.async_load())["token"], "rotated-token")

    async def test_reauth_invalidates_previous_store_and_runtime(self):
        old, entry, store = self.make_session()
        store.async_load.return_value = {"session_fingerprint": old.fingerprint, "auth": ROTATED}
        entry.data = {**entry.data, "auth": {**AUTH, "token": "new-sms-login"}}
        renewed = _AuthSession(entry, store)
        self.assertEqual((await renewed.async_load())["token"], "new-sms-login")
        with self.assertRaises(BololoConnectionError):
            await old.async_save(ROTATED)
        store.async_save.assert_not_awaited()

    async def test_password_reauth_also_invalidates_old_rotations(self):
        session, entry, store = self.make_session()
        entry.data = {"username": "user", "password": "old"}
        old_password_session = _AuthSession(entry, store)
        store.async_load.return_value = {
            "session_fingerprint": old_password_session.fingerprint,
            "auth": ROTATED,
        }
        entry.data["password"] = "new"
        renewed = _AuthSession(entry, store)
        self.assertIsNone(await renewed.async_load())

    async def test_other_account_store_and_rotation_are_rejected(self):
        session, entry, store = self.make_session()
        store.async_load.return_value = {
            "session_fingerprint": session.fingerprint,
            "auth": {**ROTATED, "uid": "other-account"},
        }
        self.assertEqual(await session.async_load(), AUTH)
        with self.assertRaises(BololoAuthError):
            await session.async_save({**ROTATED, "uid": "other-account"})
        entry.data = {**entry.data, "auth": {**AUTH, "uid": "other-account"}}
        with self.assertRaises(BololoAuthError):
            await _AuthSession(entry, store).async_load()
        store.async_save.assert_not_awaited()

    async def test_save_excludes_unrelated_credentials(self):
        session, entry, store = self.make_session()
        await session.async_save({**ROTATED, "password": "secret", "phone": "13800000000"})
        store.async_save.assert_awaited_once_with(
            {"session_fingerprint": session.fingerprint, "auth": ROTATED}
        )

    async def test_store_failure_blocks_using_new_session_without_exposing_secrets(self):
        session, entry, store = self.make_session()
        store.async_save.side_effect = OSError("disk failure")
        with self.assertRaisesRegex(
            BololoConnectionError, "Unable to persist BOLOLO authentication session"
        ) as context:
            await session.async_save(ROTATED)
        self.assertNotIn("rotated-token", str(context.exception))

    async def test_shutdown_drains_inflight_save_and_rejects_later_callbacks(self):
        session, entry, store = self.make_session()
        started = asyncio.Event()
        release = asyncio.Event()

        async def save(value):
            started.set()
            await release.wait()

        store.async_save.side_effect = save
        saving = asyncio.create_task(session.async_save(ROTATED))
        await started.wait()
        closing = asyncio.create_task(session.async_close())
        # An event loop turn lets close deactivate callbacks while waiting on IO.
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        release.set()
        await asyncio.gather(saving, closing)
        with self.assertRaises(BololoConnectionError):
            await session.async_save(AUTH)
        store.async_save.assert_awaited_once()

    async def test_actual_ha_store_write_errors_are_not_silently_suppressed(self):
        session, entry, _ = self.make_session()
        hass = HomeAssistant(str(Path(__file__).parent))
        session.store = _AuthStore(hass, 1, "test.auth-runtime", private=True)
        with patch(
            "homeassistant.helpers.storage.Store._async_write_data",
            new=AsyncMock(side_effect=WriteError("disk write failed")),
        ):
            with self.assertRaises(BololoConnectionError):
                await session.async_save(ROTATED)

    async def test_stopping_and_readonly_storage_reject_undurable_rotation(self):
        session, entry, _ = self.make_session()
        hass = HomeAssistant(str(Path(__file__).parent))
        session.store = _AuthStore(hass, 1, "test.auth-runtime", private=True)
        hass.state = CoreState.stopping
        with self.assertRaises(BololoConnectionError):
            await session.async_save(ROTATED)
        hass.state = CoreState.not_running
        session.store.make_read_only()
        with self.assertRaises(BololoConnectionError):
            await session.async_save(ROTATED)


class SmsRuntimeTests(unittest.IsolatedAsyncioTestCase):
    setup = hybrid_tests.HybridSetupTests.setup
    asyncTearDown = hybrid_tests.HybridSetupTests.asyncTearDown

    async def asyncSetUp(self):
        await hybrid_tests.HybridSetupTests.asyncSetUp(self)
        self.entry.unique_id = "account-1"
        self.entry.data.pop("password")
        self.entry.data.update(auth_method="sms", auth=dict(AUTH))

    async def test_sms_cloud_mode_ensures_session_without_password_login(self):
        self.entry.options = {"connection_mode": "cloud"}
        factory, _ = await self.setup()
        self.cloud.async_ensure_authenticated.assert_awaited_once()
        self.cloud.async_login.assert_not_awaited()
        self.assertEqual(factory.call_args.kwargs["auth"], AUTH)
        self.assertEqual(factory.call_args.kwargs["password"], "")
        callback = factory.call_args.kwargs["on_auth_update"]
        await callback(ROTATED)
        self.assertEqual(self.auth_store.async_save.call_args.args[0]["auth"], ROTATED)

    async def test_sms_auto_with_cache_never_authenticates_during_offline_start(self):
        factory, _ = await self.setup()
        self.cloud.async_ensure_authenticated.assert_not_awaited()
        self.cloud.async_login.assert_not_awaited()
        self.assertEqual(factory.call_args.kwargs["auth"], AUTH)

    async def test_restored_rotation_is_passed_to_runtime_client(self):
        self.auth_store.async_load.return_value = {
            "session_fingerprint": _session_fingerprint(self.entry),
            "auth": ROTATED,
        }
        factory, _ = await self.setup()
        self.assertEqual(factory.call_args.kwargs["auth"], ROTATED)

    async def test_expired_sms_session_triggers_reauth_without_password_login(self):
        self.entry.options = {"connection_mode": "cloud"}
        self.cloud.async_ensure_authenticated.side_effect = BololoAuthError("expired")
        with self.assertRaises(ConfigEntryAuthFailed):
            await self.setup()
        self.cloud.async_login.assert_not_awaited()

    async def test_unloaded_runtime_callback_cannot_write_store(self):
        factory, _ = await self.setup()
        callback = factory.call_args.kwargs["on_auth_update"]
        await async_unload_entry(self.hass, self.entry)
        with self.assertRaises(BololoConnectionError):
            await callback(ROTATED)
        self.auth_store.async_save.assert_not_awaited()
