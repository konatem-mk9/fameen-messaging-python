"""Clients HTTP (synchrone et asynchrone) de l'API Fameen Messaging.

Sémantique des réessais (identique au SDK Node de référence) :

- **erreur réseau** → réessai (toutes méthodes), backoff exponentiel + jitter ;
- **HTTP 429** → réessai en respectant l'en-tête ``Retry-After`` (secondes)
  si présent, sinon backoff ;
- **HTTP 5xx** → réessai UNIQUEMENT si la requête est un GET **ou** si une
  ``idempotency_key`` est fournie (un POST non idempotent a pu être traité
  côté serveur → jamais réessayé) ;
- réseau épuisé → :class:`~fameen_messaging.errors.FameenConnectionError` ;
- sinon → :class:`~fameen_messaging.errors.FameenAPIError`.
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import time
import warnings
from typing import Any, Dict, Mapping, Optional, Tuple, Union
from urllib.parse import quote

from typing import List

import httpx

from .errors import FameenAPIError, FameenConnectionError
from .media import MediaContent, build_media_fields, has_media
from .types import MessageList, MessageResource, RateLimitInfo, WalletBalance

VERSION = "0.2.0"
DEFAULT_BASE_URL = "https://business.fameengroupe.com/api/v1"
USER_AGENT = f"fameen-messaging-python/{VERSION}"

Number = Union[int, float]

_CODE_BY_STATUS = {
    400: "bad_request",
    401: "unauthorized",
    402: "insufficient_credits",
    403: "channel_not_allowed",
    404: "not_found",
    429: "rate_limited",
}

_HISTORY_DEPRECATION = (
    "messages.history() est déprécié (lignes brutes, non garanties stables) — "
    "utilisez messages.list()."
)


# ---------------------------------------------------------------------------
# Aides internes partagées (sync + async)
# ---------------------------------------------------------------------------


def _code_from_status(status: int) -> str:
    """Code d'erreur de repli quand le corps de la réponse est illisible."""
    code = _CODE_BY_STATUS.get(status)
    if code:
        return code
    return "internal_error" if status >= 500 else "unknown_error"


def _assert_sendable(
    to: Any,
    message: Any,
    channel: Optional[str] = None,
    *,
    with_media: bool = False,
) -> None:
    """Valide les champs minimum AVANT tout appel réseau (meilleure DX)."""
    if not isinstance(to, str) or not to.strip():
        raise TypeError("`to` est requis (numéro E.164 ou adresse email).")
    # Un message peut n'être qu'un média (légende facultative).
    if not with_media and (not isinstance(message, str) or not message.strip()):
        raise TypeError("`message` est requis (ou fournissez un média).")
    if with_media and channel == "sms":
        raise TypeError("Le canal SMS ne supporte pas les pièces jointes.")
    if channel and channel != "email" and isinstance(to, str) and "@" in to:
        raise TypeError(
            f'`to` ressemble à un email mais le canal demandé est "{channel}".'
        )


def _clean_sid(sid: Any) -> str:
    """Valide et normalise un identifiant de message."""
    if not isinstance(sid, str) or not sid.strip():
        raise TypeError("`sid` est requis.")
    return sid.strip()


def _send_body(
    to: str,
    message: Any,
    *,
    channel: Optional[str] = None,
    subject: Optional[str] = None,
    status_callback: Optional[str] = None,
    media: Optional[MediaContent] = None,
    file_name: Optional[str] = None,
    content_type: Optional[str] = None,
    media_type: Optional[str] = None,
    attachments: Optional[List[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Construit le corps JSON d'envoi (clés camelCase de l'API, sans ``None``)."""
    body: Dict[str, Any] = {"to": to, "message": message if isinstance(message, str) else ""}
    if channel is not None:
        body["channel"] = channel
    if subject is not None:
        body["subject"] = subject
    if status_callback is not None:
        body["statusCallback"] = status_callback
    body.update(
        build_media_fields(
            media=media,
            file_name=file_name,
            content_type=content_type,
            media_type=media_type,
            attachments=attachments,
        )
    )
    return body


def _clean_query(query: Optional[Mapping[str, Any]]) -> Optional[Dict[str, str]]:
    """Retire les paramètres ``None``/vides et convertit le reste en chaînes."""
    if not query:
        return None
    params: Dict[str, str] = {}
    for key, value in query.items():
        if value is None or value == "":
            continue
        params[key] = str(value)
    return params or None


def _parse_json(response: httpx.Response) -> Any:
    """Parse le corps en JSON, ``None`` si vide ou illisible."""
    raw = response.text
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _read_rate_limit(response: httpx.Response) -> Optional[RateLimitInfo]:
    """Lit les en-têtes ``X-RateLimit-*`` (les 3 requis, ``limit`` > 0)."""
    try:
        limit = int(response.headers["X-RateLimit-Limit"])
        remaining = int(response.headers["X-RateLimit-Remaining"])
        reset = int(response.headers["X-RateLimit-Reset"])
    except (KeyError, TypeError, ValueError):
        return None
    if limit > 0:
        return RateLimitInfo(limit=limit, remaining=remaining, reset=reset)
    return None


def _read_retry_after(response: httpx.Response) -> Optional[Number]:
    """Lit ``Retry-After`` en secondes (``None`` si absent ou illisible)."""
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    if math.isnan(seconds) or math.isinf(seconds) or seconds < 0:
        return None
    return int(seconds) if seconds.is_integer() else seconds


def _error_parts(parsed: Any, status: int, method: str, path: str) -> Tuple[str, str]:
    """Extrait ``(code, message)`` de l'enveloppe d'erreur, avec replis."""
    code: Optional[str] = None
    message: Optional[str] = None
    if isinstance(parsed, Mapping):
        err = parsed.get("error")
        if isinstance(err, Mapping):
            c = err.get("code")
            if isinstance(c, str) and c:
                code = c
            m = err.get("message")
            if isinstance(m, str) and m:
                message = m
        if message is None:
            m = parsed.get("message")
            if isinstance(m, str) and m:
                message = m
    if code is None:
        code = _code_from_status(status)
    if message is None:
        message = f"Erreur HTTP {status} sur {method} {path}"
    return code, message


# ---------------------------------------------------------------------------
# Socle commun sync/async
# ---------------------------------------------------------------------------


class _BaseClient:
    """Configuration et logique de réessai partagées par les deux clients."""

    #: Compteurs ``X-RateLimit-*`` de la dernière réponse qui les fournissait.
    last_rate_limit: Optional[RateLimitInfo]

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        timeout: Any = 30.0,
        max_retries: int = 2,
        retry_base: float = 0.5,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise TypeError('FameenMessaging: `api_key` est requis (clé "fam_…").')
        if base_url is None:
            base_url = DEFAULT_BASE_URL
        if not isinstance(base_url, str) or not base_url.strip():
            raise TypeError("`base_url` doit être une URL non vide.")

        self._api_key = api_key.strip()
        self._base_url = base_url.strip().rstrip("/")
        self._timeout = timeout
        self._max_retries = max(0, int(max_retries))
        self._retry_base = max(0.0, float(retry_base))
        self.last_rate_limit = None

    # -- construction de requête ------------------------------------------------

    def _url(self, path: str) -> str:
        return self._base_url + path

    def _headers(self, has_body: bool, idempotency_key: Optional[str]) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if has_body:
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    def _backoff(self, attempt: int) -> float:
        """Backoff exponentiel : ``retry_base × 2^tentative + jitter(0..retry_base)``."""
        return self._retry_base * (2 ** attempt) + random.random() * self._retry_base

    def _connection_error(self, url: str, exc: Exception) -> FameenConnectionError:
        host = httpx.URL(url).host or url
        return FameenConnectionError(
            f"Impossible de joindre l'API Fameen ({host}) : {exc}"
        )

    # -- traitement de réponse ----------------------------------------------------

    def _process_response(
        self,
        response: httpx.Response,
        *,
        method: str,
        path: str,
        idempotency_key: Optional[str],
        attempt: int,
    ) -> Tuple[str, Any]:
        """Analyse une réponse HTTP.

        Returns:
            ``("ok", data)`` — succès, ``data`` déjà déballé de l'enveloppe ;
            ``("retry", délai_en_secondes)`` — réessai autorisé.

        Raises:
            FameenAPIError: erreur définitive (non réessayable ou réessais épuisés).
        """
        rate_limit = _read_rate_limit(response)
        if rate_limit is not None:
            self.last_rate_limit = rate_limit

        parsed = _parse_json(response)

        if response.is_success:
            # Enveloppe standard { success, data } → on renvoie `data` directement.
            if isinstance(parsed, Mapping) and "success" in parsed and "data" in parsed:
                return ("ok", parsed["data"])
            return ("ok", parsed)

        status = response.status_code
        code, message = _error_parts(parsed, status, method, path)
        retry_after = _read_retry_after(response)

        retriable = status == 429 or status >= 500
        # POST non idempotent : un 5xx a pu être traité côté serveur → pas de réessai.
        safe_to_retry = method == "GET" or bool(idempotency_key) or status == 429

        if retriable and safe_to_retry and attempt < self._max_retries:
            delay = float(retry_after) if retry_after is not None else self._backoff(attempt)
            return ("retry", delay)

        raise FameenAPIError(
            message,
            status=status,
            code=code,
            retry_after=retry_after,
            rate_limit=self.last_rate_limit,
        )


# ---------------------------------------------------------------------------
# Client synchrone
# ---------------------------------------------------------------------------


class FameenMessaging(_BaseClient):
    """Client synchrone de l'API Fameen Messaging.

    Exemple::

        from fameen_messaging import FameenMessaging

        client = FameenMessaging(api_key="fam_…")
        message = client.sms.send("+224620000000", "Bonjour !")
        print(message.sid, message.status)

    Args:
        api_key: clé API du compte (``fam_…``) — requise, jamais côté navigateur.
        base_url: défaut ``https://business.fameengroupe.com/api/v1``
            (les ``/`` finaux sont retirés).
        timeout: timeout httpx par tentative, en secondes (défaut : 30.0).
        max_retries: nombre de réessais automatiques (défaut : 2).
        retry_base: base du backoff exponentiel en secondes (défaut : 0.5) —
            surtout utile en test.
        transport: transport httpx injectable (``httpx.MockTransport`` en test,
            proxy…). Défaut : transport httpx standard.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        timeout: Any = 30.0,
        max_retries: int = 2,
        retry_base: float = 0.5,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        super().__init__(
            api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            retry_base=retry_base,
        )
        self._http = httpx.Client(timeout=timeout, transport=transport, follow_redirects=True)

        #: Ressource « Messages » unifiée (création, suivi, liste).
        self.messages = MessagesResource(self)
        #: Envoi SMS (``POST /sms/send``).
        self.sms = SmsResource(self)
        #: Envoi WhatsApp (``POST /whatsapp/send``).
        self.whatsapp = WhatsappResource(self)
        #: Envoi email (``POST /email/send``).
        self.email = EmailResource(self)
        #: Soldes et facturation (``GET /wallet/balance``).
        self.wallet = WalletResource(self)

    def close(self) -> None:
        """Ferme le client HTTP sous-jacent."""
        self._http.close()

    def __enter__(self) -> "FameenMessaging":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: Optional[Mapping[str, Any]] = None,
        body: Optional[Mapping[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Any:
        """Exécute une requête avec réessais ; renvoie ``data`` déballé."""
        url = self._url(path)
        params = _clean_query(query)
        headers = self._headers(body is not None, idempotency_key)

        for attempt in range(self._max_retries + 1):
            try:
                response = self._http.request(
                    method, url, params=params, headers=headers, json=body
                )
            except httpx.TransportError as exc:
                # Échec réseau : la requête n'a (très probablement) pas été traitée.
                if attempt < self._max_retries:
                    time.sleep(self._backoff(attempt))
                    continue
                raise self._connection_error(url, exc) from exc

            outcome, value = self._process_response(
                response,
                method=method,
                path=path,
                idempotency_key=idempotency_key,
                attempt=attempt,
            )
            if outcome == "ok":
                return value
            time.sleep(value)

        # Inatteignable (la boucle renvoie ou lève toujours) — parité Node.
        raise FameenConnectionError("Réessais épuisés.")  # pragma: no cover


# ---------------------------------------------------------------------------
# Ressources synchrones
# ---------------------------------------------------------------------------


class MessagesResource:
    """Ressource « Messages » unifiée (façon Twilio)."""

    def __init__(self, client: FameenMessaging) -> None:
        self._client = client

    def create(
        self,
        to: str,
        message: str = "",
        channel: Optional[str] = None,
        subject: Optional[str] = None,
        status_callback: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        *,
        media: Optional[MediaContent] = None,
        file_name: Optional[str] = None,
        content_type: Optional[str] = None,
        media_type: Optional[str] = None,
        attachments: Optional[List[Mapping[str, Any]]] = None,
    ) -> MessageResource:
        """Envoie un message — canal explicite ou déduit du destinataire.

        Sans ``channel`` : email si ``to`` contient « @ », sinon SMS
        (WhatsApp doit donc toujours être explicite). Voir :meth:`SmsResource.send`
        pour la liste complète des paramètres média.
        """
        _assert_sendable(to, message, channel, with_media=has_media(media, attachments))
        body = _send_body(
            to, message, channel=channel, subject=subject, status_callback=status_callback,
            media=media, file_name=file_name, content_type=content_type,
            media_type=media_type, attachments=attachments,
        )
        data = self._client._request(
            "POST", "/messages", body=body, idempotency_key=idempotency_key
        )
        return MessageResource.from_dict(data)

    def get(self, sid: str) -> MessageResource:
        """Statut courant d'un message."""
        cleaned = _clean_sid(sid)
        data = self._client._request("GET", f"/messages/{quote(cleaned, safe='')}")
        return MessageResource.from_dict(data)

    def list(
        self,
        channel: Optional[str] = None,
        status: Optional[str] = None,
        to: Optional[str] = None,
        page: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> MessageList:
        """Liste paginée (filtres canal / statut / destinataire « contient »)."""
        data = self._client._request(
            "GET",
            "/messages",
            query={"channel": channel, "status": status, "to": to, "page": page, "limit": limit},
        )
        return MessageList.from_dict(data)

    def history(
        self,
        channel: Optional[str] = None,
        status: Optional[str] = None,
        page: Optional[int] = None,
    ) -> Any:
        """(Déprécié) Endpoint historique aux lignes brutes — préférez :meth:`list`."""
        warnings.warn(_HISTORY_DEPRECATION, DeprecationWarning, stacklevel=2)
        return self._client._request(
            "GET", "/messages/history", query={"channel": channel, "status": status, "page": page}
        )


class _ChannelResource:
    """Socle des ressources par canal (sms / whatsapp / email)."""

    _path = ""
    _channel = ""

    def __init__(self, client: FameenMessaging) -> None:
        self._client = client

    def _send(
        self,
        to: str,
        message: Any,
        subject: Optional[str] = None,
        status_callback: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        *,
        media: Optional[MediaContent] = None,
        file_name: Optional[str] = None,
        content_type: Optional[str] = None,
        media_type: Optional[str] = None,
        attachments: Optional[List[Mapping[str, Any]]] = None,
    ) -> MessageResource:
        _assert_sendable(to, message, self._channel, with_media=has_media(media, attachments))
        body = _send_body(
            to, message, subject=subject, status_callback=status_callback,
            media=media, file_name=file_name, content_type=content_type,
            media_type=media_type, attachments=attachments,
        )
        data = self._client._request(
            "POST", self._path, body=body, idempotency_key=idempotency_key
        )
        return MessageResource.from_dict(data)


class SmsResource(_ChannelResource):
    """Envoi SMS (nécessite le scope ``sms``)."""

    _path = "/sms/send"
    _channel = "sms"

    def send(
        self,
        to: str,
        message: str,
        status_callback: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> MessageResource:
        """Envoie un SMS à ``to`` (numéro E.164, ex. ``+224620000000``)."""
        return self._send(
            to, message, status_callback=status_callback, idempotency_key=idempotency_key
        )


class WhatsappResource(_ChannelResource):
    """Envoi WhatsApp (nécessite le scope ``whatsapp``)."""

    _path = "/whatsapp/send"
    _channel = "whatsapp"

    def send(
        self,
        to: str,
        message: str = "",
        status_callback: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        *,
        media: Optional[MediaContent] = None,
        file_name: Optional[str] = None,
        content_type: Optional[str] = None,
        media_type: Optional[str] = None,
        attachments: Optional[List[Mapping[str, Any]]] = None,
    ) -> MessageResource:
        """Envoie un message WhatsApp à ``to`` (numéro E.164).

        Média (PDF, image, vidéo, audio) : passez ``media`` (octets ou base64)
        avec ``file_name`` — ou ``attachments=[...]`` (un seul média par message
        sur WhatsApp). ``message`` sert alors de légende (facultatif).
        """
        return self._send(
            to, message, status_callback=status_callback, idempotency_key=idempotency_key,
            media=media, file_name=file_name, content_type=content_type,
            media_type=media_type, attachments=attachments,
        )


class EmailResource(_ChannelResource):
    """Envoi email (nécessite le scope ``email``)."""

    _path = "/email/send"
    _channel = "email"

    def send(
        self,
        to: str,
        message: str = "",
        subject: Optional[str] = None,
        status_callback: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        *,
        media: Optional[MediaContent] = None,
        file_name: Optional[str] = None,
        content_type: Optional[str] = None,
        media_type: Optional[str] = None,
        attachments: Optional[List[Mapping[str, Any]]] = None,
    ) -> MessageResource:
        """Envoie un email à ``to`` (``subject`` ≤ 255 caractères).

        Pièces jointes : ``attachments=[file_attachment("f.pdf"), ...]`` (plusieurs
        autorisées) ou le raccourci ``media`` + ``file_name`` pour un seul fichier.
        """
        return self._send(
            to,
            message,
            subject=subject,
            status_callback=status_callback,
            idempotency_key=idempotency_key,
            media=media,
            file_name=file_name,
            content_type=content_type,
            media_type=media_type,
            attachments=attachments,
        )


class WalletResource:
    """Portefeuille du compte."""

    def __init__(self, client: FameenMessaging) -> None:
        self._client = client

    def balance(self) -> WalletBalance:
        """Soldes SMS / WhatsApp / Email et mode de facturation."""
        data = self._client._request("GET", "/wallet/balance")
        return WalletBalance.from_dict(data)


# ---------------------------------------------------------------------------
# Client asynchrone
# ---------------------------------------------------------------------------


class AsyncFameenMessaging(_BaseClient):
    """Client **asynchrone** de l'API Fameen Messaging (``httpx.AsyncClient``).

    Mêmes méthodes que :class:`FameenMessaging`, en ``async``. Exemple::

        from fameen_messaging import AsyncFameenMessaging

        async with AsyncFameenMessaging(api_key="fam_…") as client:
            message = await client.sms.send("+224620000000", "Bonjour !")

    Voir :class:`FameenMessaging` pour la description des options.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        timeout: Any = 30.0,
        max_retries: int = 2,
        retry_base: float = 0.5,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        super().__init__(
            api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            retry_base=retry_base,
        )
        self._http = httpx.AsyncClient(
            timeout=timeout, transport=transport, follow_redirects=True
        )

        #: Ressource « Messages » unifiée (création, suivi, liste).
        self.messages = AsyncMessagesResource(self)
        #: Envoi SMS (``POST /sms/send``).
        self.sms = AsyncSmsResource(self)
        #: Envoi WhatsApp (``POST /whatsapp/send``).
        self.whatsapp = AsyncWhatsappResource(self)
        #: Envoi email (``POST /email/send``).
        self.email = AsyncEmailResource(self)
        #: Soldes et facturation (``GET /wallet/balance``).
        self.wallet = AsyncWalletResource(self)

    async def aclose(self) -> None:
        """Ferme le client HTTP sous-jacent."""
        await self._http.aclose()

    async def __aenter__(self) -> "AsyncFameenMessaging":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        query: Optional[Mapping[str, Any]] = None,
        body: Optional[Mapping[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Any:
        """Exécute une requête avec réessais ; renvoie ``data`` déballé."""
        url = self._url(path)
        params = _clean_query(query)
        headers = self._headers(body is not None, idempotency_key)

        for attempt in range(self._max_retries + 1):
            try:
                response = await self._http.request(
                    method, url, params=params, headers=headers, json=body
                )
            except httpx.TransportError as exc:
                # Échec réseau : la requête n'a (très probablement) pas été traitée.
                if attempt < self._max_retries:
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                raise self._connection_error(url, exc) from exc

            outcome, value = self._process_response(
                response,
                method=method,
                path=path,
                idempotency_key=idempotency_key,
                attempt=attempt,
            )
            if outcome == "ok":
                return value
            await asyncio.sleep(value)

        # Inatteignable (la boucle renvoie ou lève toujours) — parité Node.
        raise FameenConnectionError("Réessais épuisés.")  # pragma: no cover


# ---------------------------------------------------------------------------
# Ressources asynchrones
# ---------------------------------------------------------------------------


class AsyncMessagesResource:
    """Ressource « Messages » unifiée (version asynchrone)."""

    def __init__(self, client: AsyncFameenMessaging) -> None:
        self._client = client

    async def create(
        self,
        to: str,
        message: str = "",
        channel: Optional[str] = None,
        subject: Optional[str] = None,
        status_callback: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        *,
        media: Optional[MediaContent] = None,
        file_name: Optional[str] = None,
        content_type: Optional[str] = None,
        media_type: Optional[str] = None,
        attachments: Optional[List[Mapping[str, Any]]] = None,
    ) -> MessageResource:
        """Envoie un message — canal explicite ou déduit du destinataire."""
        _assert_sendable(to, message, channel, with_media=has_media(media, attachments))
        body = _send_body(
            to, message, channel=channel, subject=subject, status_callback=status_callback,
            media=media, file_name=file_name, content_type=content_type,
            media_type=media_type, attachments=attachments,
        )
        data = await self._client._request(
            "POST", "/messages", body=body, idempotency_key=idempotency_key
        )
        return MessageResource.from_dict(data)

    async def get(self, sid: str) -> MessageResource:
        """Statut courant d'un message."""
        cleaned = _clean_sid(sid)
        data = await self._client._request("GET", f"/messages/{quote(cleaned, safe='')}")
        return MessageResource.from_dict(data)

    async def list(
        self,
        channel: Optional[str] = None,
        status: Optional[str] = None,
        to: Optional[str] = None,
        page: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> MessageList:
        """Liste paginée (filtres canal / statut / destinataire « contient »)."""
        data = await self._client._request(
            "GET",
            "/messages",
            query={"channel": channel, "status": status, "to": to, "page": page, "limit": limit},
        )
        return MessageList.from_dict(data)

    async def history(
        self,
        channel: Optional[str] = None,
        status: Optional[str] = None,
        page: Optional[int] = None,
    ) -> Any:
        """(Déprécié) Endpoint historique aux lignes brutes — préférez :meth:`list`."""
        warnings.warn(_HISTORY_DEPRECATION, DeprecationWarning, stacklevel=2)
        return await self._client._request(
            "GET", "/messages/history", query={"channel": channel, "status": status, "page": page}
        )


class _AsyncChannelResource:
    """Socle des ressources par canal (version asynchrone)."""

    _path = ""
    _channel = ""

    def __init__(self, client: AsyncFameenMessaging) -> None:
        self._client = client

    async def _send(
        self,
        to: str,
        message: Any,
        subject: Optional[str] = None,
        status_callback: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        *,
        media: Optional[MediaContent] = None,
        file_name: Optional[str] = None,
        content_type: Optional[str] = None,
        media_type: Optional[str] = None,
        attachments: Optional[List[Mapping[str, Any]]] = None,
    ) -> MessageResource:
        _assert_sendable(to, message, self._channel, with_media=has_media(media, attachments))
        body = _send_body(
            to, message, subject=subject, status_callback=status_callback,
            media=media, file_name=file_name, content_type=content_type,
            media_type=media_type, attachments=attachments,
        )
        data = await self._client._request(
            "POST", self._path, body=body, idempotency_key=idempotency_key
        )
        return MessageResource.from_dict(data)


class AsyncSmsResource(_AsyncChannelResource):
    """Envoi SMS (version asynchrone)."""

    _path = "/sms/send"
    _channel = "sms"

    async def send(
        self,
        to: str,
        message: str,
        status_callback: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> MessageResource:
        """Envoie un SMS à ``to`` (numéro E.164)."""
        return await self._send(
            to, message, status_callback=status_callback, idempotency_key=idempotency_key
        )


class AsyncWhatsappResource(_AsyncChannelResource):
    """Envoi WhatsApp (version asynchrone)."""

    _path = "/whatsapp/send"
    _channel = "whatsapp"

    async def send(
        self,
        to: str,
        message: str = "",
        status_callback: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        *,
        media: Optional[MediaContent] = None,
        file_name: Optional[str] = None,
        content_type: Optional[str] = None,
        media_type: Optional[str] = None,
        attachments: Optional[List[Mapping[str, Any]]] = None,
    ) -> MessageResource:
        """Envoie un message WhatsApp à ``to`` (numéro E.164, média possible)."""
        return await self._send(
            to, message, status_callback=status_callback, idempotency_key=idempotency_key,
            media=media, file_name=file_name, content_type=content_type,
            media_type=media_type, attachments=attachments,
        )


class AsyncEmailResource(_AsyncChannelResource):
    """Envoi email (version asynchrone)."""

    _path = "/email/send"
    _channel = "email"

    async def send(
        self,
        to: str,
        message: str = "",
        subject: Optional[str] = None,
        status_callback: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        *,
        media: Optional[MediaContent] = None,
        file_name: Optional[str] = None,
        content_type: Optional[str] = None,
        media_type: Optional[str] = None,
        attachments: Optional[List[Mapping[str, Any]]] = None,
    ) -> MessageResource:
        """Envoie un email à ``to`` (``subject`` ≤ 255 caractères, pièces jointes possibles)."""
        return await self._send(
            to,
            message,
            subject=subject,
            status_callback=status_callback,
            idempotency_key=idempotency_key,
            media=media,
            file_name=file_name,
            content_type=content_type,
            media_type=media_type,
            attachments=attachments,
        )


class AsyncWalletResource:
    """Portefeuille du compte (version asynchrone)."""

    def __init__(self, client: AsyncFameenMessaging) -> None:
        self._client = client

    async def balance(self) -> WalletBalance:
        """Soldes SMS / WhatsApp / Email et mode de facturation."""
        data = await self._client._request("GET", "/wallet/balance")
        return WalletBalance.from_dict(data)
