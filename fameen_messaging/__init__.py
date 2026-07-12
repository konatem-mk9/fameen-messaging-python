"""SDK Python officiel de l'API Fameen Messaging (SMS, WhatsApp, Email).

Exemple minimal::

    from fameen_messaging import FameenMessaging

    client = FameenMessaging(api_key="fam_…")
    message = client.sms.send("+224620000000", "Bonjour {prenom} !")
    print(message.sid, message.status)

Webhooks::

    from fameen_messaging import construct_webhook_event

    event = construct_webhook_event(corps_brut, signature, secret)
"""

from .client import (
    DEFAULT_BASE_URL,
    VERSION,
    AsyncFameenMessaging,
    FameenMessaging,
)
from .errors import (
    FameenAPIError,
    FameenConnectionError,
    FameenError,
    WebhookVerificationError,
)
from .types import (
    MessageList,
    MessageResource,
    RateLimitInfo,
    WalletBalance,
    WalletBilling,
    WebhookEvent,
)
from .webhooks import construct_webhook_event, verify_webhook_signature

__version__ = VERSION

__all__ = [
    # Clients
    "FameenMessaging",
    "AsyncFameenMessaging",
    "DEFAULT_BASE_URL",
    # Erreurs
    "FameenError",
    "FameenAPIError",
    "FameenConnectionError",
    "WebhookVerificationError",
    # Modèles
    "MessageResource",
    "MessageList",
    "WalletBalance",
    "WalletBilling",
    "WebhookEvent",
    "RateLimitInfo",
    # Webhooks
    "verify_webhook_signature",
    "construct_webhook_event",
    # Divers
    "VERSION",
    "__version__",
]
