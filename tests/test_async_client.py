"""Tests du client asynchrone — httpx.MockTransport, aucun appel réseau."""

import json

import httpx
import pytest

from fameen_messaging import (
    AsyncFameenMessaging,
    FameenConnectionError,
    MessageResource,
)

pytestmark = pytest.mark.asyncio

API_KEY = "fam_cle_de_test"

MESSAGE_DATA = {
    "sid": "msg_123",
    "status": "queued",
    "channel": "sms",
    "to": "+224620000000",
    "from": "FAMEEN",
    "body": "Bonjour",
    "segments": 1,
    "credits": 1,
    "error": None,
    "externalId": None,
    "statusCallback": None,
    "createdAt": "2026-07-12T10:00:00.000Z",
    "sentAt": None,
    "deliveredAt": None,
}


def envelope(data):
    return {"success": True, "data": data, "message": "OK"}


async def test_async_email_send_deballe_et_en_tetes():
    seen = {}

    def handler(request):
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content.decode("utf-8"))
        seen["path"] = request.url.path
        return httpx.Response(200, json=envelope({**MESSAGE_DATA, "channel": "email"}))

    async with AsyncFameenMessaging(
        api_key=API_KEY, transport=httpx.MockTransport(handler), retry_base=0.0001
    ) as client:
        message = await client.email.send(
            "client@exemple.com", "Bonjour", subject="Objet", idempotency_key="idem-7"
        )

    assert isinstance(message, MessageResource)
    assert message.sid == "msg_123"
    assert message.channel == "email"
    assert seen["path"] == "/api/v1/email/send"
    assert seen["body"] == {"to": "client@exemple.com", "message": "Bonjour", "subject": "Objet"}
    assert seen["headers"]["Authorization"] == f"Bearer {API_KEY}"
    assert seen["headers"]["User-Agent"] == "fameen-messaging-python/0.1.0"
    assert seen["headers"]["Idempotency-Key"] == "idem-7"


async def test_async_retry_429_puis_succes():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "0"},
                json={
                    "success": False,
                    "error": {"code": "rate_limited", "message": "Trop de requêtes"},
                },
            )
        return httpx.Response(200, json=envelope(MESSAGE_DATA))

    async with AsyncFameenMessaging(
        api_key=API_KEY, transport=httpx.MockTransport(handler), retry_base=0.0001
    ) as client:
        message = await client.sms.send("+224620000000", "Bonjour")

    assert message.sid == "msg_123"
    assert calls["n"] == 2


async def test_async_connection_error_apres_epuisement():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectError("réseau injoignable")

    async with AsyncFameenMessaging(
        api_key=API_KEY,
        transport=httpx.MockTransport(handler),
        retry_base=0.0001,
        max_retries=1,
    ) as client:
        with pytest.raises(FameenConnectionError):
            await client.wallet.balance()

    assert calls["n"] == 2  # max_retries=1 → 2 tentatives
