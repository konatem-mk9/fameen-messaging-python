"""Pièces jointes des messages (WhatsApp & email).

Le client transmet chaque fichier **encodé en base64** ; l'API l'héberge puis
le distribue (URL signée pour WhatsApp, pièce jointe inline pour l'email).
Aucune URL publique n'est requise de votre côté.
"""

from __future__ import annotations

import base64
import mimetypes
import os
from typing import Any, Dict, List, Mapping, Optional, Union

#: Contenu accepté pour une pièce jointe : octets bruts ou base64 déjà encodé.
MediaContent = Union[str, bytes, bytearray, memoryview]


def encode_media(content: MediaContent) -> str:
    """Encode un contenu média en base64 pour le transport JSON.

    Les ``str`` sont supposés déjà encodés (base64 ou data-URI) et passent tels
    quels ; les octets bruts sont convertis.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(content)).decode("ascii")
    raise TypeError(
        "Contenu média invalide : attendu str (base64), bytes, bytearray ou memoryview."
    )


def _attachment_dict(att: Mapping[str, Any]) -> Dict[str, Any]:
    """Normalise une pièce jointe en dict prêt pour l'API (contenu encodé)."""
    if "content" not in att:
        raise TypeError("Chaque pièce jointe doit fournir `content`.")
    out: Dict[str, Any] = {"content": encode_media(att["content"])}
    for key in ("filename", "content_type", "type"):
        value = att.get(key)
        if value is not None:
            # L'API attend `contentType` (camelCase).
            out["contentType" if key == "content_type" else key] = value
    return out


def build_media_fields(
    *,
    media: Optional[MediaContent] = None,
    file_name: Optional[str] = None,
    content_type: Optional[str] = None,
    media_type: Optional[str] = None,
    attachments: Optional[List[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Construit le fragment de corps média (clés camelCase de l'API, sans ``None``)."""
    fields: Dict[str, Any] = {}
    if media is not None:
        fields["media"] = encode_media(media)
        if file_name is not None:
            fields["fileName"] = file_name
        if content_type is not None:
            fields["contentType"] = content_type
        if media_type is not None:
            fields["mediaType"] = media_type
    if attachments:
        fields["attachments"] = [_attachment_dict(a) for a in attachments]
    return fields


def has_media(
    media: Optional[MediaContent],
    attachments: Optional[List[Mapping[str, Any]]],
) -> bool:
    """``True`` si un média (raccourci ou tableau) est présent."""
    return media is not None or bool(attachments)


def file_attachment(
    path: str,
    *,
    filename: Optional[str] = None,
    content_type: Optional[str] = None,
    type: Optional[str] = None,  # noqa: A002 - miroir du champ API
) -> Dict[str, Any]:
    """Construit une pièce jointe depuis un fichier local.

    Exemple::

        att = file_attachment("./facture.pdf")
        client.email.send("a@b.com", "Voir pièce jointe",
                          subject="Facture", attachments=[att])
    """
    with open(path, "rb") as handle:
        content = handle.read()
    guessed = content_type or mimetypes.guess_type(path)[0]
    att: Dict[str, Any] = {"content": content, "filename": filename or os.path.basename(path)}
    if guessed:
        att["content_type"] = guessed
    if type:
        att["type"] = type
    return att
