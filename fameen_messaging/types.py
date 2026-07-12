"""Modèles de données de l'API Fameen Messaging.

Dataclasses **gelées** construites via ``from_dict`` tolérant :

- les champs inconnus du JSON sont ignorés ;
- les champs manquants valent ``None`` (ou ``0`` pour les compteurs) ;
- les clés camelCase du JSON sont exposées en snake_case côté Python
  (``externalId`` → ``external_id``) ; ``from`` (mot réservé) → ``from_``.

Les dates sont des chaînes ISO 8601, telles que renvoyées par l'API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Mapping, Optional, Union

Number = Union[int, float]


def _mapping(data: Any) -> Mapping[str, Any]:
    """Renvoie ``data`` si c'est un mapping, sinon un mapping vide (tolérance)."""
    return data if isinstance(data, Mapping) else {}


def _num(value: Any, default: Number = 0) -> Number:
    """Coercition numérique tolérante (booléens exclus, chaînes acceptées)."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            try:
                return float(value)
            except ValueError:
                return default
    return default


def _int(value: Any, default: int = 0) -> int:
    """Coercition entière tolérante (``None``/illisible → ``default``)."""
    try:
        return int(_num(value, default))
    except (OverflowError, ValueError):
        return default


@dataclass(frozen=True)
class RateLimitInfo:
    """Compteurs de limitation de débit (en-têtes ``X-RateLimit-*``).

    Attributes:
        limit: plafond de requêtes par fenêtre (``X-RateLimit-Limit``).
        remaining: requêtes restantes dans la fenêtre (``X-RateLimit-Remaining``).
        reset: fin de fenêtre, epoch en secondes (``X-RateLimit-Reset``).
    """

    limit: int = 0
    remaining: int = 0
    reset: int = 0

    @classmethod
    def from_dict(cls, data: Any) -> "RateLimitInfo":
        """Construit l'objet depuis un dict JSON (tolérant)."""
        d = _mapping(data)
        return cls(
            limit=_int(d.get("limit")),
            remaining=_int(d.get("remaining")),
            reset=_int(d.get("reset")),
        )


@dataclass(frozen=True)
class MessageResource:
    """Ressource Message telle que renvoyée par l'API (``data`` de l'enveloppe).

    ``status`` ∈ ``queued | sending | sent | delivered | failed``.
    ``from_`` correspond à la clé JSON ``from`` (mot réservé en Python) :
    expéditeur effectif (sender name SMS, numéro WhatsApp, adresse email).
    """

    sid: Optional[str] = None
    status: Optional[str] = None
    channel: Optional[str] = None
    to: Optional[str] = None
    from_: Optional[str] = None
    body: Optional[str] = None
    #: Tranches de 160 caractères (SMS) ; 1 pour WhatsApp/email.
    segments: Number = 0
    credits: Number = 0
    error: Optional[str] = None
    #: Identifiant du message chez l'opérateur, une fois envoyé.
    external_id: Optional[str] = None
    status_callback: Optional[str] = None
    created_at: Optional[str] = None
    sent_at: Optional[str] = None
    delivered_at: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Any) -> "MessageResource":
        """Construit l'objet depuis un dict JSON (tolérant)."""
        d = _mapping(data)
        return cls(
            sid=d.get("sid"),
            status=d.get("status"),
            channel=d.get("channel"),
            to=d.get("to"),
            from_=d.get("from"),
            body=d.get("body"),
            segments=_num(d.get("segments")),
            credits=_num(d.get("credits")),
            error=d.get("error"),
            external_id=d.get("externalId"),
            status_callback=d.get("statusCallback"),
            created_at=d.get("createdAt"),
            sent_at=d.get("sentAt"),
            delivered_at=d.get("deliveredAt"),
        )


@dataclass(frozen=True)
class MessageList:
    """Page renvoyée par ``GET /v1/messages``."""

    data: List[MessageResource] = field(default_factory=list)
    page: int = 0
    #: Taille de page effective (max 100).
    limit: int = 0
    total: int = 0
    total_pages: int = 0

    @classmethod
    def from_dict(cls, data: Any) -> "MessageList":
        """Construit la page depuis un dict JSON (tolérant)."""
        d = _mapping(data)
        items = d.get("data")
        messages = (
            [MessageResource.from_dict(item) for item in items]
            if isinstance(items, (list, tuple))
            else []
        )
        return cls(
            data=messages,
            page=_int(d.get("page")),
            limit=_int(d.get("limit")),
            total=_int(d.get("total")),
            total_pages=_int(d.get("totalPages")),
        )


@dataclass(frozen=True)
class WalletBilling:
    """Mode de facturation du compte.

    Attributes:
        mode: ``prepaid`` ou ``consumption``.
        postpaid: ``True`` = facturation à la consommation (envoi non limité par le solde).
        prepaid_required: ``True`` = un rechargement préalable est exigé.
        sending_blocked: ``True`` = compte bloqué (période de consommation expirée).
    """

    mode: Optional[str] = None
    postpaid: bool = False
    prepaid_required: bool = False
    sending_blocked: bool = False

    @classmethod
    def from_dict(cls, data: Any) -> "WalletBilling":
        """Construit l'objet depuis un dict JSON (tolérant)."""
        d = _mapping(data)
        return cls(
            mode=d.get("mode"),
            postpaid=bool(d.get("postpaid", False)),
            prepaid_required=bool(d.get("prepaidRequired", False)),
            sending_blocked=bool(d.get("sendingBlocked", False)),
        )


@dataclass(frozen=True)
class WalletBalance:
    """Soldes et mode de facturation (``GET /v1/wallet/balance``)."""

    sms_credits: Number = 0
    wa_credits: Number = 0
    email_credits: Number = 0
    billing: WalletBilling = field(default_factory=WalletBilling)

    @classmethod
    def from_dict(cls, data: Any) -> "WalletBalance":
        """Construit l'objet depuis un dict JSON (tolérant)."""
        d = _mapping(data)
        return cls(
            sms_credits=_num(d.get("smsCredits")),
            wa_credits=_num(d.get("waCredits")),
            email_credits=_num(d.get("emailCredits")),
            billing=WalletBilling.from_dict(d.get("billing")),
        )


@dataclass(frozen=True)
class WebhookEvent:
    """Corps JSON reçu sur votre webhook de statut.

    ``event`` ∈ ``queued | sent | delivered | failed`` (aussi en-tête
    ``X-Fameen-Event``). ``timestamp`` est la date ISO 8601 d'émission
    du callback. ``from_`` correspond à la clé JSON ``from``.
    """

    event: Optional[str] = None
    sid: Optional[str] = None
    status: Optional[str] = None
    channel: Optional[str] = None
    to: Optional[str] = None
    from_: Optional[str] = None
    error: Optional[str] = None
    external_id: Optional[str] = None
    timestamp: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Any) -> "WebhookEvent":
        """Construit l'événement depuis un dict JSON (tolérant)."""
        d = _mapping(data)
        return cls(
            event=d.get("event"),
            sid=d.get("sid"),
            status=d.get("status"),
            channel=d.get("channel"),
            to=d.get("to"),
            from_=d.get("from"),
            error=d.get("error"),
            external_id=d.get("externalId"),
            timestamp=d.get("timestamp"),
        )
