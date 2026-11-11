# MASTER KICKOFF PROMPT — LOBO AI SALES AGENT (FASE 1: MVP DE ATENDIMENTO & CONVERSÃO)

> **Instrução para a IA que assumir esta IDE:**  
> Você é o Engenheiro de Software Sênior e Especialista em IA Agêntica responsável por construir o **Lobo AI Sales Agent**, uma plataforma open source/self-hosted de vendas e atendimento inteligente no WhatsApp para clientes da agência LoboAds.  
> Leia atentamente este documento e o arquivo `ARCHITECTURE.md` antes de escrever qualquer código. Siga as instruções à risca.

---

## 1. Contexto e Missão

A LoboAds atende diversos clientes em segmentos distintos:
- **Clínica de Psicologia (Vizzu Psicologia):** Recebe dezenas de leads de anúncios no Meta Ads procurando terapia e avaliação neuropsicológica, mas perde conversões por demora no atendimento e falta de triagem rápida. Usa o GoHighLevel (GHL) como CRM.
- **Lojas de Colchões (Companhia dos Sonhos / Vitória Prime):** Clientes chegam perguntando preço de colchão; o agente precisa descobrir a necessidade (tamanho, dores de coluna, firmeza) e direcionar para visita na loja física ou fechamento. Usa Chatwoot (ChatCRM).
- **Lojas de Piscinas (Splash Piscinas):** Lead pede orçamento; o agente precisa qualificar cidade, espaço disponível, analisar foto do terreno e agendar a visita técnica presencial do consultor. Usa Chatwoot (ChatCRM).

**O seu objetivo na FASE 1:**  
Construir o **núcleo agêntico funcional (MVP)** que:
1. Receba mensagens de WhatsApp via Webhook de provedores não-oficiais (**WAHA** ou **AstraCalls**).
2. Agrupe mensagens picadas do lead usando um **buffer inteligente com debounce de 4 a 8 segundos no Redis** (evitando responder a cada "oi", "bom dia", "quanto custa").
3. Transcreva áudios enviados pelo lead (Gemini Transcribe / Whisper).
4. Execute o **LangGraph State Machine** para conduzir a conversa (Triagem -> Descoberta -> Catálogo/Preço -> Agendamento -> Handoff).
5. **NUNCA alucine preços:** consulte ferramentas determinísticas (`get_realtime_price`, `search_catalog`, `check_availability`).
6. Transfira para atendente humano (**Handoff**) com anotação privada de resumo no CRM (Chatwoot ou GHL) e pause a IA instantaneamente quando um humano assumir.

---

## 2. Pilha Técnica Obrigatória

- **Linguagem:** Python 3.11+
- **Framework Web:** FastAPI (assíncrono)
- **Gerenciador de Dependências:** `requirements.txt` ou `pyproject.toml`
- **Validação:** Pydantic v2
- **Orquestrador de IA:** LangGraph + LangChain Core
- **Modelos de IA:** Google Gemini via `google-genai` ou `langchain-google-genai` (Modelo primário: `gemini-2.5-flash` / `gemini-3.8-flash`) + OpenAI Whisper como fallback de áudio
- **Buffer & Locks:** Redis (via `redis-py` assíncrono `redis.asyncio`)
- **Banco de Dados:** PostgreSQL + SQLAlchemy / Asyncpg (com suporte a pgvector para RAG futuro)
- **Configurações:** As chaves de API e URLs já estão disponíveis no arquivo `.env` na raiz do projeto.

---

## 3. Estrutura de Diretórios Recomendada

Organize o projeto rigorosamente nesta estrutura modular:

```
lobo-sales-agent/
├── .env                     # Já configurado com credenciais reais
├── .env.example             # Modelo público
├── ARCHITECTURE.md          # Especificação técnica completa
├── README.md                # Como rodar e testar
├── requirements.txt         # Dependências do projeto
├── main.py                  # Ponto de entrada FastAPI
├── src/
│   ├── config.py            # Carregamento tipado do .env via pydantic-settings
│   ├── core/
│   │   ├── exceptions.py    # Exceções customizadas
│   │   └── schemas.py       # Pydantic models (Mensagens, Leads, Tenants)
│   ├── buffer/
│   │   └── message_queue.py # Lógica de Debounce, Redis Buffer e Lock Atômico
│   ├── gateways/
│   │   ├── base.py          # Interface abstrata do conector de WhatsApp
│   │   ├── waha.py          # Implementação WAHA (sendText, sendMedia, typing, etc.)
│   │   └── astracalls.py    # Implementação AstraCalls
│   ├── crm/
│   │   ├── base.py          # Interface abstrata de CRM
│   │   ├── chatwoot.py      # Conector Chatwoot (tags, notas privadas, handoff)
│   │   └── ghl.py           # Conector GoHighLevel API v2
│   ├── tools/
│   │   ├── catalog.py       # Ferramentas determinísticas de preços e catálogo
│   │   ├── calendar.py      # Ferramentas de verificação e criação de agendamento
│   │   └── handoff.py       # Ferramenta para acionar transferência humana
│   ├── agent/
│   │   ├── state.py         # Definição do AgentState do LangGraph
│   │   ├── prompts.py       # System prompts e playbooks por segmento
│   │   ├── nodes.py         # Nós do grafo (triagem, descoberta, catálogo, etc.)
│   │   ├── graph.py         # Montagem e compilação do grafo LangGraph
│   │   └── jev_guard.py     # Integração opcional com JEV (System One)
│   └── database/
│       ├── connection.py    # Sessão assíncrona do Postgres
│       └── models.py        # Modelos ORM (Tenant, Conversation, MessageLog)
└── tests/
    ├── test_debounce.py     # Teste unitário do buffer de mensagens
    ├── test_tools.py        # Teste unitário das ferramentas determinísticas
    └── test_simulation.py   # Simulação E2E de uma conversa completa no terminal
```

---

## 4. Roteiro de Execução do Passo 1 (MVP)

Execute a implementação em **4 Etapas Sequenciais**:

### ETAPA 1: Setup do Ambiente e Schemas Base
1. Crie o arquivo `requirements.txt` com:
   `fastapi`, `uvicorn[standard]`, `pydantic`, `pydantic-settings`, `redis`, `httpx`, `langgraph`, `langchain-core`, `langchain-google-genai`, `openai`, `sqlalchemy`, `asyncpg`, `pytest`, `pytest-asyncio`.
2. Implemente `src/config.py` para ler o `.env` com validação Pydantic.
3. Crie `src/core/schemas.py` com os modelos unificados:
   - `IncomingMessage`: representação neutra de mensagem (texto, áudio ou imagem) vinda de qualquer provedor.
   - `OutgoingMessage`: resposta a ser despachada.
   - `LeadProfile`: contexto do cliente (nome, telefone, tenant, tags, status).

### ETAPA 2: Buffer de Debounce (Redis) e Conectores de WhatsApp
1. Crie `src/buffer/message_queue.py`:
   - Método `push_message(tenant_id, session_id, contact_phone, message)`: armazena no Redis com expiração configurável (ex: 4s).
   - Se novas mensagens chegarem do mesmo contato antes de expirar, adiciona ao lote e renova a janela de silêncio (com trava de teto máximo de 10s).
   - Ao disparar o flush, devolve as mensagens agrupadas para o motor de IA.
2. Crie `src/gateways/waha.py` e `src/gateways/astracalls.py`:
   - Normalizar o payload bruto recebido no webhook para `IncomingMessage`.
   - Implementar método para enviar o status `"digitando..."` (`composing`) antes de a IA responder.
   - Implementar método de envio de resposta final.

### ETAPA 3: Ferramentas Determinísticas e Conectores de CRM
1. Crie `src/tools/catalog.py`:
   - Função mock/banco `get_realtime_price(tenant_id, item_code)` e `search_catalog(tenant_id, query)`.
   - Garantir que preços sejam estritamente determinísticos.
2. Crie `src/tools/calendar.py`:
   - Função `check_available_slots(tenant_id, date)` e `create_booking(tenant_id, phone, slot)`.
3. Crie `src/crm/chatwoot.py` e `src/crm/ghl.py`:
   - Criação de anotação interna (`private note`) para notificar o atendente humano.
   - Aplicação de tags (ex: `qualificado`, `visita_agendada`, `atendimento_humano`).

### ETAPA 4: O Grafo LangGraph e a Simulação Completa
1. Crie `src/agent/state.py`:
   - `AgentState` com lista de mensagens, dados do lead, intenção, ferramentas executadas e flag `human_handoff_requested`.
2. Crie `src/agent/nodes.py` e `src/agent/graph.py`:
   - Conecte o modelo Gemini com o binding das ferramentas determinísticas.
   - Adicione nós de controle:
     - Nó 1: Validação de Handoff (se atendente humano já assumiu, retorna silêncio).
     - Nó 2: Raciocínio comercial e chamada de ferramentas (Catálogo, Preço, Agendamento).
     - Nó 3: Despacho da resposta.
3. Crie `tests/test_simulation.py`:
   - Um script CLI que simule um lead mandando 3 mensagens rápidas:
     1. *"Olá, boa tarde"*
     2. *"Queria ver um colchão de casal com molas ensacadas"*
     3. *"Vocês entregam em Belém?"*
   - O teste deve demonstrar o debounce agrupando as mensagens em 1 único turno, a IA chamando a ferramenta de catálogo e respondendo com preço e prazo reais.

---

## 5. Regras Invioláveis do Projeto

1. **PROIBIDO INVENTAR PREÇOS OU DESCONTOS:** Se a ferramenta de catálogo ou preço retornar nulo ou não tiver o dado, o agente deve dizer que precisa confirmar com a equipe humana e registrar o motivo.
2. **ZERO POLUIÇÃO DE LOGS COM DADOS PESSOAIS:** Anonimize telefones e dados sensíveis em logs públicos (LGPD compliance).
3. **ISOLAMENTO MULTI-TENANT:** Todas as funções e consultas a banco/ferramentas exigem `tenant_id`. Nunca consulte um dado sem isolar o cliente da agência.
4. **FALLBACK SEGURO:** Se qualquer API externa falhar (Redis, Gemini, CRM), capture a exceção e direcione o contato para o atendente humano com mensagem cordial, sem quebrar a execução do servidor.

---

**Comando de Início:**  
Você pode começar criando o `requirements.txt`, o `src/config.py` e a estrutura de pastas da **ETAPA 1**. Avance passo a passo validando cada componente com testes unitários.
