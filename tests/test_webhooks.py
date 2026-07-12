"""Tests des helpers webhooks (HMAC-SHA256 hex du corps brut)."""

import hashlib
import hmac
import json

import pytest

from fameen_messaging import (
    WebhookEvent,
    WebhookVerificationError,
    construct_webhook_event,
    verify_webhook_signature,
)

SECRET = "whsec_secret_de_test"

EVENT = {
    "event": "delivered",
    "sid": "msg_123",
    "status": "delivered",
    "channel": "sms",
    "to": "+224620000000",
    "from": "FAMEEN",
    "error": None,
    "externalId": "ext-9",
    "timestamp": "2026-07-12T10:00:00.000Z",
}
PAYLOAD = json.dumps(EVENT).encode("utf-8")


def sign(payload, secret=SECRET):
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def test_signature_valide_et_evenement_construit():
    signature = sign(PAYLOAD)

    assert verify_webhook_signature(PAYLOAD, signature, SECRET) is True

    event = construct_webhook_event(PAYLOAD, signature, SECRET)
    assert isinstance(event, WebhookEvent)
    assert event.event == "delivered"
    assert event.sid == "msg_123"
    assert event.status == "delivered"
    assert event.to == "+224620000000"
    assert event.from_ == "FAMEEN"  # clé JSON `from` → attribut `from_`
    assert event.external_id == "ext-9"
    assert event.timestamp == "2026-07-12T10:00:00.000Z"


def test_payload_str_equivalent_aux_bytes():
    payload_str = PAYLOAD.decode("utf-8")
    signature = sign(PAYLOAD)
    assert verify_webhook_signature(payload_str, signature, SECRET) is True
    event = construct_webhook_event(payload_str, signature, SECRET)
    assert event.sid == "msg_123"


def test_payload_altere_rejete():
    signature = sign(PAYLOAD)
    tampered = PAYLOAD.replace(b"msg_123", b"msg_999")

    assert verify_webhook_signature(tampered, signature, SECRET) is False
    with pytest.raises(WebhookVerificationError):
        construct_webhook_event(tampered, signature, SECRET)


def test_mauvais_secret_rejete():
    signature = sign(PAYLOAD, secret="whsec_autre_secret")
    assert verify_webhook_signature(PAYLOAD, signature, SECRET) is False
    with pytest.raises(WebhookVerificationError):
        construct_webhook_event(PAYLOAD, signature, SECRET)


def test_signature_absente_refusee():
    assert verify_webhook_signature(PAYLOAD, None, SECRET) is False
    assert verify_webhook_signature(PAYLOAD, "", SECRET) is False
    with pytest.raises(WebhookVerificationError):
        construct_webhook_event(PAYLOAD, None, SECRET)


def test_json_invalide_signe_correctement_rejete():
    payload = b"{ceci n'est pas du JSON"
    signature = sign(payload)

    # La signature elle-même est valide…
    assert verify_webhook_signature(payload, signature, SECRET) is True
    # …mais le corps est illisible → WebhookVerificationError.
    with pytest.raises(WebhookVerificationError):
        construct_webhook_event(payload, signature, SECRET)


def test_secret_vide_erreur_de_type():
    with pytest.raises(TypeError):
        verify_webhook_signature(PAYLOAD, sign(PAYLOAD), "")
    with pytest.raises(TypeError):
        construct_webhook_event(PAYLOAD, sign(PAYLOAD), "")
