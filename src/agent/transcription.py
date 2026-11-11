"""Transcrição de áudios do WhatsApp (Gemini Multimodal primário + OpenAI Whisper fallback).

Cumpre o requisito 3 do MVP (`MASTER_KICKOFF_PROMPT.md` §1 e §2):
- Obtém os bytes do áudio a partir de `data_base64` ou via `gateway.download_media(msg)`.
- Tenta transcrever via Google Gemini (`gemini_transcribe_model`).
- Em caso de falha ou indisponibilidade do Gemini, faz fallback automático para
  OpenAI Whisper (`whisper-1`).
- Nunca vaza PII nos logs (usa `msg.log_context()`).
"""

from __future__ import annotations

import base64
import io
import logging
from collections.abc import Awaitable, Callable

from langchain_core.messages import HumanMessage

from src.config import Settings
from src.core.exceptions import GatewayError, TranscriptionError
from src.core.schemas import IncomingMessage, MessageBatch
from src.gateways.base import WhatsAppGateway

logger = logging.getLogger("lobo.transcription")

TranscribeFn = Callable[[bytes, str], Awaitable[str]]

_TRANSCRIPTION_PROMPT = (
    "Transcreva fielmente este áudio em português brasileiro. "
    "Retorne APENAS o texto falado pelo cliente, sem comentários adicionais."
)


def _decode_base64_media(raw: str) -> bytes:
    payload = raw.strip()
    if "," in payload and payload.lower().startswith("data:"):
        _, _, payload = payload.partition(",")
    try:
        return base64.b64decode(payload)
    except (ValueError, TypeError) as exc:
        raise TranscriptionError("base64 de áudio inválido") from exc


def _extension_for_mime(mimetype: str) -> str:
    mime = mimetype.lower().split(";")[0].strip()
    mapping = {
        "audio/ogg": "ogg",
        "audio/opus": "ogg",
        "audio/mpeg": "mp3",
        "audio/mp3": "mp3",
        "audio/mp4": "m4a",
        "audio/x-m4a": "m4a",
        "audio/wav": "wav",
        "audio/webm": "webm",
    }
    return mapping.get(mime, "ogg")


class AudioTranscriber:
    """Serviço de transcrição de voz com fallback Gemini -> OpenAI Whisper."""

    def __init__(
        self,
        settings: Settings,
        *,
        transcribe_fn: TranscribeFn | None = None,
    ) -> None:
        self._settings = settings
        self._custom_fn = transcribe_fn

    async def transcribe_message(
        self,
        message: IncomingMessage,
        *,
        gateway: WhatsAppGateway | None = None,
    ) -> IncomingMessage:
        """Retorna cópia de `message` com `transcription` preenchido."""
        if not message.needs_transcription:
            return message

        audio_bytes, mimetype = await self._load_audio(message, gateway=gateway)
        if self._custom_fn is not None:
            text = (await self._custom_fn(audio_bytes, mimetype)).strip()
            if not text:
                raise TranscriptionError("transcrição retornou vazia", tenant_id=message.tenant_id)
            return message.with_transcription(text)

        errors: list[str] = []
        if self._settings.gemini_enabled:
            try:
                text = await self._transcribe_with_gemini(audio_bytes, mimetype)
                if text:
                    logger.info("Áudio transcrito via Gemini %s", message.log_context())
                    return message.with_transcription(text)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"gemini:{type(exc).__name__}")
                logger.warning("Falha no Gemini Transcribe (%s), tentando fallback Whisper", type(exc).__name__)

        if self._settings.openai_enabled:
            try:
                text = await self._transcribe_with_whisper(audio_bytes, mimetype)
                if text:
                    logger.info("Áudio transcrito via Whisper %s", message.log_context())
                    return message.with_transcription(text)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"whisper:{type(exc).__name__}")
                logger.warning("Falha no Whisper STT (%s)", type(exc).__name__)

        raise TranscriptionError(
            f"Não foi possível transcrever o áudio ({', '.join(errors) or 'nenhum provedor STT configurado'})",
            tenant_id=message.tenant_id,
            context=message.log_context(),
        )

    async def transcribe_batch(
        self,
        batch: MessageBatch,
        *,
        gateway: WhatsAppGateway | None = None,
    ) -> MessageBatch:
        """Transcreve todos os áudios pendentes do lote. Não levanta exceção se falhar."""
        if not batch.needs_transcription:
            return batch

        updated: list[IncomingMessage] = []
        for msg in batch.messages:
            if msg.needs_transcription:
                try:
                    updated.append(await self.transcribe_message(msg, gateway=gateway))
                except TranscriptionError as exc:
                    logger.error("Falha ao transcrever áudio no lote: %s", exc.message)
                    updated.append(msg)
            else:
                updated.append(msg)
        return batch.model_copy(update={"messages": updated})

    async def _load_audio(
        self,
        message: IncomingMessage,
        *,
        gateway: WhatsAppGateway | None,
    ) -> tuple[bytes, str]:
        media = message.media
        if media is None:
            raise TranscriptionError("mensagem de áudio sem objeto media", tenant_id=message.tenant_id)
        mimetype = (media.mimetype or "audio/ogg").split(";")[0].strip()

        if media.data_base64:
            return _decode_base64_media(media.data_base64), mimetype

        if gateway is not None:
            try:
                audio_bytes = await gateway.download_media(message)
                return audio_bytes, mimetype
            except GatewayError as exc:
                raise TranscriptionError(
                    "falha ao baixar mídia de áudio no gateway",
                    tenant_id=message.tenant_id,
                    context=message.log_context(),
                ) from exc

        raise TranscriptionError(
            "áudio sem base64 embutido e sem gateway disponível para download",
            tenant_id=message.tenant_id,
            context=message.log_context(),
        )

    async def _transcribe_with_gemini(self, audio_bytes: bytes, mimetype: str) -> str:
        from langchain_google_genai import ChatGoogleGenerativeAI

        assert self._settings.google_api_key is not None
        llm = ChatGoogleGenerativeAI(
            model=self._settings.gemini_transcribe_model,
            google_api_key=self._settings.google_api_key.get_secret_value(),
            api_version="v1beta",
            temperature=0.0,
        )
        b64 = base64.b64encode(audio_bytes).decode("ascii")
        msg = HumanMessage(
            content=[
                {"type": "text", "text": _TRANSCRIPTION_PROMPT},
                {"type": "media", "mime_type": mimetype, "data": b64},
            ]
        )
        resp = await llm.ainvoke([msg])
        content = resp.content if isinstance(resp.content, str) else str(resp.content)
        return content.strip()

    async def _transcribe_with_whisper(self, audio_bytes: bytes, mimetype: str) -> str:
        from openai import AsyncOpenAI

        assert self._settings.openai_api_key is not None
        client = AsyncOpenAI(api_key=self._settings.openai_api_key.get_secret_value())
        ext = _extension_for_mime(mimetype)
        buf = io.BytesIO(audio_bytes)
        buf.name = f"audio.{ext}"
        transcript = await client.audio.transcriptions.create(
            model="whisper-1",
            file=buf,
            language="pt",
        )
        return (transcript.text or "").strip()


__all__ = ["AudioTranscriber", "TranscribeFn"]

