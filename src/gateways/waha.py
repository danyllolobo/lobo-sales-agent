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
import json
import logging
from collections.abc import Mapping
from typing import Any, ClassVar
from urllib.parse import quote, urlsplit

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
        quoted_id, quoted_text = self._extract_quoted(p, data)
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
                quoted_message_id=quoted_id,
                quoted_text=quoted_text,
                from_me=from_me,
                timestamp=epoch_to_datetime(p.get("timestamp")) or utcnow(),
                raw_payload=dict(payload),
            )
        except ValidationError:
            return result.ignore("invalid_payload", event, msg_id)

        result.messages.append(message)
        return result

    @staticmethod
    def _extract_quoted(p: Mapping[str, Any], data: Mapping[str, Any]) -> tuple[str | None, str | None]:
        """Extrai o ID e o texto da mensagem citada (replyTo / _data.quotedMsg / contextInfo)."""
        reply_to = p.get("replyTo") if isinstance(p.get("replyTo"), Mapping) else {}
        quoted_msg = data.get("quotedMsg") if isinstance(data.get("quotedMsg"), Mapping) else {}
        ctx = (
            dig(data, "message", "extendedTextMessage", "contextInfo")
            or dig(data, "contextInfo")
            or {}
        )
        ctx = ctx if isinstance(ctx, Mapping) else {}
        proto_quoted = ctx.get("quotedMessage") if isinstance(ctx.get("quotedMessage"), Mapping) else {}

        quoted_id_raw = (
            reply_to.get("id")
            or quoted_msg.get("id")
            or data.get("quotedStanzaID")
            or ctx.get("stanzaId")
        )
        if isinstance(quoted_id_raw, Mapping):
            quoted_id_raw = quoted_id_raw.get("_serialized") or quoted_id_raw.get("id")
        quoted_id = str(quoted_id_raw).strip() if quoted_id_raw else None

        candidates = (
            reply_to.get("body"),
            reply_to.get("text"),
            reply_to.get("caption"),
            quoted_msg.get("body"),
            quoted_msg.get("caption"),
            quoted_msg.get("text"),
            quoted_msg.get("conversation"),
            proto_quoted.get("conversation"),
            dig(proto_quoted, "extendedTextMessage", "text"),
            dig(proto_quoted, "imageMessage", "caption"),
            dig(proto_quoted, "videoMessage", "caption"),
        )
        quoted_text = next(
            (str(c).strip() for c in candidates if isinstance(c, str) and c.strip()),
            None,
        )
        return quoted_id, quoted_text

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
            raw_b64 = info.get("data_base64") or info.get("data")
            data_b64 = str(raw_b64).strip() if isinstance(raw_b64, str) and str(raw_b64).strip() else None
            media = MediaAttachment(
                url=info.get("url") or None,
                data_base64=data_b64,
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

    async def react(self, session_id: str, chat_id: str, message_id: str, emoji: str) -> None:
        await self._request(
            "POST",
            "/api/sendReaction",
            json={"session": session_id, "chatId": self.chat_id_for(chat_id), "messageId": message_id, "text": emoji},
            log_contact=chat_id,
        )

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
            raise GatewayError("WAHA: mensagem sem mídia", tenant_id=message.tenant_id)
        if media.data_base64:
            return base64.b64decode(media.data_base64.split(",", 1)[-1])

        raw_b64 = dig(message.raw_payload, "payload", "media", "data_base64") or dig(
            message.raw_payload, "payload", "media", "data"
        )
        if isinstance(raw_b64, str) and raw_b64.strip():
            return base64.b64decode(raw_b64.strip().split(",", 1)[-1])

        # Candidatos de download:
        # 1) media.url reescrita para self.base_url (reescreve localhost / IP interno da VPS)
        # 2) /api/files/{filename} (se filename estiver presente)
        # 3) /api/{session}/chats/{chatId}/messages/{messageId}/media
        # 4) /api/{session}/chats/{chatId}/messages/{messageId}?downloadMedia=true
        candidates: list[tuple[str, str]] = []
        seen_urls: set[str] = set()

        def _add(path_label: str, full_url: str) -> None:
            if full_url not in seen_urls:
                seen_urls.add(full_url)
                candidates.append((path_label, full_url))

        if media.url:
            parts = urlsplit(media.url)
            host = (parts.hostname or "").lower()
            path_q = parts.path + (f"?{parts.query}" if parts.query else "")
            if host in ("localhost", "127.0.0.1", "::1"):
                # URL interna do container WAHA → reescreve para a base_url pública
                _add(parts.path, f"{self.base_url}{path_q}")
            else:
                # URL externa (ex.: MinIO S3 do Easypanel / CDN) → baixa diretamente
                _add("media.url (direto)", media.url)
                _add(parts.path, f"{self.base_url}{path_q}")

        if media.filename:
            clean_fn = media.filename.lstrip("/")
            fn_path = f"/api/files/{quote(clean_fn, safe='/')}"
            _add(fn_path, f"{self.base_url}{fn_path}")

        session = quote(message.session_id or self.default_session, safe="")
        chat_id = quote(self.chat_id_for(message.chat_id or message.contact_phone), safe="@.")
        msg_id = quote(media.provider_media_id or message.message_id, safe="@._-")
        if msg_id:
            msg_media_path = f"/api/{session}/chats/{chat_id}/messages/{msg_id}/media"
            _add(msg_media_path, f"{self.base_url}{msg_media_path}")
            msg_dl_path = f"/api/{session}/chats/{chat_id}/messages/{msg_id}"
            _add(msg_dl_path, f"{self.base_url}{msg_dl_path}?downloadMedia=true")

        if not candidates:
            raise GatewayError(
                "WAHA: mídia sem URL ou message_id para download",
                tenant_id=message.tenant_id,
            )

        last_err: GatewayError | None = None
        for path_label, full_url in candidates:
            try:
                raw_bytes: bytes = await self._request(
                    "GET",
                    path_label,
                    absolute_url=full_url,
                    headers={"Accept": "*/*"},
                    expect_json=False,
                    log_contact=message.contact_phone,
                )
                resolved = await self._unwrap_media_response(raw_bytes, log_contact=message.contact_phone)
                if resolved:
                    return resolved
            except GatewayError as exc:
                last_err = exc
                logger.warning("WAHA: falha ao baixar mídia em %s (%s), tentando fallback", path_label, exc.message)

        if last_err is not None:
            raise last_err
        raise GatewayError("WAHA: não foi possível baixar a mídia", tenant_id=message.tenant_id)

    async def _unwrap_media_response(self, raw_bytes: bytes, *, log_contact: str | None) -> bytes:
        """Se o endpoint retornar JSON com `media.data`/`media.url` em vez de binário puro, extrai os bytes."""
        stripped = raw_bytes.lstrip()
        if stripped.startswith(b"{"):
            try:
                obj = json.loads(stripped.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return raw_bytes
            if isinstance(obj, Mapping):
                b64 = (
                    obj.get("data_base64")
                    or obj.get("data")
                    or dig(obj, "media", "data_base64")
                    or dig(obj, "media", "data")
                )
                if isinstance(b64, str) and b64.strip():
                    return base64.b64decode(b64.strip().split(",", 1)[-1])
                nested_url = obj.get("url") or dig(obj, "media", "url")
                if isinstance(nested_url, str) and nested_url.strip():
                    parts = urlsplit(nested_url.strip())
                    path_q = parts.path + (f"?{parts.query}" if parts.query else "")
                    return await self._request(
                        "GET",
                        parts.path,
                        absolute_url=f"{self.base_url}{path_q}",
                        headers={"Accept": "*/*"},
                        expect_json=False,
                        log_contact=log_contact,
                    )
        return raw_bytes
