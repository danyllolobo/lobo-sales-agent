# ETAPA 6 — HUMANIZAÇÃO MÁXIMA DO AGENTE DE WHATSAPP: TRANSCRIÇÃO DE ÁUDIO ROBUSTA, MENSAGENS PICOTADAS, CONTEXTO DE RESPOSTA (QUOTED/REPLY), CONFIRMAÇÃO DE LEITURA (BLUE TICKS) E CONTROLE DE DEBOUNCE NA UI

> **Instrução para a IA na IDE:**  
> O Lobo AI Sales Agent já está em produção no Easypanel em `https://salesagent.loboads.top` com o painel visual e a trava de segurança funcionando perfeitamente.  
> O usuário testou em tempo real no WhatsApp e identificou 5 melhorias cruciais de **Humanização e Experiência Comercial**.  
> Sua missão nesta **ETAPA 6** é implementar e validar esses 5 pilares com novos testes automatizados e atualizar a interface administrativa.

---

## 1. PILAR 1: Diagnóstico e Correção Definitiva da Transcrição de Áudio (Áudio do WhatsApp)

### O Problema Identificado
Quando o usuário enviou um áudio de 22 segundos no WhatsApp (sessão WAHA), o agente respondeu:
> *"Não consegui ouvir o seu áudio por aqui no momento. Poderia escrever por mensagem de texto rapidinho?..."*

Isso ocorreu porque o `download_media` no WAHA ou o envio para o Gemini falhou silenciosamente e acionou o fallback de texto `[Áudio sem transcrição disponível]`.

### O Que Implementar em `src/gateways/waha.py` e `src/agent/transcription.py`
1. **Download de Mídia Resiliente no WAHA (`waha.py`):**
   - O payload do WAHA pode trazer mídia de três formas diferentes conforme a versão e engine:
     a) `media.data` ou `media.data_base64` (base64 direto no payload).
     b) `media.url` (URL de arquivo do WAHA, ex: `http://localhost:3000/api/files/...` ou `https://waha.loboads.top/api/files/...`).
     c) Sem URL direta no payload do webhook, exigindo download via endpoint de mensagem do WAHA:
        `GET /api/{session}/chats/{chatId}/messages/{messageId}/media` ou `GET /api/files/{filename}`.
   - **Correção:** Em `waha.py -> download_media()`, suporte os três formatos! Se não houver `media.url`, tente automaticamente o endpoint de mídia da mensagem pelo `message.id` e `session_id`. Se a URL interna contiver `localhost` ou IP interno da VPS, reescreva a base usando `self.base_url` (`https://waha.loboads.top`).
   - Adicione timeout de 15s e headers `{"X-Api-Key": self.api_key, "Accept": "*/*"}`.
2. **Normalização do Mimetype e Transcrição no Gemini (`transcription.py`):**
   - Áudios de WhatsApp (PTT / nota de voz) chegam com mimetype `audio/ogg; codecs=opus`.
   - Limpe o mimetype antes de passar para a API do Gemini: passe sempre `audio/ogg` (ou `audio/mp4` / `audio/mpeg`).
   - Use `api_version="v1beta"` na chamada do Gemini Multimodal (`gemini-3.8-flash`).
   - Se o Gemini falhar por qualquer motivo de formato, faça o fallback automático imediato para o OpenAI Whisper (`whisper-1`).
   - **Log detalhado:** Registre em log `INFO` quando o áudio for baixado (tamanho em bytes) e quando for transcrito com o texto extraído, para nunca mais falhar às cegas.

---

## 2. PILAR 2: Confirmação de Leitura (Blue Ticks / Visualizado) Antes de Digitar

### Comportamento Humano Real
Nenhum humano começa a digitar antes de abrir a conversa e visualizar a mensagem do cliente.
Atualmente, o sistema estava disparando o evento `composing` (digitando...) diretamente.

### O Que Implementar em `src/agent/graph.py` e nos Gateways:
1. Quando o lote de debounce fechar e o turno for iniciado:
   - **Passo 1 (Visualizar):** Chame `await gateway.mark_seen(session_id, contact, message_ids)`. O lead verá os **tiques azuis de lido** instantaneamente!
   - **Passo 2 (Pausa Humanizada):** Aguarde um breve delay de leitura proporcional ao tamanho das mensagens do lead (ex.: 500ms a 1000ms).
   - **Passo 3 (Digitando):** Em seguida, inicie o status `"composing"` (digitando...) via `gateway.set_typing(session_id, contact, True)`.

---

## 3. PILAR 3: Mensagens Picotadas (Envio Humanizado em Partes / Chunks)

### O Problema do "Textão"
O lead mandou um áudio ou fez uma pergunta, e a IA devolveu um bloco gigantesco com 5 parágrafos e lista de perguntas numeradas de uma vez só. No WhatsApp, isso parece um e-mail ou script de robô.

### O Que Implementar em `src/core/schemas.py` e `src/agent/graph.py`:
1. **Algoritmo de Divisão Inteligente de Mensagens (`split_human_chunks`):**
   - Quebre respostas longas em **2 a 4 partes naturais**:
     - Primeiro split por quebras de parágrafo duplas (`\n\n`) ou blocos lógicos.
     - Se um parágrafo contiver uma lista numerada ou bullet points, mantenha a lista coesa ou separe a introdução da lista.
     - Garanta que nenhum chunk fique gigante (máximo de 350 a 450 caracteres por chunk).
2. **Despacho com Intervalo Humanizado:**
   - Em vez de enviar 1 `sendText` com todo o conteúdo, despache em sequência:
     - Envia Parte 1.
     - Simula digitação proporcional ao tamanho da Parte 2 (ex.: 1.5 a 2.5 segundos de "composing").
     - Envia Parte 2.
     - Se houver Parte 3, repete a simulação de digitação e envia a Parte 3.
3. **Controle na Configuração (`TenantConfig` & Painel):**
   - Adicione o parâmetro `split_messages: bool = True` (padrão ativado) para permitir ligar/desligar esse comportamento por cliente ou no painel.

---

## 4. PILAR 4: Contexto de Mensagens Respondidas (Quoted Message / Reply-To)

### O Problema do Contexto Isolado
Muitas vezes o lead não escreve uma frase completa. O bot pergunta: *"Qual o tamanho do colchão que você procura (solteiro, casal, queen ou king)?"*.
O lead clica em **Responder (Reply / Citar)** naquela mensagem específica e manda apenas: *"Casal"*.
Se a IA receber apenas a palavra *"Casal"* isolada, ela pode perder o contexto ou achar que é uma mensagem solta.

### O Que Implementar:
1. **Captura do Texto Citado nos Webhooks:**
   - **Em `waha.py`:** Extraia o texto da mensagem respondida de `payload.get("replyTo", {}).get("body")` ou `payload.get("_data", {}).get("quotedMsg", {}).get("body")`.
   - **Em `astracalls.py`:** Extraia de `contextInfo.quotedMessage.conversation` ou `extendedTextMessage.text`.
   - Salve em `IncomingMessage.quoted_text: str | None`.
2. **Injeção de Contexto para a IA:**
   - No método `content_for_llm` de `IncomingMessage`:
     - Se `quoted_text` existir, formate como:
       ```
       [Lead respondeu citando a mensagem: "{quoted_text}"]
       {text}
       ```
   - Dessa forma, o Gemini 3.8 Flash entende instantaneamente que *"Casal"* é a resposta exata para a pergunta de tamanho do colchão!

---

## 5. PILAR 5: Controle Dinâmico do Tempo de Debounce no Painel Visual (UI)

### O Problema
O usuário achou a janela de silêncio de 4 segundos e o teto de 8/10 segundos muito rápidos. Ele quer poder ajustar no painel para 10s, 15s ou 20s conforme o perfil de cliente de cada nicho.

### O Que Implementar:
1. **No Backend (`src/config.py` e `src/tools/tenant_data.py`):**
   - Permita que `debounce_silence_seconds` varie de `1.0` a `30.0` segundos (atualmente estava limitado a 8.0).
   - Permita que `debounce_max_window_seconds` varie de `2.0` a `60.0` segundos.
2. **No Painel Visual (`static/admin/index.html` e `static/admin/app.js`):**
   - Na **Aba 1 (Segurança Sandbox)** e na **Aba 3 (Configurações do Cliente)**:
     - Adicione controles deslizantes (Sliders com exibição numérica em tempo real) ou campos numéricos para:
       - **Tempo de Silêncio do Debounce (segundos):** Slider de 2s a 30s (ex.: padrão 8s a 12s).
       - **Janela Máxima de Coleta (segundos):** Slider de 5s a 60s (ex.: padrão 20s).
       - **Chave de Picotar Mensagens Longas:** Toggle switch `[Ativar Envio Humanizado em Partes]`.
     - Ao clicar em **Salvar**, o backend grava as alterações no tenant correspondente e no `sandbox_config.json`, atualizando o comportamento imediatamente sem precisar reiniciar o servidor!

---

## 6. Testes Automatizados da ETAPA 6

Crie um novo arquivo de testes `tests/test_humanization.py` cobrindo:
1. Extração de `quoted_text` e injeção correta no `content_for_llm`.
2. O algoritmo `split_human_chunks` quebrando mensagens longas em 2 ou 3 partes preservando palavras e blocos lógicos.
3. O fluxo de `mark_seen` sendo chamado antes do envio e do typing.
4. Ajuste dos limites de validação de debounce até 30s / 60s em `src/config.py`.
5. Mock da transcrição com o mimetype `audio/ogg; codecs=opus`.

Execute a suíte completa garantindo que todos os testes passem (226 testes anteriores + novos testes) e que não haja avisos no `pyflakes`.
