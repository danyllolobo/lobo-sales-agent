# ETAPA 5 — PAINEL VISUAL ADMINISTRATIVO (LIGHT/DARK), PLAYGROUND AO VIVO E TRAVA DE SEGURANÇA (SANDBOX)

> **Instrução para a IA:**  
> As Etapas 1 a 4 do MVP já estão prontas, com 192 testes passando, e o serviço já está rodando no Easypanel em `https://salesagent.loboads.top`.  
> Agora sua missão na **ETAPA 5** é implementar duas frentes essenciais:
> 1. **Trava de Segurança Obrigatória para Testes em Produção/Horário Comercial (Sandbox Guard) + Tenant `loboads`**.
> 2. **Painel Visual Administrativo & Playground Completo (`GET /`)**: extremamente bonito, leve, ultra rápido, 100% personalizável na tela, com Tema Claro e Escuro, usando as logos oficiais da LoboAds já salvas no projeto, e com feedback imediato de Sucesso ou Erro em todas as operações.

---

## 1. Logos Oficiais da LoboAds (Já salvas no projeto)

As duas versões da logo oficial da **LoboAds Marketing Digital** já estão salvas em:
- `static/assets/loboads-logo-dark.png` -> Logo com letras brancas e lobo cinza (usar quando o **Tema Escuro / Dark Mode** estiver ativo).
- `static/assets/loboads-logo-light.png` -> Logo com letras azul-marinho e lobo cinza (usar quando o **Tema Claro / Light Mode** estiver ativo).

Monte a pasta `/static` no FastAPI (`app.mount("/static", StaticFiles(directory=...), name="static")`). O `Dockerfile` já foi atualizado com `COPY static ./static`.

---

## 2. AÇÃO 1: Trava de Segurança Obrigatória (Sandbox Guard) + Tenant `loboads`

### 2.1 Por que essa trava é crítica
O usuário vai conectar o webhook no número de WhatsApp real dele (sessão `Danyllo` no WAHA ou AstraCalls) em pleno horário comercial. O agente **NÃO PODE** sair respondendo clientes reais ou contatos pessoais que mandarem mensagem normal!

### 2.2 Como o `SandboxGuard` (`src/core/sandbox.py`) deve funcionar
1. **Estado da Trava (`SandboxConfig`):**
   - Sempre que `settings.environment != "production"` **OU** `sandbox.enabled == True` (padrão: **`True`**, ativado por segurança!), o filtro de Sandbox está ativo.
   - Armazene a configuração do Sandbox no Redis (e em arquivo `data/sandbox_config.json` para persistir) contendo:
     - `enabled: bool = True` (Trava de segurança ativa).
     - `allowed_phones: list[str] = []` (Lista de telefones na whitelist que a IA pode responder livremente sem precisar de palavra-chave).
     - `trigger_keywords: list[str] = ["/lobo-sales-agent", "/lobo"]` (Palavras-chave que destravam uma sessão temporária de teste para quem enviar).
     - `session_ttl_minutes: int = 30` (Por quantos minutos a conversa daquele contato fica liberada após enviar `/lobo` ou `/lobo-sales-agent`).
     - `stop_keywords: list[str] = ["/parar", "/reset"]` (Comandos que encerram imediatamente a sessão de teste daquele número e limpam o histórico/pausa da conversa para permitir testar do zero).
2. **Regras de Avaliação no Webhook (`POST /webhook/{tenant_id}/{provider}`):**
   - Quando chega uma mensagem de entrada (`from_me=False`):
     - Se o texto começar com ou for igual a um comando de parada (`/parar` ou `/reset`):
       - Remove a sessão temporária de teste do Redis, limpa o histórico e a pausa daquele contato no `LeadConversationStore` / `AIPauseRegistry`, envia uma mensagem curta confirmando (`"🛑 Sessão de teste do Lobo AI Sales Agent encerrada e memória resetada para este número."`) e retorna `{"status": "sandbox_reset"}`.
     - Se `sandbox.enabled` for `True`:
       - Verifica se o `contact_phone` está em `allowed_phones` **OU** se já possui uma sessão temporária ativa no Redis (`lobo:sandbox:session:{tenant_id}:{phone}`) **OU** se o texto da mensagem contém alguma `trigger_keyword` (`/lobo-sales-agent` ou `/lobo`, case-insensitive).
       - **Se NÃO cumprir nenhuma das 3 condições:** ignora a mensagem silenciosamente! Retorna HTTP 200 com `{"status": "ignored", "reason": "sandbox_locked"}` e **não coloca no buffer nem responde nada no WhatsApp**.
       - **Se contiver a `trigger_keyword`:** ativa/renova a sessão temporária no Redis por `session_ttl_minutes` (30 min) e **remove a palavra-chave da mensagem** antes de enviá-la ao buffer (ex.: `"/lobo-sales-agent Quero saber o preço do colchão"` vira `"Quero saber o preço do colchão"`; se a pessoa enviou apenas `"/lobo"` ou `"/lobo-sales-agent"` sozinho, substitua o texto por `"Olá, gostaria de atendimento"` para a IA iniciar a recepção normalmente!).

### 2.3 Novo Tenant `data/tenants/loboads.json`
Crie também o arquivo `data/tenants/loboads.json` para a própria **Agência LoboAds Marketing Digital**:
- `tenant_id`: `"loboads"`, `name`: `"LoboAds Marketing Digital"`, `segment`: `"generic"`, `timezone`: `"America/Belem"`.
- `whatsapp_provider`: `"waha"`, `whatsapp_session`: `"Danyllo"`.
- `crm`: Chatwoot (`account_id: 4`, `assignee_means_human: false`).
- Catálogo de demonstração/serviços da LoboAds:
  1. `LB-TRAFEGO-PRO` — Gestão de Tráfego Pago (Meta Ads & Google Ads)
  2. `LB-AI-ESSENCIAL` — Lobo AI Sales Agent (Plano Atendimento Essencial)
  3. `LB-AI-PRO` — Lobo AI Sales Agent (Plano Comercial Pro com CRM & Agenda)
- Agenda: `"diagnostico_comercial"` (Reunião de Diagnóstico Estratégico com Danyllo Lobo - 30 min).

---

## 3. AÇÃO 2: Painel Visual Administrativo & Playground (`GET /` e API `/api/admin/...`)

Construa uma interface web **extremamente bonita, moderna, leve e ultra rápida** servida diretamente pelo FastAPI em `GET /` (e `/admin`), sem exigir compilação pesada de Node.js (use HTML5 + CSS moderno com variáveis de tema + JavaScript Vanilla/Alpine modular em `static/`).

### 3.1 Requisitos Visuais e de UX
1. **Temas Claro e Escuro (Light / Dark Mode):**
   - Alternador suave no cabeçalho (Sol ☀️ / Lua 🌙), persistido no `localStorage`.
   - No **Dark Mode**, exibe `static/assets/loboads-logo-dark.png` (fundo escuro sofisticado estilo `#0B0F19` / `#111827`, bordas sutis, acentos em azul/ciano e verde esmeralda).
   - No **Light Mode**, exibe `static/assets/loboads-logo-light.png` (fundo claro limpo `#F8FAFC` / `#FFFFFF`, tipografia nítida e alto contraste).
2. **Feedback Visual Imediato (Status de Sucesso 🟢 ou Erro 🔴):**
   - Toda ação (salvar produto, alterar debounce, adicionar telefone na whitelist, testar integração, simular conversa, reativar IA) deve exibir um **Toast/Banner claro de Sucesso (verde) ou Erro (vermelho)** com o tempo de resposta em `ms` e a mensagem detalhada.

### 3.2 As 5 Telas / Abas do Painel

#### ABA 1: 🛡️ Segurança Sandbox & Status das Conexões
- **Card Principal — Trava de Segurança Sandbox:**
  - Switch grande e claro: **`🔒 Modo Sandbox Seguro (Ativo)`** vs. **`🟢 Modo Produção Aberto (Atende Todos)`**.
  - Editor de **Palavras-Chave de Ativação** (ex.: `/lobo-sales-agent`, `/lobo`) e tempo de sessão (30 min).
  - Gerenciador de **Telefones Autorizados (Whitelist)**: campo para digitar um número (ex.: `5591988887777`) e botão **"+ Liberar Meu Número"**, com lista de números liberados e botão de remover.
  - Lista de **Sessões de Teste Ativas no Momento** (contatos que mandaram `/lobo`), com botão **"Encerrar / Resetar"**.
- **Grid de Status e Diagnóstico das 6 Integrações:**
  - Cards para **Gemini (`gemini-3.8-flash`)**, **OpenAI Whisper**, **WAHA**, **AstraCalls**, **ChatCRM (Chatwoot)** e **GoHighLevel (Vizzu)**.
  - Botão **"⚡ Testar Conexão"** em cada card (ou "Testar Todas") que verifica a conectividade real/configuração no backend e exibe badge **`🟢 SUCESSO (latência ms)`** ou **`🔴 ERRO (motivo)`**.

#### ABA 2: 💬 Playground WhatsApp Ao Vivo & Raio-X da IA
- **Coluna Esquerda — Simulador Visual de WhatsApp:**
  - Seletor do Cliente (`loboads`, `companhia-dos-sonhos`, `splash-piscinas`, `vizzu`).
  - Seletor do Motor: **Gemini 3.8 Flash (Live API)** ou **Motor Determinístico (Instantâneo Offline)**.
  - Campo de Nome e Telefone do Lead simulado.
  - Janela de chat idêntica ao WhatsApp onde você pode enviar várias mensagens seguidas:
    - Ao digitar e enviar mensagens rápidas, mostre um contador visual do **Debounce** (ou permita enviar como lote imediato) para testar o agrupamento de 3+ mensagens em 1 única resposta!
    - Botões de atalho rápido para testar cenários com 1 clique:
      - *"🛏️ Cenário 3 Mensagens (Colchão + Belém)"*
      - *"📅 Cenário Agendamento de Visita"*
      - *"💸 Cenário Pedido de Desconto Alto (Handoff)"*
      - *"🚨 Cenário Emergência Clínica CVV 188 (Vizzu)"*
  - Botão **"🗑️ Limpar e Resetar Conversa"** para testar novamente do zero.
- **Coluna Direita — Raio-X Técnico do Turno:**
  - Exibe após cada resposta:
    - Badge de Status (`🟢 Turno Processado com Sucesso` ou `🟡 Handoff Acionado` ou `🔴 Erro`), `Turn ID` e `Latência Total (ms)`.
    - **Estágio do LangGraph** (`triage`, `discovery`, `offer_catalog`, `booking`, `handoff`, `silenced`) e **Intenção do JEV**.
    - **Ferramentas Executadas:** lista cada tool chamada (`search_catalog`, `get_delivery_info`, `check_available_slots`, etc.), os argumentos enviados e o JSON retornado.
    - **Atributos Extraídos do Lead** (`cidade`, `tamanho`, `tecnologia`, etc.) e **Tags Aplicadas** (`qualificado`, `visita_agendada`, `atendimento_humano`).

#### ABA 3: 🏢 Clientes (Tenants), Catálogo, Preços, Entregas e Agenda
- Seletor de Cliente no topo + botão **"+ Criar Novo Cliente"**.
- **Configurações Gerais do Cliente:**
  - Nome, Segmento (`mattress`, `pools`, `psychology`, `generic`), Fuso Horário, Janela de Silêncio do Debounce (`silence_seconds`), Teto Máximo (`max_window_seconds`), Provedor WhatsApp (`waha` / `astracalls`), Sessão (`Danyllo`), CRM (`chatwoot` / `ghl`, `account_id`, `inbox_id`, `location_id`, `assignee_means_human`) e chave **`Dados de Demonstração (demo_data)`**.
- **Editor Visual de Catálogo e Preços:**
  - Tabela interativa com todos os itens do cliente.
  - Botões para **+ Novo Produto/Serviço**, **Editar** e **Excluir** direto na tela (código, nome, categoria, descrição, preço em R$, máximo de parcelas sem juros, % desconto à vista, em estoque, preço sob consulta, sinônimos).
- **Editor Visual de Entregas / Frete por Cidade:**
  - Adicionar, editar ou remover cidades atendidas, valor do frete (ou Grátis) e prazo em dias úteis.
- **Editor Visual de Agenda e Informações do Negócio:**
  - Editar Endereço, Horário de Funcionamento, Garantia, Formas de Pagamento e Serviços Agendáveis.
- **Botão `"💾 Salvar e Aplicar Imediatamente"`:**
  - Valida com Pydantic no backend, salva no JSON do tenant em `data/tenants/{tenant_id}.json`, recarrega o `TenantDataStore` em memória na hora (sem precisar reiniciar o servidor!) e mostra Toast verde de **Sucesso** ou vermelho de **Erro**.

#### ABA 4: 🤝 Conversas Ativas & Controle de Handoff Humano
- Lista todos os contatos que interagiram com o agente (com status atual, estágio, tags e atributos coletados).
- Destaca os contatos onde a **IA está Pausada (`human_active` / `AIPauseRegistry`)**.
- Botão **"🔄 Reativar IA"** ao lado de cada contato pausado (remove a pausa no Redis e a tag `atendimento_humano` na hora!) e botão **"⏸️ Pausar IA Manualmente"**.

#### ABA 5: 🔗 Webhooks & Teste de Disparo Real no WhatsApp
- Exibe as URLs prontas de Webhook para cada cliente (detectando automaticamente o domínio atual, ex.: `https://salesagent.loboads.top/webhook/{tenant_id}/waha` e `/astracalls`) com botão **"📋 Copiar"**.
- **Instruções Rápidas de Teste no Celular:** explica em 3 linhas como mandar `/lobo-sales-agent Olá` ou liberar o próprio número na Aba 1.
- **Card "📲 Testar Envio de Mensagem via Gateway (WAHA / AstraCalls)":** permite digitar um telefone de destino, escolher o provedor/sessão e enviar uma mensagem de teste real a partir do painel, exibindo na tela se o WAHA/AstraCalls respondeu **Sucesso** ou **Erro**.

---

## 4. SegurançaOpcional do Painel e Testes Automatizados

1. Se a variável `ADMIN_TOKEN` ou `ADMIN_PASSWORD` estiver definida no `.env`, proteja as rotas `/api/admin/*` via header/sessão simples; se não estiver definida (ambiente atual de desenvolvimento/homologação), permita acesso direto sem fricção.
2. Escreva testes automatizados em `tests/test_admin_and_sandbox.py` cobrindo:
   - O bloqueio silencioso do `SandboxGuard` quando um número desconhecido manda mensagem sem `/lobo` ou `/lobo-sales-agent`.
   - A liberação imediata quando o número está em `allowed_phones`.
   - A liberação de sessão por 30 minutos quando a mensagem contém `/lobo` ou `/lobo-sales-agent` (e a remoção do prefixo antes de ir para a IA).
   - O comando `/parar` ou `/reset` encerrando a sessão de teste.
   - As rotas da API do Painel Administrativo (`GET /`, listagem/edição de tenants, catálogo, sandbox config, simulação no playground e reativação de IA).
3. Garanta que todos os 192 testes existentes continuem passando junto com os novos testes!
