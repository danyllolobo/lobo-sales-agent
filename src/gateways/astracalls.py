"""Conector AstraCalls (Go + whatsmeow, self-hosted).

Referência: github.com/AstraOnlineWeb/AstraCalls (client/public/openapi.yaml, cmd/server/webhook.go)
- Webhook:  `{session, event, timestamp, data}`; `event="message"` traz
            `data.{id, chat, sender, fromMe, pushName, timestamp(ms), isGroup, type, text,
            quotedMessageId, referral, raw}` (`raw` = protojson do waE2E.Message).
- Envio:    POST /api/sessions/{sid}/messages/{text|image|audio|video|document}  (`to` = número ou JID)
- Presença: POST /api/sessions/{sid}/messages/typing  {to, typing}
- Leitura:  POST /api/sessions/{sid}/messages/seen    {to, messageIds}
- Mídia:    GET  /api/sessions/{sid}/messages/{id}/media
- Auth:     header `X-API-Key` (+ `X-Client-Id` opcional, apenas identificação)
- HMAC:     header `X-Webhook-Signature: sha256=<hex>`
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Mapping
from typing import Any, ClassVar
from urllib.parse import quote, unquote, urlsplit

import httpx
from pydantic import SecretStr, ValidationError

from src.core.exceptions import GatewayError
from src.core.schemas import (
    GeoLocation,
    IncomingMessage,
    MediaAttachment,
    MessageType,
    OutgoingMessage,
    Provider,
    normalize_phone,
    utcnow,
)
from src.gateways.base import (
    ParsedWebhook,
    SendResult,
    WhatsAppGateway,
    dig,
    epoch_to_datetime,
    get_header,
    is_non_contact_jid,
    to_float,
    to_int,
)

logger = logging.getLogger("lobo.gateway.astracalls")

_MEDIA_TYPES: dict[str, MessageType] = {
    "image": MessageType.IMAGE,
    "audio": MessageType.AUDIO,
    "video": MessageType.VIDEO,
    "document": MessageType.DOCUMENT,
    "sticker": MessageType.STICKER,
}
_CAPTION_TYPES = frozenset({MessageType.IMAGE, MessageType.VIDEO, MessageType.DOCUMENT})
_SEND_MEDIA_PATH: dict[MessageType, str] = {
    MessageType.IMAGE: "image",
    MessageType.AUDIO: "audio",
    MessageType.VIDEO: "video",
    MessageType.DOCUMENT: "document",
}


class AstraCallsGateway(WhatsAppGateway):
    provider: ClassVar[Provider] = Provider.ASTRACALLS

    def __init__(
        self,
        base_url: str,
        api_key: SecretStr | str | None = None,
        *,
        default_session: str,
        client_id: str | None = None,
        webhook_secret: SecretStr | str | None = None,
        timeout: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            base_url,
            api_key,
            default_session=default_session,
            webhook_secret=webhook_secret,
            timeout=timeout,
            client=client,
        )
        self.client_id = client_id

    # ------------------------------------------------------------------ #
    # Webhook → IncomingMessage
    # ------------------------------------------------------------------ #
    def parse_webhook(self, tenant_id: str, payload: Mapping[str, Any]) -> ParsedWebhook:
        result = ParsedWebhook()
        event = payload.get("event")
        if event != "message":
            return result.ignore("event_not_supported", event)

        data = payload.get("data")
        if not isinstance(data, Mapping) or not data.get("id"):
            return result.ignore("invalid_payload", event)
        msg_id = str(data["id"])

        chat_jid = str(data.get("chat") or "")
        if data.get("isGroup") or not chat_jid or is_non_contact_jid(chat_jid):
            return result.ignore("not_a_contact", event, msg_id)

        from_me = bool(data.get("fromMe"))
        contact_jid = chat_jid
        if chat_jid.endswith("@lid"):
            sender = str(data.get("sender") or "")
            if not from_me and "@s.whatsapp.net" in sender:
                contact_jid = sender
            else:
                logger.warning("AstraCalls: contato @lid sem telefone resolvível (msg ignorada)")
                return result.ignore("lid_unresolved", event, msg_id)

        try:
            phone = normalize_phone(contact_jid)  # remove sufixo de device ":12@..."
        except ValueError:
            return result.ignore("invalid_contact", event, msg_id)

        text = (data.get("text") or "").strip() or None
        raw = data.get("raw") if isinstance(data.get("raw"), Mapping) else {}
        type_str = str(data.get("type") or "unknown")

        media: MediaAttachment | None = None
        location: GeoLocation | None = None
        if type_str in _MEDIA_TYPES:
            msg_type = _MEDIA_TYPES[type_str]
            media = self._media_from_raw(raw, type_str, msg_id, text if msg_type in _CAPTION_TYPES else None)
        elif type_str == "location":
            msg_type = MessageType.LOCATION
            location = self._location_from_raw(raw)
            if location is None:
                return result.ignore("invalid_payload", event, msg_id)
        elif text:
            # text, respostas de botão/lista, contato, produto, enquete... viram texto
            msg_type = MessageType.TEXT
        else:
            return result.ignore("unsupported_message", event, msg_id)

        push_name = (data.get("pushName") or "").strip() or None
        referral = data.get("referral")
        quoted_id, quoted_text = self._extract_quoted(data, raw)
        try:
            message = IncomingMessage(
                message_id=msg_id,
                tenant_id=tenant_id,
                provider=self.provider,
                session_id=str(payload.get("session") or self.default_session),
                contact_phone=phone,
                contact_name=None if from_me else push_name,
                chat_id=chat_jid,
                type=msg_type,
                text=text if msg_type is MessageType.TEXT else None,
                media=media,
                location=location,
                quoted_message_id=quoted_id,
                quoted_text=quoted_text,
                ad_referral=dict(referral) if isinstance(referral, Mapping) else None,
                from_me=from_me,
                timestamp=epoch_to_datetime(data.get("timestamp")) or utcnow(),
                raw_payload=dict(payload),
            )
        except ValidationError:
            return result.ignore("invalid_payload", event, msg_id)

        result.messages.append(message)
        return result

    @staticmethod
    def _extract_quoted(data: Mapping[str, Any], raw: Mapping[str, Any]) -> tuple[str | None, str | None]:
        """Extrai o ID e o texto da mensagem citada em payloads AstraCalls / whatsmeow."""
        ctx_candidates = (
            data.get("contextInfo"),
            raw.get("contextInfo"),
            dig(raw, "extendedTextMessage", "contextInfo"),
            dig(raw, "imageMessage", "contextInfo"),
            dig(raw, "audioMessage", "contextInfo"),
            dig(raw, "videoMessage", "contextInfo"),
            dig(raw, "documentMessage", "contextInfo"),
            dig(raw, "buttonsResponseMessage", "contextInfo"),
            dig(raw, "listResponseMessage", "contextInfo"),
        )
        ctx = next((c for c in ctx_candidates if isinstance(c, Mapping)), {})
        quoted_msg = (
            ctx.get("quotedMessage")
            or data.get("quotedMessage")
            or raw.get("quotedMessage")
            or {}
        )
        quoted_msg = quoted_msg if isinstance(quoted_msg, Mapping) else {}

        quoted_id_raw = data.get("quotedMessageId") or ctx.get("stanzaId") or ctx.get("stanzaID")
        quoted_id = str(quoted_id_raw).strip() if quoted_id_raw else None

        text_candidates = (
            data.get("quotedText"),
            data.get("quoted_text"),
            quoted_msg.get("conversation"),
            dig(quoted_msg, "extendedTextMessage", "text"),
            quoted_msg.get("text"),
            dig(quoted_msg, "imageMessage", "caption"),
            dig(quoted_msg, "videoMessage", "caption"),
            dig(quoted_msg, "documentMessage", "caption"),
            dig(quoted_msg, "buttonsMessage", "contentText"),
        )
        quoted_text = next(
            (str(c).strip() for c in text_candidates if isinstance(c, str) and c.strip()),
            None,
        )
        return quoted_id, quoted_text

    @staticmethod
    def _media_from_raw(raw: Mapping[str, Any], type_str: str, msg_id: str, caption: str | None) -> MediaAttachment:
        sub = raw.get(f"{type_str}Message")
        if not isinstance(sub, Mapping) and type_str == "document":
            sub = dig(raw, "documentWithCaptionMessage", "message", "documentMessage")
        sub = sub if isinstance(sub, Mapping) else {}
        # A URL do proto aponta para o CDN criptografado do WhatsApp: o download
        # é feito pelo endpoint /messages/{id}/media usando o id da mensagem.
        return MediaAttachment(
            provider_media_id=msg_id,
            mimetype=sub.get("mimetype"),
            filename=sub.get("fileName"),
            caption=caption,
            duration_seconds=to_float(sub.get("seconds")),
            size_bytes=to_int(sub.get("fileLength")),
        )

    @staticmethod
    def _location_from_raw(raw: Mapping[str, Any]) -> GeoLocation | None:
        loc = raw.get("locationMessage") or raw.get("liveLocationMessage")
        if not isinstance(loc, Mapping):
            return None
        try:
            return GeoLocation(
                latitude=float(loc["degreesLatitude"]),
                longitude=float(loc["degreesLongitude"]),
                name=loc.get("name"),
                address=loc.get("address"),
            )
        except (KeyError, TypeError, ValueError, ValidationError):
            return None

    # ------------------------------------------------------------------ #
    # Assinatura
    # ------------------------------------------------------------------ #
    def verify_signature(self, body: bytes, headers: Mapping[str, str]) -> bool:
        return self._hmac_matches(
            self._webhook_secret, body, get_header(headers, "X-Webhook-Signature"), "sha256", prefix="sha256="
        )

    # ------------------------------------------------------------------ #
    # Saída
    # ------------------------------------------------------------------ #
    def _auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        if self.client_id:
            headers["X-Client-Id"] = self.client_id
        return headers

    @staticmethod
    def _session_path(session_id: str, suffix: str) -> str:
        return f"/api/sessions/{quote(session_id, safe='')}/{suffix}"

    @staticmethod
    def _to(to: str) -> str:
        """AstraCalls aceita número (DDI+DDD) ou JID completo (inclusive @lid)."""
        return to if "@" in to else normalize_phone(to)

    async def set_typing(self, session_id: str, to: str, typing: bool = True) -> None:
        await self._request(
            "POST",
            self._session_path(session_id, "messages/typing"),
            json={"to": self._to(to), "typing": typing},
            log_contact=to,
        )

    async def mark_seen(self, session_id: str, to: str, message_ids: list[str]) -> None:
        if not message_ids:
            return  # a API exige ao menos um id
        await self._request(
            "POST",
            self._session_path(session_id, "messages/seen"),
            json={"to": self._to(to), "messageIds": message_ids},
            log_contact=to,
        )

    async def react(self, session_id: str, chat_id: str, message_id: str, emoji: str) -> None:
        await self._request(
            "POST",
            self._session_path(session_id, "messages/reaction"),
            json={"to": self._to(chat_id), "messageId": message_id, "emoji": emoji},
            log_contact=chat_id,
        )

    async def send_message(self, message: OutgoingMessage) -> SendResult:
        to = self.destination(message)
        body: dict[str, Any] = {"to": self._to(to)}
        if message.reply_to_message_id:
            body["quotedMessageId"] = message.reply_to_message_id

        if message.type is MessageType.TEXT:
            suffix = "messages/text"
            body["text"] = message.text
        else:
            suffix = f"messages/{_SEND_MEDIA_PATH[message.type]}"
            media = message.media
            assert media is not None  # garantido pelo schema
            if media.url:
                body["url"] = media.url
            elif media.data_base64:
                body["base64"] = media.data_base64
            else:
                raise GatewayError("AstraCalls: mídia de saída exige url ou base64")
            if media.mimetype:
                body["mimetype"] = media.mimetype
            if message.type is MessageType.DOCUMENT and media.filename:
                body["filename"] = media.filename
            if message.type in _CAPTION_TYPES and message.text:
                body["caption"] = message.text
            if message.type is MessageType.AUDIO:
                body["ptt"] = True  # nota de voz

        resp = await self._request("POST", self._session_path(message.session_id, suffix), json=body, log_contact=to)
        mid = resp.get("id") if isinstance(resp, Mapping) else None
        return SendResult(provider=self.provider, message_id=str(mid) if mid else None, raw=resp or {})

    @staticmethod
    def _is_safe_media_path(path: str) -> bool:
        """Valida caminhos de mídia do AstraCalls contra Path Traversal (`..`) e rotas fora de mídia."""
        if not path or not path.startswith("/"):
            return False
        decoded = unquote(path)
        if ".." in decoded or "\\" in decoded or "//" in decoded:
            return False
        return decoded.startswith(("/api/", "/v1/", "/files/", "/media/")) and (
            "/media" in decoded or "/files/" in decoded
        )

    @staticmethod
    def _is_safe_storage_url(url: str) -> bool:
        """Valida URLs pré-assinadas de Object Storage (MinIO / S3) bloqueando SSRF e Path Traversal."""
        if not url or not isinstance(url, str):
            return False
        parts = urlsplit(url.strip())
        if parts.scheme.lower() not in ("https", "http") or not parts.hostname:
            return False
        host = parts.hostname.lower()
        if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1", "169.254.169.254") or host.startswith(
            ("127.", "169.254.")
        ):
            return False
        decoded_path = unquote(parts.path or "")
        if not decoded_path.startswith("/") or ".." in decoded_path or "\\" in decoded_path or "//" in decoded_path:
            return False
        if decoded_path.startswith("/api/"):
            return False
        query_lower = (parts.query or "").lower()
        return any(
            k in query_lower for k in ("x-amz-signature", "x-amz-algorithm", "x-amz-credential", "signature=", "token=")
        ) or decoded_path.lower().endswith(
            (".oga", ".ogg", ".opus", ".mp3", ".m4a", ".wav", ".aac", ".webm", ".mp4", ".jpg", ".jpeg", ".png", ".webp", ".pdf")
        )

    async def download_media(self, message: IncomingMessage) -> bytes:
        media = message.media
        if media is None:
            raise GatewayError("AstraCalls: mensagem sem mídia", tenant_id=message.tenant_id)
        if media.data_base64:
            return base64.b64decode(media.data_base64.split(",", 1)[-1])

        raw_b64 = dig(message.raw_payload, "data", "base64") or dig(message.raw_payload, "data", "data_base64")
        if isinstance(raw_b64, str) and raw_b64.strip():
            return base64.b64decode(raw_b64.strip().split(",", 1)[-1])

        candidates: list[tuple[str, str, bool]] = []
        if media.url:
            clean_url = media.url.strip()
            parts = urlsplit(clean_url)
            if self._is_safe_media_path(parts.path):
                path_q = parts.path + (f"?{parts.query}" if parts.query else "")
                candidates.append((parts.path, f"{self.base_url}{path_q}", True))
            elif self._is_safe_storage_url(clean_url):
                candidates.append((parts.path, clean_url, False))
            else:
                logger.warning("AstraCalls: caminho de mídia ignorado por segurança: %s", parts.path)

        media_id = media.provider_media_id or message.message_id
        msg_path = self._session_path(message.session_id, f"messages/{quote(media_id, safe='')}/media")
        candidates.append((msg_path, f"{self.base_url}{msg_path}", True))

        last_err: GatewayError | None = None
        for path_label, full_url, include_auth in candidates:
            try:
                raw_bytes: bytes = await self._request(
                    "GET",
                    path_label,
                    absolute_url=full_url,
                    headers={"Accept": "*/*"},
                    expect_json=False,
                    include_auth=include_auth,
                    log_contact=message.contact_phone,
                )
                stripped = raw_bytes.lstrip()
                if stripped.startswith(b"{"):
                    try:
                        obj = json.loads(stripped.decode("utf-8"))
                        if isinstance(obj, Mapping):
                            b64 = obj.get("base64") or obj.get("data_base64") or obj.get("data")
                            if isinstance(b64, str) and b64.strip():
                                return base64.b64decode(b64.strip().split(",", 1)[-1])
                    except (UnicodeDecodeError, ValueError):
                        pass
                if raw_bytes:
                    return raw_bytes
            except GatewayError as exc:
                last_err = exc
                logger.warning("AstraCalls: falha ao baixar mídia em %s (%s), tentando fallback", path_label, exc.message)

        if last_err is not None:
            raise last_err
        raise GatewayError("AstraCalls: não foi possível baixar a mídia", tenant_id=message.tenant_id)
