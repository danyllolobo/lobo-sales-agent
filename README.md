# Lobo AI Sales Agent 🐺🤖

Motor Agêntico Multi-tenant para Vendas e Atendimento Autônomo no WhatsApp (LoboAds).

---

## 🚀 Como Iniciar o Desenvolvimento no Antigravity IDE

1. No **Antigravity IDE**, vá em:
   `File` -> `Open Folder...` (ou `Abrir Pasta...`)
2. Selecione esta pasta:
   `C:\Users\Micro\Documents\SITE COM TRAE\lobo-sales-agent`
3. Abra a janela de chat com o Agente na nova IDE.
4. Envie o seguinte comando inicial:
   > *"Olá! Por favor, leia os arquivos `MASTER_KICKOFF_PROMPT.md` e `ARCHITECTURE.md` na raiz do projeto e inicie a execução da ETAPA 1 (Setup do ambiente, requirements.txt, schemas e configuração base)."*

---

## 📁 Estrutura deste Projeto

- **`MASTER_KICKOFF_PROMPT.md`**: Prompt mestre com a especificação técnica detalhada, regras de negócio e roteiro de implementação.
- **`ARCHITECTURE.md`**: Diagrama de fluxo, arquitetura do LangGraph, buffer de mensagens no Redis e contratos de ferramentas determinísticas.
- **`.env`**: Já pré-configurado com as chaves reais de APIs (Google Gemini, OpenAI, WAHA, AstraCalls, ChatCRM, GHL e TypeSafe/JEV).
- **`.env.example`**: Modelo público sem valores reais.

---

## ⚙️ Setup Local (ETAPA 1)

> ⚠️ **Windows:** o `python` do PATH nesta máquina aponta para o Python embutido do Inkscape.
> Use o launcher oficial `py` para criar o ambiente virtual.

```powershell
py -3.14 -m venv .venv            # ou qualquer Python 3.11+
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest                        # suíte completa de testes
.\.venv\Scripts\python.exe tests/test_simulation.py         # simulação CLI determinística (3 msgs -> 1 turno)
.\.venv\Scripts\python.exe tests/test_simulation.py --live  # simulação CLI usando Gemini real do .env
.\.venv\Scripts\python.exe -m uvicorn main:app --reload     # GET http://localhost:8000/health
```

### Módulos implementados (MVP Completo — Etapas 1 a 4)
| Módulo | Conteúdo |
| :--- | :--- |
| `src/config.py` | `Settings` tipado (pydantic-settings), segredos como `SecretStr`, `async_database_url`, `safe_summary()` |
| `src/core/schemas.py` | `IncomingMessage`, `OutgoingMessage`, `LeadProfile`, `TenantConfig`, enums e utilitários LGPD (`mask_phone`) |
| `src/core/exceptions.py` | Hierarquia `LoboError` com flag `handoff_recommended` (fallback seguro) |
| `main.py` | App FastAPI com `/health`, `POST /webhook/{tenant_id}/{provider}`, detecção de `from_me` humano vs eco da IA e disparo do `SalesAgentRunner` |
| `src/buffer/message_queue.py` | `MessageBuffer`: debounce no Redis (scripts Lua atômicos), teto máximo, trava distribuída com renovação, deduplicação por `message_id` |
| `src/buffer/redis_client.py` | Cliente Redis real via `REDIS_URL` com fallback para fakeredis fora de produção (`REDIS_BACKEND`) |
| `src/gateways/` | `WahaGateway` e `AstraCallsGateway`: normalização de webhook, HMAC, "digitando...", envio de texto/mídia, leitura e download de mídia |
| `src/tools/catalog.py` | `CatalogService`: `search_catalog`, `get_realtime_price`, `get_delivery_info`, `get_business_info` — preços em `Decimal`, já formatados, com validade |
| `src/tools/calendar.py` | `CalendarService`: `check_available_slots` (sempre 2 sugestões) e `create_booking` atômico (sem overbooking) |
| `src/tools/handoff.py` | `HandoffService` + `AIPauseRegistry`: pausa a IA (Redis), nota privada + tag no CRM, mensagem cordial (CVV 188 em emergência clínica) |
| `src/tools/tenant_data.py` | Dados por tenant em `data/tenants/<tenant_id>.json` (catálogo, entrega, agenda, CRM) |
| `src/crm/` | `ChatwootConnector` e `GHLConnector`: contato, nota privada, tags (merge), detecção de humano ativo, escalonamento |
| `src/agent/state.py` | `AgentState` (`TypedDict` LangGraph), `ConversationStage`, `AgentIntent` e `TurnResult` |
| `src/agent/prompts.py` | System prompts e playbooks por segmento (`mattress`, `pools`, `psychology`) com governança anti-alucinação |
| `src/agent/jev_guard.py` | `JEVGuard` (System One: emergência clínica, pedido de humano, barganha de desconto, roteamento) + `verify_no_unbacked_prices` |
| `src/agent/nodes.py` | Nós do grafo (`node_triage`, `node_discovery`, `node_offer_catalog`, `node_booking_conversion`, `node_handoff`, `node_dispatch`) e `build_tenant_tools` (isolamento por closure) |
| `src/agent/graph.py` | `build_sales_graph` (`StateGraph` compilado), `LeadConversationStore` e `SalesAgentRunner` |
| `src/agent/transcription.py` | `AudioTranscriber`: transcrição de áudios via Gemini Multimodal com fallback automático para OpenAI Whisper |
| `src/database/` | `DatabaseManager` (SQLAlchemy 2.0 async) e modelos ORM (`Tenant`, `Conversation`, `MessageLog`) |

### Dados dos tenants (`data/tenants/*.json`)
> ⚠️ Os arquivos atuais são **DEMONSTRAÇÃO** (`"demo_data": true`, preços fictícios) e são **bloqueados automaticamente em produção**.
> Antes de ir ao ar, substitua pelos catálogos, preços, áreas de entrega e horários reais de cada cliente e remova `demo_data`.

- Preços: strings decimais (`"2899.00"`), `amount: null` = "sob consulta" (a IA transfere para humano).
- `price_max_age_days`: preço mais antigo que isso é tratado como indisponível (a IA não informa).
- Chatwoot: `account_id`/`inbox_id` por tenant; o token fica no `.env`. Se o inbox tiver **auto-atribuição**, use `"assignee_means_human": false`.
- GHL: o tenant referencia os campos do `.env` (`ghl_vizzu_api_token`, `ghl_vizzu_location_id`), nunca o segredo em si.

### Configurando o webhook nos provedores
- **WAHA:** `https://SEU_HOST/webhook/<tenant_id>/waha` com eventos `message` (e opcionalmente `message.any`, para detectar respostas do atendente).
- **AstraCalls:** `https://SEU_HOST/webhook/<tenant_id>/astracalls` (`POST /api/sessions/{sid}/webhook`, `events: ["message"]`, `ignoreGroups: true`).
- Recomendado: defina um segredo HMAC no provedor e o mesmo valor em `WAHA_WEBHOOK_HMAC_KEY` / `ASTRACALLS_WEBHOOK_SECRET`.

> ⚠️ Com `REDIS_BACKEND=auto` sem Redis disponível, o buffer roda em memória: use **um único worker** do uvicorn. Na VPS/Easypanel use `REDIS_BACKEND=redis`.
