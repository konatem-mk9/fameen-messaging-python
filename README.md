# Fameen Messaging — SDK Python officiel

SDK Python officiel de l'API **Fameen Messaging** : envoyez des **SMS**, des messages **WhatsApp** et des **emails** depuis vos applications Python, suivez leur statut et recevez des webhooks signés.

- Paquet PyPI : `fameen-messaging` (module `fameen_messaging`) — version 0.1.0
- Python ≥ 3.9 — dépendance unique : [`httpx`](https://www.python-httpx.org/)
- Client synchrone (`FameenMessaging`) **et** asynchrone (`AsyncFameenMessaging`)
- Réessais automatiques (réseau, 429, 5xx idempotents), erreurs typées, vérification de webhooks en temps constant

## Installation

```bash
pip install fameen-messaging
```

## Démarrage rapide

```python
import os
from fameen_messaging import FameenMessaging

client = FameenMessaging(api_key=os.environ["FAMEEN_API_KEY"])  # clé "fam_…", jamais en dur

# SMS
message = client.sms.send("+224620000000", "Bonjour {prenom} !")
print(message.sid, message.status)  # msg_…  queued

# WhatsApp
client.whatsapp.send("+224620000000", "Votre commande est prête ✅")

# Email
client.email.send("client@exemple.com", "Corps du message", subject="Bienvenue !")

# Envoi unifié — canal explicite ou déduit (« @ » dans `to` → email, sinon SMS)
client.messages.create("client@exemple.com", "Bonjour !", subject="Info")
client.messages.create("+224620000000", "Bonjour !", channel="whatsapp")

# Suivi
statut = client.messages.get(message.sid)
page = client.messages.list(channel="sms", status="delivered", page=1, limit=30)
for m in page.data:
    print(m.sid, m.status, m.delivered_at)

# Solde du portefeuille
solde = client.wallet.balance()
print(solde.sms_credits, solde.wa_credits, solde.email_credits, solde.billing.mode)
```

Le contenu accepte les variables de personnalisation `{prenom}`, `{nom}`, `{email}`, `{phone}` (max 5 000 caractères ; `subject` ≤ 255).

## Client asynchrone

Mêmes méthodes, en `async`, sur `httpx.AsyncClient` :

```python
import asyncio
import os
from fameen_messaging import AsyncFameenMessaging

async def main():
    async with AsyncFameenMessaging(api_key=os.environ["FAMEEN_API_KEY"]) as client:
        message = await client.sms.send("+224620000000", "Bonjour !")
        solde = await client.wallet.balance()
        print(message.sid, solde.sms_credits)

asyncio.run(main())
```

## Idempotence

Passez une `idempotency_key` (en-tête `Idempotency-Key`, fenêtre de 24 h côté serveur) : tout réessai renvoie la réponse d'origine au lieu de créer un doublon — et cela rend les réessais automatiques du SDK **sûrs sur les POST** :

```python
client.sms.send("+224620000000", "Commande confirmée", idempotency_key="commande-42-confirmation")
```

## Erreurs

Toutes les erreurs du SDK héritent de `FameenError` :

```python
from fameen_messaging import FameenAPIError, FameenConnectionError

try:
    client.sms.send("+224620000000", "Bonjour !")
except FameenAPIError as err:
    print(err.status, err.code, err)      # ex. 402 insufficient_credits Crédits insuffisants…
    if err.code == "insufficient_credits":
        ...  # rechargez le portefeuille
    if err.retry_after is not None:
        ...  # 429 : attendez err.retry_after secondes
except FameenConnectionError:
    ...  # l'API n'a pas pu être jointe (DNS, timeout, coupure réseau)
```

| Exception | Quand | Attributs |
|---|---|---|
| `FameenError` | classe mère | `str(err)` = message |
| `FameenAPIError` | réponse HTTP non-2xx | `status`, `code`, `retry_after`, `rate_limit` |
| `FameenConnectionError` | API injoignable après épuisement des réessais | — |
| `WebhookVerificationError` | signature/corps de webhook invalide | — |

Codes stables (`err.code`) : `bad_request` (400), `unauthorized` (401), `insufficient_credits` (402), `channel_not_allowed` (403), `not_found` (404), `rate_limited` (429), `internal_error` (5xx), `unknown_error`. Si le corps d'erreur est illisible, le code est déduit du statut HTTP.

Une validation locale est faite **avant** tout appel réseau (lève `TypeError`) : `to` et `message` non vides ; `to` contenant « @ » refusé si le canal explicite n'est pas `email`.

## Réessais automatiques

`max_retries` = 2 par défaut ; backoff exponentiel `retry_base × 2^tentative + aléa(0..retry_base)` (base 0,5 s).

| Situation | Réessayé ? |
|---|---|
| Erreur réseau (DNS, timeout, coupure) | ✅ toutes méthodes |
| HTTP 429 | ✅ en respectant `Retry-After` (secondes) si présent |
| HTTP 5xx sur **GET** | ✅ |
| HTTP 5xx sur **POST avec `idempotency_key`** | ✅ |
| HTTP 5xx sur POST **sans** clé d'idempotence | ❌ (la requête a pu être traitée côté serveur) |
| HTTP 4xx (400, 401, 402, 403, 404…) | ❌ |

## Limite de débit

60 requêtes/minute/clé. Chaque réponse expose `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset` (epoch secondes) ; le SDK les mémorise :

```python
info = client.last_rate_limit  # RateLimitInfo(limit=60, remaining=42, reset=1770000000) ou None
```

## Webhooks

L'API notifie votre `status_callback` à chaque changement de statut (`event` ∈ `queued | sent | delivered | failed`). Chaque requête est signée : **HMAC-SHA256 (hex) du corps brut** avec le secret `whsec_…` du compte, dans l'en-tête `X-Fameen-Signature` (en-tête informatif : `X-Fameen-Event`).

⚠️ Vérifiez toujours la signature sur le **corps brut** (les octets reçus, avant tout parsing JSON). La comparaison est faite en temps constant.

```python
from fameen_messaging import verify_webhook_signature, construct_webhook_event, WebhookVerificationError

ok = verify_webhook_signature(corps_brut, signature, secret)   # -> bool
event = construct_webhook_event(corps_brut, signature, secret)  # -> WebhookEvent (vérifie PUIS parse)
```

### Django

```python
# views.py
import os
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt
from fameen_messaging import construct_webhook_event, WebhookVerificationError

@csrf_exempt
def fameen_webhook(request):
    try:
        event = construct_webhook_event(
            request.body,  # corps brut : request.body, PAS request.POST
            request.headers.get("X-Fameen-Signature"),
            os.environ["FAMEEN_WEBHOOK_SECRET"],
        )
    except WebhookVerificationError:
        return HttpResponse(status=401)

    if event.event == "delivered":
        ...  # mettez à jour votre base avec event.sid
    return HttpResponse(status=200)
```

### Flask

```python
import os
from flask import Flask, request
from fameen_messaging import construct_webhook_event, WebhookVerificationError

app = Flask(__name__)

@app.post("/webhooks/fameen")
def fameen_webhook():
    try:
        event = construct_webhook_event(
            request.get_data(),  # corps brut : get_data(), PAS request.json
            request.headers.get("X-Fameen-Signature"),
            os.environ["FAMEEN_WEBHOOK_SECRET"],
        )
    except WebhookVerificationError:
        return "", 401

    print(event.sid, event.status)
    return "", 200
```

### FastAPI

```python
import os
from fastapi import FastAPI, Request, Response
from fameen_messaging import construct_webhook_event, WebhookVerificationError

app = FastAPI()

@app.post("/webhooks/fameen")
async def fameen_webhook(request: Request):
    payload = await request.body()  # corps brut, avant tout parsing
    try:
        event = construct_webhook_event(
            payload,
            request.headers.get("x-fameen-signature"),
            os.environ["FAMEEN_WEBHOOK_SECRET"],
        )
    except WebhookVerificationError:
        return Response(status_code=401)

    print(event.sid, event.status)
    return Response(status_code=200)
```

## Configuration

```python
FameenMessaging(
    api_key="fam_…",          # requis
    base_url="https://business.fameengroupe.com/api/v1",  # défaut ; « / » finaux retirés
    timeout=30.0,              # timeout httpx par tentative, en secondes
    max_retries=2,             # réessais automatiques
    retry_base=0.5,            # base du backoff exponentiel (s) — utile en test
    transport=None,            # transport httpx injectable (httpx.MockTransport en test)
)
```

`AsyncFameenMessaging` accepte exactement les mêmes options.

## Modèles de données

Les retours sont des **dataclasses gelées**, construites de manière tolérante depuis le JSON (champs inconnus ignorés, champs manquants → `None`/`0`). Les clés camelCase deviennent snake_case (`externalId` → `external_id`) ; la clé `from` (mot réservé Python) devient `from_`.

| Dataclass | Renvoyée par |
|---|---|
| `MessageResource` | `sms/whatsapp/email.send`, `messages.create`, `messages.get` |
| `MessageList` | `messages.list` (`.data` = liste de `MessageResource`) |
| `WalletBalance` (+ `WalletBilling`) | `wallet.balance` |
| `WebhookEvent` | `construct_webhook_event` |
| `RateLimitInfo` | `client.last_rate_limit`, `err.rate_limit` |

`messages.history()` est **déprécié** (lignes brutes, `DeprecationWarning`) — préférez `messages.list()`.

## Tests (développement)

```bash
python -m venv .venv
.venv/Scripts/python -m pip install httpx pytest pytest-asyncio   # (Linux/macOS : .venv/bin/python)
.venv/Scripts/python -m pytest -q
```

Les tests utilisent `httpx.MockTransport` : aucun appel réseau.

## Licence

MIT — © Fameen Groupe.
