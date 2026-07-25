"""Tests des codes de vérification (OTP) — sync et async, sans appel réseau."""

import json

import httpx
import pytest

from fameen_messaging import (
    AsyncFameenMessaging,
    FameenMessaging,
    VerificationResource,
)

API_KEY = "fam_cle_de_test"

PENDING = {
    "verificationId": "ver_1",
    "status": "pending",
    "channel": "sms",
    "to": "+224620000000",
    "attempts": 0,
    "maxAttempts": 5,
    "attemptsRemaining": 5,
    "expiresAt": "2026-07-25T23:05:00.000Z",
    "createdAt": "2026-07-25T23:00:00.000Z",
    "messageSid": "msg_1",
    "champInconnu": "ignoré",  # from_dict doit tolérer les champs inconnus
}


def envelope(data):
    return {"success": True, "data": data, "message": "OK"}


def make_client(handler, **kwargs):
    kwargs.setdefault("retry_base", 0.0001)
    return FameenMessaging(api_key=API_KEY, transport=httpx.MockTransport(handler), **kwargs)


def make_async_client(handler, **kwargs):
    kwargs.setdefault("retry_base", 0.0001)
    return AsyncFameenMessaging(
        api_key=API_KEY, transport=httpx.MockTransport(handler), **kwargs
    )


# ---------------------------------------------------------------------------
# Envoi
# ---------------------------------------------------------------------------


def test_send_poste_sur_otp_send():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=envelope(PENDING))

    result = make_client(handler).otp.send("+224620000000", channel="sms")

    assert seen["url"].endswith("/otp/send")
    assert seen["body"] == {"to": "+224620000000", "channel": "sms"}
    assert isinstance(result, VerificationResource)
    assert result.verification_id == "ver_1"
    assert result.status == "pending"
    assert result.attempts_remaining == 5
    assert result.message_sid == "msg_1"
    assert result.approved is False


def test_send_transmet_les_reglages_ponctuels():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=envelope(PENDING))

    make_client(handler).otp.send(
        "client@exemple.com",
        code_length=8,
        ttl_seconds=600,
        max_attempts=3,
        subject="Votre code",
    )

    assert seen["body"] == {
        "to": "client@exemple.com",
        "codeLength": 8,
        "ttlSeconds": 600,
        "maxAttempts": 3,
        "subject": "Votre code",
    }


def test_send_accepte_une_cle_d_idempotence():
    seen = {}

    def handler(request):
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json=envelope(PENDING))

    make_client(handler).otp.send("+224620000000", idempotency_key="otp-001")
    assert seen["headers"]["idempotency-key"] == "otp-001"


def test_send_refuse_un_destinataire_vide():
    with pytest.raises(ValueError):
        make_client(lambda r: httpx.Response(200)).otp.send("   ")


def test_send_refuse_un_gabarit_sans_marqueur():
    with pytest.raises(ValueError):
        make_client(lambda r: httpx.Response(200)).otp.send(
            "+224620000000", template="Bonjour !"
        )


def test_send_refuse_un_email_sur_canal_sms():
    with pytest.raises(ValueError):
        make_client(lambda r: httpx.Response(200)).otp.send("a@b.c", channel="sms")


# ---------------------------------------------------------------------------
# Vérification
# ---------------------------------------------------------------------------


def test_verify_valide_un_code_correct():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200, json=envelope({**PENDING, "status": "approved", "attempts": 1})
        )

    result = make_client(handler).otp.verify("483920", verification_id="ver_1")

    assert seen["url"].endswith("/otp/verify")
    assert seen["body"] == {"code": "483920", "verificationId": "ver_1"}
    assert result.approved is True


def test_verify_ne_leve_pas_sur_code_errone():
    def handler(request):
        return httpx.Response(
            200,
            json=envelope(
                {**PENDING, "status": "rejected", "reason": "invalid_code", "attemptsRemaining": 4}
            ),
        )

    result = make_client(handler).otp.verify("000000", verification_id="ver_1")

    assert result.approved is False
    assert result.status == "rejected"
    assert result.reason == "invalid_code"
    assert result.attempts_remaining == 4


def test_verify_par_destinataire():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=envelope({**PENDING, "status": "approved"}))

    make_client(handler).otp.verify("483920", to="+224620000000")
    assert seen["body"] == {"code": "483920", "to": "+224620000000"}


def test_verify_exige_un_code():
    with pytest.raises(ValueError):
        make_client(lambda r: httpx.Response(200)).otp.verify("  ", verification_id="ver_1")


def test_verify_exige_un_identifiant_ou_un_destinataire():
    with pytest.raises(ValueError):
        make_client(lambda r: httpx.Response(200)).otp.verify("483920")


# ---------------------------------------------------------------------------
# Lecture
# ---------------------------------------------------------------------------


def test_get_encode_l_identifiant():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json=envelope(PENDING))

    make_client(handler).otp.get("ver/1")
    assert seen["url"].endswith("/otp/ver%2F1")


def test_get_exige_un_identifiant():
    with pytest.raises(ValueError):
        make_client(lambda r: httpx.Response(200)).otp.get("")


# ---------------------------------------------------------------------------
# Client asynchrone
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_send_et_verify():
    seen = {}

    def handler(request):
        seen.setdefault("urls", []).append(str(request.url))
        if request.url.path.endswith("/otp/send"):
            return httpx.Response(200, json=envelope(PENDING))
        return httpx.Response(200, json=envelope({**PENDING, "status": "approved"}))

    client = make_async_client(handler)
    try:
        sent = await client.otp.send("+224620000000", channel="sms")
        assert sent.status == "pending"

        checked = await client.otp.verify("483920", verification_id=sent.verification_id)
        assert checked.approved is True
    finally:
        await client.aclose()

    assert seen["urls"][0].endswith("/otp/send")
    assert seen["urls"][1].endswith("/otp/verify")


@pytest.mark.asyncio
async def test_async_send_valide_avant_appel_reseau():
    client = make_async_client(lambda r: httpx.Response(200))
    try:
        with pytest.raises(ValueError):
            await client.otp.send("")
    finally:
        await client.aclose()
