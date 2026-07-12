"""Tests du client synchrone — httpx.MockTransport, aucun appel réseau."""

import json

import httpx
import pytest

from fameen_messaging import (
    FameenAPIError,
    FameenConnectionError,
    FameenMessaging,
    MessageList,
    MessageResource,
    RateLimitInfo,
    WalletBalance,
)

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
    "champInconnu": "ignoré",  # doit être toléré par from_dict
}


def envelope(data):
    return {"success": True, "data": data, "message": "OK"}


def make_client(handler, **kwargs):
    kwargs.setdefault("retry_base", 0.0001)  # backoff minuscule pour les tests
    return FameenMessaging(api_key=API_KEY, transport=httpx.MockTransport(handler), **kwargs)


# ---------------------------------------------------------------------------
# Enveloppe & envoi
# ---------------------------------------------------------------------------


def test_sms_send_deballe_l_enveloppe():
    seen = {}

    def handler(request):
        seen["request"] = request
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=envelope(MESSAGE_DATA))

    client = make_client(handler)
    message = client.sms.send("+224620000000", "Bonjour")

    assert isinstance(message, MessageResource)
    assert message.sid == "msg_123"
    assert message.status == "queued"
    assert message.from_ == "FAMEEN"  # clé JSON `from` → attribut `from_`
    assert message.external_id is None
    assert message.created_at == "2026-07-12T10:00:00.000Z"
    assert seen["request"].method == "POST"
    assert seen["request"].url.path == "/api/v1/sms/send"
    assert seen["body"] == {"to": "+224620000000", "message": "Bonjour"}


def test_corps_sans_enveloppe_renvoye_tel_quel_et_history_deprecie():
    raw = {"messages": [{"id": 1}], "total": 1, "page": 1, "pages": 1}

    def handler(request):
        assert request.url.path == "/api/v1/messages/history"
        return httpx.Response(200, json=raw)  # pas d'enveloppe {success, data}

    client = make_client(handler)
    with pytest.warns(DeprecationWarning):
        page = client.messages.history(page=1)
    assert page == raw


def test_create_canal_deduit_email_et_corps_camel_case():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=envelope({**MESSAGE_DATA, "channel": "email"}))

    client = make_client(handler)
    message = client.messages.create(
        "client@exemple.com",
        "Bonjour",
        subject="Info",
        status_callback="https://exemple.com/cb",
    )

    assert seen["path"] == "/api/v1/messages"
    assert seen["body"] == {
        "to": "client@exemple.com",
        "message": "Bonjour",
        "subject": "Info",
        "statusCallback": "https://exemple.com/cb",
    }
    assert message.channel == "email"


# ---------------------------------------------------------------------------
# En-têtes
# ---------------------------------------------------------------------------


def test_en_tetes_auth_idempotence_et_user_agent():
    seen = {}

    def handler(request):
        seen["headers"] = request.headers
        return httpx.Response(200, json=envelope(MESSAGE_DATA))

    client = make_client(handler)
    client.whatsapp.send("+224620000000", "Salut", idempotency_key="idem-42")

    headers = seen["headers"]
    assert headers["Authorization"] == f"Bearer {API_KEY}"
    assert headers["User-Agent"] == "fameen-messaging-python/0.1.0"
    assert headers["Idempotency-Key"] == "idem-42"
    assert headers["Accept"] == "application/json"
    assert headers["Content-Type"] == "application/json"


def test_pas_d_en_tete_idempotency_par_defaut():
    seen = {}

    def handler(request):
        seen["headers"] = request.headers
        return httpx.Response(200, json=envelope(MESSAGE_DATA))

    client = make_client(handler)
    client.sms.send("+224620000000", "Bonjour")
    assert "Idempotency-Key" not in seen["headers"]


# ---------------------------------------------------------------------------
# Query string de list()
# ---------------------------------------------------------------------------


def test_liste_query_string_et_pagination():
    seen = {}

    def handler(request):
        seen["url"] = request.url
        return httpx.Response(
            200,
            json=envelope(
                {"data": [MESSAGE_DATA], "page": 2, "limit": 50, "total": 51, "totalPages": 2}
            ),
        )

    client = make_client(handler)
    result = client.messages.list(channel="sms", status="sent", to="+224", page=2, limit=50)

    assert seen["url"].path == "/api/v1/messages"
    params = seen["url"].params
    assert params["channel"] == "sms"
    assert params["status"] == "sent"
    assert params["to"] == "+224"
    assert params["page"] == "2"
    assert params["limit"] == "50"

    assert isinstance(result, MessageList)
    assert (result.page, result.limit, result.total, result.total_pages) == (2, 50, 51, 2)
    assert len(result.data) == 1
    assert isinstance(result.data[0], MessageResource)
    assert result.data[0].sid == "msg_123"


def test_liste_omet_les_filtres_none():
    seen = {}

    def handler(request):
        seen["url"] = request.url
        return httpx.Response(
            200, json=envelope({"data": [], "page": 1, "limit": 30, "total": 0, "totalPages": 0})
        )

    client = make_client(handler)
    client.messages.list(page=1)

    params = seen["url"].params
    assert "channel" not in params
    assert "status" not in params
    assert "to" not in params
    assert "limit" not in params
    assert params["page"] == "1"


# ---------------------------------------------------------------------------
# Erreurs API
# ---------------------------------------------------------------------------


def test_erreur_402_mappee():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(
            402,
            json={
                "success": False,
                "error": {
                    "code": "insufficient_credits",
                    "message": "Crédits insuffisants — rechargez votre portefeuille.",
                },
                "statusCode": 402,
            },
        )

    client = make_client(handler)
    with pytest.raises(FameenAPIError) as excinfo:
        client.sms.send("+224620000000", "Bonjour")

    err = excinfo.value
    assert err.status == 402
    assert err.code == "insufficient_credits"
    assert "Crédits insuffisants" in str(err)
    assert err.retry_after is None
    assert calls["n"] == 1  # un 402 n'est jamais réessayé


def test_code_de_repli_selon_statut_si_corps_illisible():
    def handler(request):
        return httpx.Response(403, content=b"interdit", headers={"Content-Type": "text/plain"})

    client = make_client(handler)
    with pytest.raises(FameenAPIError) as excinfo:
        client.sms.send("+224620000000", "Bonjour")
    assert excinfo.value.status == 403
    assert excinfo.value.code == "channel_not_allowed"


# ---------------------------------------------------------------------------
# Réessais
# ---------------------------------------------------------------------------


def test_retry_429_respecte_retry_after(monkeypatch):
    sleeps = []
    monkeypatch.setattr(
        "fameen_messaging.client.time.sleep", lambda seconds: sleeps.append(seconds)
    )
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                429,
                headers={
                    "Retry-After": "1",
                    "X-RateLimit-Limit": "60",
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": "1770000000",
                },
                json={
                    "success": False,
                    "error": {"code": "rate_limited", "message": "Trop de requêtes"},
                    "statusCode": 429,
                },
            )
        return httpx.Response(200, json=envelope(MESSAGE_DATA))

    client = make_client(handler)
    # POST sans clé d'idempotence : un 429 est quand même réessayé (jamais traité).
    message = client.sms.send("+224620000000", "Bonjour")

    assert message.sid == "msg_123"
    assert calls["n"] == 2
    assert sleeps == [1]  # Retry-After (secondes) respecté, pas le backoff
    assert client.last_rate_limit == RateLimitInfo(limit=60, remaining=0, reset=1770000000)


def test_429_epuise_leve_fameen_api_error():
    def handler(request):
        return httpx.Response(
            429,
            headers={"Retry-After": "0"},
            json={
                "success": False,
                "error": {"code": "rate_limited", "message": "Trop de requêtes"},
            },
        )

    client = make_client(handler, max_retries=1)
    with pytest.raises(FameenAPIError) as excinfo:
        client.messages.get("msg_123")
    assert excinfo.value.status == 429
    assert excinfo.value.code == "rate_limited"
    assert excinfo.value.retry_after == 0


def test_post_5xx_sans_cle_non_reessaye():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(
            500,
            json={"success": False, "error": {"code": "internal_error", "message": "Erreur interne"}},
        )

    client = make_client(handler)
    with pytest.raises(FameenAPIError) as excinfo:
        client.sms.send("+224620000000", "Bonjour")

    assert calls["n"] == 1  # POST non idempotent : jamais réessayé sur 5xx
    assert excinfo.value.status == 500
    assert excinfo.value.code == "internal_error"


def test_post_5xx_avec_cle_reessaye():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                502,
                json={
                    "success": False,
                    "error": {"code": "internal_error", "message": "Passerelle en panne"},
                },
            )
        return httpx.Response(200, json=envelope(MESSAGE_DATA))

    client = make_client(handler)
    message = client.sms.send("+224620000000", "Bonjour", idempotency_key="idem-1")

    assert message.sid == "msg_123"
    assert calls["n"] == 2


def test_get_5xx_reessaye():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, content=b"")
        return httpx.Response(200, json=envelope(MESSAGE_DATA))

    client = make_client(handler)  # max_retries = 2 par défaut → 3 tentatives
    message = client.messages.get("msg_123")

    assert message.sid == "msg_123"
    assert calls["n"] == 3


def test_connection_error_apres_epuisement():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ConnectError("réseau injoignable")

    client = make_client(handler)  # max_retries = 2 → 3 tentatives
    with pytest.raises(FameenConnectionError):
        client.wallet.balance()
    assert calls["n"] == 3


# ---------------------------------------------------------------------------
# Wallet & rate limit
# ---------------------------------------------------------------------------


def test_wallet_balance_dataclass():
    def handler(request):
        assert request.url.path == "/api/v1/wallet/balance"
        return httpx.Response(
            200,
            json=envelope(
                {
                    "smsCredits": 120,
                    "waCredits": 3,
                    "emailCredits": 0,
                    "billing": {
                        "mode": "prepaid",
                        "postpaid": False,
                        "prepaidRequired": True,
                        "sendingBlocked": False,
                    },
                }
            ),
        )

    client = make_client(handler)
    balance = client.wallet.balance()

    assert isinstance(balance, WalletBalance)
    assert balance.sms_credits == 120
    assert balance.wa_credits == 3
    assert balance.email_credits == 0
    assert balance.billing.mode == "prepaid"
    assert balance.billing.postpaid is False
    assert balance.billing.prepaid_required is True
    assert balance.billing.sending_blocked is False


def test_last_rate_limit_non_ecrase_sans_en_tetes():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        headers = (
            {
                "X-RateLimit-Limit": "60",
                "X-RateLimit-Remaining": "59",
                "X-RateLimit-Reset": "1770000000",
            }
            if calls["n"] == 1
            else {}
        )
        return httpx.Response(200, headers=headers, json=envelope(MESSAGE_DATA))

    client = make_client(handler)
    assert client.last_rate_limit is None

    client.messages.get("a")
    first = client.last_rate_limit
    assert first == RateLimitInfo(limit=60, remaining=59, reset=1770000000)

    client.messages.get("b")  # réponse sans en-têtes → ne doit pas écraser
    assert client.last_rate_limit == first


# ---------------------------------------------------------------------------
# Validation locale & configuration
# ---------------------------------------------------------------------------


def test_validation_locale_sans_appel_reseau():
    def handler(request):
        raise AssertionError("aucun appel réseau ne doit avoir lieu")

    client = make_client(handler)

    with pytest.raises(TypeError):
        client.sms.send("", "Bonjour")
    with pytest.raises(TypeError):
        client.sms.send("+224620000000", "   ")
    with pytest.raises(TypeError):
        client.sms.send("client@exemple.com", "Bonjour")  # email sur canal sms
    with pytest.raises(TypeError):
        client.whatsapp.send("client@exemple.com", "Bonjour")
    with pytest.raises(TypeError):
        client.messages.create("client@exemple.com", "Bonjour", channel="whatsapp")


def test_validation_api_key_et_sid():
    with pytest.raises(TypeError):
        FameenMessaging()
    with pytest.raises(TypeError):
        FameenMessaging(api_key="   ")

    def handler(request):
        raise AssertionError("aucun appel réseau ne doit avoir lieu")

    client = make_client(handler)
    with pytest.raises(TypeError):
        client.messages.get("  ")


def test_base_url_slashs_finaux_retires():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json=envelope({"smsCredits": 0}))

    client = FameenMessaging(
        api_key=API_KEY,
        base_url="https://exemple.test/api/v1///",
        transport=httpx.MockTransport(handler),
        retry_base=0.0001,
    )
    client.wallet.balance()
    assert seen["url"] == "https://exemple.test/api/v1/wallet/balance"
