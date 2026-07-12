"""Erreurs typées du SDK Fameen Messaging.

Hiérarchie :

- :class:`FameenError` — classe mère de toutes les erreurs du SDK ;
- :class:`FameenAPIError` — réponse HTTP non-2xx de l'API ;
- :class:`FameenConnectionError` — échec réseau (API injoignable) ;
- :class:`WebhookVerificationError` — signature ou corps de webhook invalide.

Codes stables ``error.code`` renvoyés par l'API : ``bad_request``,
``unauthorized``, ``insufficient_credits``, ``channel_not_allowed``,
``not_found``, ``rate_limited``, ``internal_error``, ``unknown_error``.
"""

from __future__ import annotations

from typing import Optional, Union

from .types import RateLimitInfo

Number = Union[int, float]


class FameenError(Exception):
    """Classe mère de toutes les erreurs du SDK."""


class FameenAPIError(FameenError):
    """Erreur renvoyée par l'API (réponse HTTP non-2xx).

    Attributes:
        status: statut HTTP (401, 402, 403, 404, 429, 500…).
        code: code stable ``error.code`` du corps (``unauthorized``,
            ``insufficient_credits``, ``channel_not_allowed``,
            ``rate_limited``, ``not_found``…) ou repli déduit du statut
            si le corps est illisible.
        retry_after: secondes à attendre avant de réessayer
            (en-tête ``Retry-After``), si fourni — surtout sur les 429.
        rate_limit: compteurs ``X-RateLimit-*`` connus au moment de l'erreur,
            ou ``None``.
    """

    def __init__(
        self,
        message: str,
        status: int = 0,
        code: str = "unknown_error",
        retry_after: Optional[Number] = None,
        rate_limit: Optional[RateLimitInfo] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.retry_after = retry_after
        self.rate_limit = rate_limit

    def __repr__(self) -> str:  # pragma: no cover - confort de debug
        return (
            f"{type(self).__name__}(status={self.status!r}, code={self.code!r}, "
            f"message={str(self)!r}, retry_after={self.retry_after!r})"
        )


class FameenConnectionError(FameenError):
    """Échec réseau : l'API n'a pas pu être jointe (DNS, timeout, coupure…)."""


class WebhookVerificationError(FameenError):
    """Signature ou corps de webhook invalide — ne traitez pas l'événement."""
