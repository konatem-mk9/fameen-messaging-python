"""Vérification et parsing des webhooks Fameen Messaging.

L'API signe chaque callback de statut avec HMAC-SHA256 (encodage **hex**)
du **corps brut** de la requête, avec le secret ``whsec_…`` du compte.
La signature est transmise dans l'en-tête ``X-Fameen-Signature``
(en-tête informatif : ``X-Fameen-Event``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Optional, Union

from .errors import WebhookVerificationError
from .types import WebhookEvent

RawPayload = Union[str, bytes, bytearray, memoryview]


def _to_bytes(payload: RawPayload) -> bytes:
    """Normalise le corps brut en ``bytes`` (str → UTF-8)."""
    if isinstance(payload, str):
        return payload.encode("utf-8")
    if isinstance(payload, (bytes, bytearray, memoryview)):
        return bytes(payload)
    raise TypeError("`payload` doit être le corps brut du webhook (bytes ou str).")


def verify_webhook_signature(
    payload: RawPayload,
    signature: Optional[str],
    secret: str,
) -> bool:
    """Vérifie la signature HMAC-SHA256 d'un webhook Fameen (``X-Fameen-Signature``).

    ⚠️ ``payload`` doit être le **corps brut** de la requête, avant tout
    parsing JSON (un re-``json.dumps`` ne produit pas forcément les mêmes
    octets). La comparaison est faite en **temps constant**
    (``hmac.compare_digest``).

    Args:
        payload: corps brut reçu (``bytes`` ou ``str``).
        signature: valeur de l'en-tête ``X-Fameen-Signature`` (hex), ou ``None``.
        secret: secret ``whsec_…`` du compte.

    Returns:
        ``True`` si la signature correspond, ``False`` sinon (y compris
        signature absente).

    Raises:
        TypeError: si ``secret`` est vide ou n'est pas une chaîne.
    """
    if not isinstance(secret, str) or not secret:
        raise TypeError('`secret` est requis (secret "whsec_…" du compte).')
    if not isinstance(signature, str) or not signature:
        return False

    expected = hmac.new(secret.encode("utf-8"), _to_bytes(payload), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode("ascii"), signature.strip().encode("utf-8"))


def construct_webhook_event(
    payload: RawPayload,
    signature: Optional[str],
    secret: str,
) -> WebhookEvent:
    """Vérifie la signature PUIS parse l'événement — à appeler dans votre handler.

    Lève :class:`~fameen_messaging.errors.WebhookVerificationError` si la
    signature ou le corps est invalide : répondez alors 401 et ne traitez rien.

    Exemple (Flask)::

        @app.post("/webhooks/fameen")
        def fameen_webhook():
            event = construct_webhook_event(
                request.get_data(),                       # corps brut
                request.headers.get("X-Fameen-Signature"),
                os.environ["FAMEEN_WEBHOOK_SECRET"],
            )
            # event.sid / event.status / event.event
            return "", 200

    Returns:
        L'événement parsé sous forme de :class:`~fameen_messaging.types.WebhookEvent`.
    """
    if not verify_webhook_signature(payload, signature, secret):
        raise WebhookVerificationError(
            "Signature X-Fameen-Signature invalide — événement rejeté."
        )
    try:
        parsed: Any = json.loads(_to_bytes(payload).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise WebhookVerificationError("Corps de webhook illisible (JSON invalide).") from exc
    if not isinstance(parsed, dict):
        raise WebhookVerificationError("Corps de webhook illisible (objet JSON attendu).")
    return WebhookEvent.from_dict(parsed)
