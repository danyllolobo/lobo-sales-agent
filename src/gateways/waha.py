"""Conector WAHA (WhatsApp HTTP API — self-hosted).

Referência: https://waha.devlike.pro
- Webhook: eventos `message` / `message.any` no formato `{event, session, payload, ...}`.
- Envio:   POST /api/sendText | sendImage | sendVoice | sendVideo | sendFile
- Presença: POST /api/startTyping | /api/stopTyping
- Leitura:  POST /api/sendSeen
- Auth:     header `X-Api-Key`
- HMAC:     header `X-Webhook-Hmac` (SHA-512 hex do corpo bruto)
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Mapping
from typing import Any, ClassVar
from urllib.parse import urlsplit

from pydantic import ValidationError

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
)

logger = logging.getLogger("lobo.gateway.waha")

_SUPPORTED_EVENTS = frozenset({"message", "message.any"})

# `_data.type` do engine WEBJS → tipo neutro
_DATA_TYPE_MAP: dict[str, MessageType] = {
    "chat": MessageType.TEXT,
    "ptt": MessageType.AUDIO,
    "audio": MessageType.AUDIO,
    "image": MessageType.IMAGE,
    "video": MessageType.VIDEO,
    "document": MessageType.DOCUMENT,
    "sticker": MessageType.STICKER,
    "location": MessageType.LOCATION,
}

_SEND_MEDIA_ENDPOINT: dict[MessageType, str] = {
    MessageType.IMAGE: "/api/sendImage",
    MessageType.VIDEO: "/api/sendVideo",
    MessageType.AUDIO: "/api/sendVoice",
    MessageType.DOCUMENT: "/api/sendFile",
}

_CAPTION_TYPES = frozenset({MessageType.IMAGE, MessageType.VIDEO, MessageType.DOCUMENT})


class WahaGateway(WhatsAppGateway):
    provider: ClassVar[Provider] = Provider.WAHA

    # ------------------------------------------------------------------ #
    # Webhook → IncomingMessage
    # ------------------------------------------------------------------ #
    def parse_webhook(self, tenant_id: str, payload: Mapping[str, Any]) -> ParsedWebhook:
        result = ParsedWebhook()
        event = payload.get("event")
        if event not in _SUPPORTED_EVENTS:
            return result.ignore("event_not_supported", event)

        p = payload.get("payload")
        if not isinstance(p, Mapping) or not p.get("id"):
            return result.ignore("invalid_payload", event)
        msg_id = str(p["id"])
        data = p.get("_data") if isinstance(p.get("_data"), Mapping) else {}

        from_me = bool(p.get("fromMe"))
        chat_jid = str((p.get("to") if from_me else p.get("from")) or "")
        if not chat_jid or is_non_contact_jid(chat_jid):
            return result.ignore("not_a_contact", event, msg_id)

        contact_jid = chat_jid
        if chat_jid.endswith("@lid"):
            contact_jid = self._resolve_lid(data, from_me) or ""
            if not contact_jid:
                logger.warning("WAHA: contato @lid sem telefone resolvível (msg ignorada)")
                return result.ignore("lid_unresolved", event, msg_id)

        try:
            phone = normalize_phone(contact_jid)
        except ValueError:
            return result.ignore("invalid_contact", event, msg_id)

        msg_type, media, location = self._classify(p, data, msg_id)
        if msg_type is None:
            return result.ignore("unsupported_message", event, msg_id)

        body = (p.get("body") or "").strip() or None
        reply_to = p.get("replyTo")
        try:
            message = IncomingMessage(
                message_id=msg_id,
                tenant_id=tenant_id,
                provider=self.provider,
                session_id=str(payload.get("session") or self.default_session),
                contact_phone=phone,
                contact_name=None if from_me else self._contact_name(data),
                chat_id=chat_jid,
                type=msg_type,
                text=body if msg_type is MessageType.TEXT else None,
                media=media,
                location=location,
                quoted_message_id=dig(reply_to, "id") if isinstance(reply_to, Mapping) else None,
                from_me=from_me,
                timestamp=epoch_to_datetime(p.get("timestamp")) or utcnow(),
                raw_payload=dict(payload),
            )
        except ValidationError:
            return result.ignore("invalid_payload", event, msg_id)

        result.messages.append(message)
        return result

    @staticmethod
    def _resolve_lid(data: Mapping[str, Any], from_me: bool) -> str | None:
        """Tenta achar o telefone real de um chat `@lid` nos campos dos engines NOWEB/GOWS."""
        candidates = [
            dig(data, "key", "remoteJidAlt"),
            dig(data, "key", "senderPn"),
            dig(data, "Info", "RecipientAlt" if from_me else "SenderAlt"),
            dig(data, "Info", "ChatAlt"),
        ]
        for c in candidates:
            if isinstance(c, str) and c.endswith(("@s.whatsapp.net", "@c.us")):
                return c
        return None

    @staticmethod
    def _contact_name(data: Mapping[str, Any]) -> str | None:
        for value in (data.get("notifyName"), data.get("pushName"), dig(data, "Info", "PushName")):
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _classify(
        p: Mapping[str, Any], data: Mapping[str, Any], msg_id: str
    ) -> tuple[MessageType | None, MediaAttachment | None, GeoLocation | None]:
        location = p.get("location")
        if isinstance(location, Mapping) and location.get("latitude") is not None:
            try:
                return (
                    MessageType.LOCATION,
                    None,
                    GeoLocation(
                        latitude=float(location["latitude"]),
                        longitude=float(location["longitude"]),
                        name=location.get("name"),
                        address=location.get("description") or location.get("address"),
                    ),
                )
            except (KeyError, TypeError, ValueError, ValidationError):
                return None, None, None

        if p.get("hasMedia"):
            info = p.get("media") if isinstance(p.get("media"), Mapping) else {}
            mimetype = info.get("mimetype") or data.get("mimetype")
            data_type = _DATA_TYPE_MAP.get(str(data.get("type") or ""))
            if data_type in (None, MessageType.TEXT, MessageType.LOCATION):
                mt = (mimetype or "").lower()
                if mt.startswith("audio/"):
                    data_type = MessageType.AUDIO
                elif mt.startswith("image/"):
                    data_type = MessageType.STICKER if mt == "image/webp" else MessageType.IMAGE
                elif mt.startswith("video/"):
                    data_type = MessageType.VIDEO
                else:
                    data_type = MessageType.DOCUMENT
            caption = (p.get("body") or "").strip() or None
            media = MediaAttachment(
                url=info.get("url") or None,
                provider_media_id=msg_id,
                mimetype=mimetype,
                filename=info.get("filename") or data.get("filename"),
                caption=caption if data_type in _CAPTION_TYPES else None,
                duration_seconds=to_float(data.get("duration")),
            )
            return data_type, media, None

        if (p.get("body") or "").strip():
            return MessageType.TEXT, None, None
        return None, None, None  # reações, revogações, notificações de sistema...

    # ------------------------------------------------------------------ #
    # Assinatura
    # ------------------------------------------------------------------ #
    def verify_signature(self, body: bytes, headers: Mapping[str, str]) -> bool:
        return self._hmac_matches(self._webhook_secret, body, get_header(headers, "X-Webhook-Hmac"), "sha512")

    # ------------------------------------------------------------------ #
    # Saída
    # ------------------------------------------------------------------ #
    @staticmethod
    def chat_id_for(to: str) -> str:
        """Telefone puro → `<telefone>@c.us`; JIDs nativos são mantidos."""
        if "@" in to:
            return to.replace("@s.whatsapp.net", "@c.us")
        return f"{normalize_phone(to)}@c.us"

    def _auth_headers(self) -> dict[str, str]:
        return {"X-Api-Key": self._api_key} if self._api_key else {}

    async def set_typing(self, session_id: str, to: str, typing: bool = True) -> None:
        path = "/api/startTyping" if typing else "/api/stopTyping"
        await self._request(
            "POST", path, json={"session": session_id, "chatId": self.chat_id_for(to)}, log_contact=to
        )

    async def mark_seen(self, session_id: str, to: str, message_ids: list[str]) -> None:
        body: dict[str, Any] = {"session": session_id, "chatId": self.chat_id_for(to)}
        if message_ids:
            body["messageIds"] = message_ids
        await self._request("POST", "/api/sendSeen", json=body, log_contact=to)

    async def send_message(self, message: OutgoingMessage) -> SendResult:
        to = self.destination(message)
        body: dict[str, Any] = {"session": message.session_id, "chatId": self.chat_id_for(to)}
        if message.reply_to_message_id:
            body["reply_to"] = message.reply_to_message_id

        if message.type is MessageType.TEXT:
            path = "/api/sendText"
            body["text"] = message.text
        else:
            path = _SEND_MEDIA_ENDPOINT[message.type]
            media = message.media
            assert media is not None  # garantido pelo schema
            file: dict[str, Any] = {"mimetype": media.mimetype, "filename": media.filename}
            if media.url:
                file["url"] = media.url
            elif media.data_base64:
                file["data"] = media.data_base64
            else:
                raise GatewayError("WAHA: mídia de saída exige url ou base64")
            body["file"] = {k: v for k, v in file.items() if v is not None}
            if message.type in _CAPTION_TYPES and message.text:
                body["caption"] = message.text
            if message.type is MessageType.AUDIO:
                body["convert"] = True  # WAHA converte para OGG/Opus (nota de voz)

        resp = await self._request("POST", path, json=body, log_contact=to)
        return SendResult(provider=self.provider, message_id=self._extract_id(resp), raw=resp or {})

    @staticmethod
    def _extract_id(resp: Any) -> str | None:
        if not isinstance(resp, Mapping):
            return None
        mid = resp.get("id")
        if isinstance(mid, Mapping):  # WEBJS: {"_serialized": "...", "id": "..."}
            mid = mid.get("_serialized") or mid.get("id")
        if mid is None:
            mid = dig(resp, "key", "id")
        return str(mid) if mid else None

    async def download_media(self, message: IncomingMessage) -> bytes:
        media = message.media
        if media is None:
            raise GatewayError("WAHA: mensagem sem mídia")
        if media.data_base64:
            return base64.b64decode(media.data_base64.split(",", 1)[-1])
        if not media.url:
            raise GatewayError(
                "WAHA: mídia sem URL (verifique o download automático de mídias no WAHA)",
                tenant_id=message.tenant_id,
            )
        # A URL vem com o host configurado DENTRO do WAHA (às vezes interno, ex. localhost:3000).
        # Reescrevemos para o nosso WAHA_BASE_URL — e garantimos que a API key nunca vá
        # para um host de terceiros.
        parts = urlsplit(media.url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        return await self._request(
            "GET",
            parts.path,
            absolute_url=f"{self.base_url}{path}",
            expect_json=False,
            log_contact=message.contact_phone,
        )
