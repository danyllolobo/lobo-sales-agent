/* Lobo AI Sales Agent — Painel administrativo (JavaScript vanilla, sem build). */
"use strict";
(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const TOKEN_KEY = "lobo_admin_token";
  const THEME_KEY = "lobo_theme";
  const WEEKDAYS = [["mon", "Seg"], ["tue", "Ter"], ["wed", "Qua"], ["thu", "Qui"], ["fri", "Sex"], ["sat", "Sáb"], ["sun", "Dom"]];
  const SEGMENTS = ["generic", "mattress", "pools", "psychology", "dental", "aesthetics", "clinic", "real_estate", "automotive", "education", "law", "retail"];
  const SEGMENT_OPTIONS = [
    ["generic", "Genérico / B2B / Serviços / Agência"],
    ["mattress", "Loja de Colchões & Estofados"],
    ["pools", "Piscinas & Lazer"],
    ["psychology", "Clínica de Psicologia / Saúde Mental"],
    ["dental", "Odontologia & Harmonização"],
    ["aesthetics", "Estética / Dermatologia / Spa"],
    ["clinic", "Clínica Médica / Fisioterapia / Exames"],
    ["real_estate", "Imobiliária / Construtora / Corretores"],
    ["automotive", "Concessionária / Seminovos / Automotivo"],
    ["education", "Educação / Cursos / Infoprodutos"],
    ["law", "Escritório de Advocacia / Jurídico"],
    ["retail", "Varejo Geral / Móveis / Moda / Ótica"],
  ];
  const PLAN_OPTIONS = [
    ["starter", "Plano Essencial — R$ 790/mês (até 800 conversas · 15 itens)"],
    ["pro", "Plano Pro Vendas — R$ 1.690/mês (até 2.500 conversas · Follow-up + CRM)"],
    ["enterprise", "Escala Enterprise — R$ 2.990/mês (até 6.000 conversas · Multi-Filial + ERP)"],
  ];
  const PLAN_SPECS = {
    starter: {
      name: "Plano Essencial",
      subtitle: "Para consultórios e pequenas lojas",
      price: "R$ 790/mês",
      setup: "Setup: R$ 2.500 (única)",
      max_whatsapp_numbers: 1,
      monthly_turn_limit: 800,
      max_catalog_items: 15,
      followup_allowed: false,
      max_followup_steps: 0,
      crm_allowed: false,
      features: ["1 número de WhatsApp", "Até 800 conversas/mês", "Compreensão de áudios", "Catálogo de até 15 itens", "Horário noturno e fins de semana"],
    },
    pro: {
      name: "Plano Pro Vendas",
      subtitle: "Para varejo, clínicas, colchões e piscinas (Mais Recomendado)",
      price: "R$ 1.690/mês",
      setup: "Setup: R$ 4.500 (única)",
      max_whatsapp_numbers: 2,
      monthly_turn_limit: 2500,
      max_catalog_items: null,
      followup_allowed: true,
      max_followup_steps: 4,
      crm_allowed: true,
      features: ["1 a 2 números de WhatsApp", "Até 2.500 conversas/mês", "Compreensão nativa de áudios", "Catálogo completo ilimitado", "Inteligência de frete por bairro", "Régua de follow-up automático (até 4 etapas)", "Integração CRM (Chatwoot / GHL)"],
    },
    enterprise: {
      name: "Escala Enterprise",
      subtitle: "Para franquias e redes com filiais",
      price: "R$ 2.990/mês",
      setup: "Setup: sob consulta",
      max_whatsapp_numbers: 4,
      monthly_turn_limit: 6000,
      max_catalog_items: null,
      followup_allowed: true,
      max_followup_steps: 6,
      crm_allowed: true,
      features: ["Até 4 números / departamentos", "Até 6.000 conversas/mês", "Catálogo completo ilimitado", "Integração com ERP / Estoque / API Externa", "Régua de follow-up completa (até 6 etapas)", "Suporte VIP e SLA prioritário"],
    },
  };
  const FOLLOWUP_STRATEGIES = [
    ["warm_check", "1º Toque — Retomada Quente (Dúvida / Produto)"],
    ["value_reinforce", "Reforço de Valor / Condição (Parcelamento / Agenda)"],
    ["objection_break", "Quebra de Objeção / Reativação Consultiva"],
    ["breakup", "Encerramento Elegante / Break-up (Sem Pressão)"],
  ];
  const DEFAULT_FOLLOWUP_STEPS = [
    { step: 1, delay_minutes: 120, label: "1º Toque — Mesmo Dia (Retomada Quente)", strategy: "warm_check", custom_message: null },
    { step: 2, delay_minutes: 1440, label: "2º Toque — Dia Seguinte (+1 dia)", strategy: "value_reinforce", custom_message: null },
    { step: 3, delay_minutes: 4320, label: "3º Toque — Reativação Consultiva (+3 dias)", strategy: "objection_break", custom_message: null },
    { step: 4, delay_minutes: 10080, label: "4º Toque — Despedida Sem Pressão (+7 dias)", strategy: "breakup", custom_message: null },
  ];
  const LLM_MODELS = ["default", "gemini-3.8-flash", "gemini-2.5-pro", "gemini-3-pro", "gemini-4-argon", "gpt-4o", "gpt-4o-mini"];
  const LLM_MODEL_OPTIONS = [
    ["default", "Padrão do Sistema (Global)"],
    ["gemini-3.8-flash", "gemini-3.8-flash (Ultra Rápido & Econômico)"],
    ["gemini-2.5-pro", "gemini-2.5-pro (Raciocínio Avançado & Vendas de Alto Ticket)"],
    ["gemini-3-pro", "gemini-3-pro (Negociação Complexa)"],
    ["gemini-4-argon", "gemini-4-argon"],
    ["gpt-4o", "gpt-4o (OpenAI)"],
    ["gpt-4o-mini", "gpt-4o-mini (OpenAI)"],
  ];
  const INTEGRATION_ICONS = { gemini: "✨", openai: "🎙️", waha: "🟢", astracalls: "📞", chatwoot: "💬", ghl: "🧠" };

  const S = {
    overview: null,
    tenants: [],
    sandbox: null,
    integrations: {},
    tenant: null,
    draft: null,
    dirty: false,
    jsonError: null,
    authPrompted: false,
    pg: { queue: [], firstAt: 0, lastAt: 0, timer: null, busy: false, chats: {} },
    cvTimer: null,
  };

  // ------------------------------------------------------------------ utils
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const clone = (o) => (o === undefined ? undefined : JSON.parse(JSON.stringify(o)));
  const brl = (v) => (v === null || v === undefined || v === "" ? "—" : Number(v).toLocaleString("pt-BR", { style: "currency", currency: "BRL" }));
  const nowTime = () => new Date().toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit" });
  const mmss = (s) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(Math.max(0, s % 60)).padStart(2, "0")}`;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function relTime(iso) {
    const diff = (Date.now() - new Date(iso).getTime()) / 1000;
    if (!isFinite(diff)) return "—";
    if (diff < 60) return "agora";
    if (diff < 3600) return `há ${Math.floor(diff / 60)} min`;
    if (diff < 86400) return `há ${Math.floor(diff / 3600)} h`;
    return new Date(iso).toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" });
  }

  function md(text) {
    // Escapa primeiro; só então aplica um markdown mínimo (negrito, itálico, código, quebras).
    return esc(text)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+?)`/g, "<code>$1</code>")
      .replace(/(^|[\s(])\*(?!\s)(.+?)\*(?=[\s).,!?]|$)/g, "$1<em>$2</em>")
      .replace(/\n/g, "<br>");
  }

  function jsonHtml(value) {
    const s = JSON.stringify(value, null, 2) ?? "null";
    const re = /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g;
    let out = "", last = 0, m;
    while ((m = re.exec(s))) {
      out += esc(s.slice(last, m.index));
      const cls = m[1] ? (m[2] ? "k" : "s") : m[3] ? "b" : "n";
      out += `<span class="${cls}">${esc(m[1] || m[0])}</span>${m[2] ? esc(m[2]) : ""}`;
      last = re.lastIndex;
    }
    return out + esc(s.slice(last));
  }

  function parseMoney(raw) {
    let v = String(raw ?? "").trim().replace(/R\$|\s/g, "");
    if (!v) return null;
    if (v.includes(",")) v = v.replace(/\./g, "").replace(",", ".");
    const n = Number(v);
    return isFinite(n) ? n.toFixed(2) : raw;
  }

  // ------------------------------------------------------------------ toasts
  function toast(kind, title, msg = "", ms = null) {
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.innerHTML = `<span class="t-icon">${kind === "ok" ? "🟢" : "🔴"}</span>
      <div class="t-body"><div class="t-title">${esc(title)}</div>${msg ? `<div class="t-msg">${esc(msg)}</div>` : ""}</div>
      ${ms !== null ? `<span class="t-ms">${esc(ms)} ms</span>` : ""}`;
    $("#toasts").appendChild(el);
    const ttl = kind === "ok" ? 3800 : 7000;
    setTimeout(() => { el.classList.add("leaving"); setTimeout(() => el.remove(), 260); }, ttl);
    el.addEventListener("click", () => el.remove());
  }

  // ------------------------------------------------------------------ API
  function detailOf(data, status) {
    if (!data) return `HTTP ${status}`;
    const d = data.detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d) && d.length) {
      const e = d[0];
      const loc = (e.loc || []).filter((p) => p !== "body").join(".");
      return `${loc ? loc + ": " : ""}${e.msg || "inválido"}${d.length > 1 ? ` (+${d.length - 1})` : ""}`;
    }
    return `HTTP ${status}`;
  }

  async function api(method, path, body) {
    const t0 = performance.now();
    const headers = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const token = localStorage.getItem(TOKEN_KEY);
    if (token) headers["X-Admin-Token"] = token;
    let res;
    try {
      res = await fetch(path, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined });
    } catch (e) {
      return { ok: false, status: 0, ms: Math.round(performance.now() - t0), error: "Servidor inacessível (falha de rede)." };
    }
    const ms = Math.round(performance.now() - t0);
    let data = null;
    try { data = await res.json(); } catch (e) { /* corpo vazio */ }
    if (!res.ok) {
      if (res.status === 401 && !S.authPrompted) { S.authPrompted = true; openAuth(); }
      return { ok: false, status: res.status, ms, data, error: detailOf(data, res.status) };
    }
    return { ok: true, status: res.status, ms, data };
  }

  /** Executa uma ação com spinner no botão e toast de sucesso/erro com latência. */
  async function act(btn, okTitle, fn, { okMsg = "", errTitle = null, silentOk = false } = {}) {
    if (btn) { btn.disabled = true; btn.classList.add("loading"); }
    try {
      const r = await fn();
      if (r.ok) { if (!silentOk) toast("ok", okTitle, typeof okMsg === "function" ? okMsg(r.data) : okMsg, r.ms); }
      else toast("err", errTitle || `Erro: ${okTitle.replace(/^[^\wÀ-ú]+/, "")}`, r.error, r.ms);
      return r;
    } finally {
      if (btn) { btn.disabled = false; btn.classList.remove("loading"); }
    }
  }

  // ------------------------------------------------------------------ tema
  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem(THEME_KEY, theme);
  }
  $("#btn-theme").addEventListener("click", () => {
    const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    applyTheme(next);
    toast("ok", next === "dark" ? "🌙 Tema escuro ativado" : "☀️ Tema claro ativado");
  });

  // ------------------------------------------------------------------ auth
  function openAuth() {
    const dlg = $("#dlg-auth");
    $("input[name=token]", dlg).value = localStorage.getItem(TOKEN_KEY) || "";
    if (!dlg.open) dlg.showModal();
  }
  $("#btn-auth").addEventListener("click", openAuth);
  $("#form-auth").addEventListener("submit", (ev) => {
    const action = ev.submitter?.value;
    if (action === "clear") { localStorage.removeItem(TOKEN_KEY); toast("ok", "Token removido deste navegador"); }
    else if (action === "save") {
      const v = $("#form-auth input[name=token]").value.trim();
      if (v) localStorage.setItem(TOKEN_KEY, v);
      toast("ok", "Token salvo", "Recarregando dados do painel…");
    } else return;
    S.authPrompted = false;
    boot();
  });

  // ------------------------------------------------------------------ abas
  const loaders = {
    sandbox: () => loadSandbox(),
    engine: () => loadEngine(),
    playground: () => renderChat(),
    tenants: () => (S.draft ? null : loadTenant($("#tn-select").value)),
    conversations: () => loadConversations(),
    webhooks: () => loadWebhooks(),
  };

  // ------------------------------------------------------------------ motor global do agente (Kernel + régua)
  const FU_STRATEGY_LABEL = Object.fromEntries(FOLLOWUP_STRATEGIES.map(([k, v]) => [k, v.split(" (")[0]]));
  const FU_SKIP_LABEL = {
    followup_disabled: "régua desligada no cliente",
    waiting_ai_not_lead: "aguardando a IA responder o lead",
    ai_paused: "IA pausada (humano atendendo)",
    max_attempts_reached: "régua concluída",
    plan_not_allowed: "plano sem follow-up",
    tenant_inactive: "cliente inativo",
    send_error: "falha de envio",
  };
  async function loadEngine() {
    if (!S.kernelLoaded) {
      const k = await api("GET", "/api/admin/kernel");
      if (k.ok) {
        S.kernelLoaded = true;
        $("#kn-text").textContent = k.data.kernel;
        $("#kn-meta").textContent = `🔒 somente leitura · ${k.data.chars.toLocaleString("pt-BR")} caracteres`;
        $("#kn-blocks").innerHTML = k.data.blocks.map((b) => `<span class="chip">${esc(b)}</span>`).join("");
      } else {
        $("#kn-text").textContent = `Falha ao carregar o Kernel: ${k.error}`;
      }
    }
    await loadFollowupStatus();
  }
  async function loadFollowupStatus(btn) {
    const r = btn ? await act(btn, "Fila de follow-up atualizada", () => api("GET", "/api/admin/followup/status"), { silentOk: true })
      : await api("GET", "/api/admin/followup/status");
    if (!r.ok) { $("#fu-sched-body").textContent = r.error; return; }
    const sc = r.data.scheduler || {};
    const chip = $("#fu-sched-chip");
    chip.className = `chip ${sc.running ? "chip-ok" : "chip-err"}`;
    chip.textContent = sc.running ? `🟢 automática · a cada ${Math.round(sc.interval_seconds)}s` : "🔴 agendador parado";
    const last = sc.last_run_at ? `${relTime(sc.last_run_at)} (${sc.last_result?.evaluated ?? 0} conversas avaliadas)` : "ainda não rodou";
    const reasons = Object.entries(r.data.skipped_reasons || {}).filter(([k]) => k !== "delay_not_reached")
      .map(([k, n]) => `${n}× ${esc(FU_SKIP_LABEL[k] || k)}`).join(" · ");
    $("#fu-sched-body").innerHTML = `
      <div class="kv"><span>Última varredura</span><strong>${esc(last)}</strong></div>
      <div class="kv"><span>Enviados desde que o servidor subiu</span><strong>${esc(sc.total_sent ?? 0)}</strong></div>
      ${sc.last_error ? `<div class="notice notice-warn">Último erro: ${esc(sc.last_error)}</div>` : ""}
      ${reasons ? `<p class="muted">Fora da fila agora: ${reasons}</p>` : ""}
      <p class="muted">Cada cliente liga a régua e define as etapas em <a href="#tenants" data-goto-tab="tenants">Clientes &amp; Catálogo</a>. O tempo de cada etapa conta a partir da última mensagem enviada.</p>`;
    const q = r.data.queue || [];
    $("#fu-queue").innerHTML = q.length ? q.map((it) => `
      <div class="list-item">
        <div class="grow"><strong>${esc(it.name || it.phone_masked)}</strong> <span class="muted small">${esc(it.tenant_id)}</span>
          <div class="muted small">${esc(it.attempt)}º toque · ${esc(FU_STRATEGY_LABEL[it.strategy] || it.strategy || "")}</div></div>
        <span class="chip ${it.due_in_minutes <= 0 ? "chip-warn" : ""}">${it.due_in_minutes <= 0 ? "envia na próxima varredura" : `em ${fmtMinutes(it.due_in_minutes)}`}</span>
      </div>`).join("") : `<div class="empty">Nenhum lead aguardando follow-up agora.</div>`;
  }
  function fmtMinutes(m) {
    if (m < 60) return `${Math.ceil(m)} min`;
    if (m < 1440) return `${Math.round(m / 60)} h`;
    return `${Math.round(m / 1440)} dia(s)`;
  }
  document.addEventListener("click", (ev) => {
    const a = ev.target.closest("[data-goto-tab]");
    if (a) { ev.preventDefault(); activateTab(a.dataset.gotoTab); }
  });
  $("#fu-sched-refresh").addEventListener("click", (ev) => loadFollowupStatus(ev.currentTarget));
  function activateTab(name) {
    if (!loaders[name]) name = "sandbox";
    $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    $$(".pane").forEach((p) => p.classList.toggle("active", p.id === `tab-${name}`));
    $("#tn-savebar").classList.toggle("show", name === "tenants" && !!S.draft);
    if (location.hash !== `#${name}`) history.replaceState(null, "", `#${name}`);
    loaders[name]();
  }
  $$(".tab").forEach((t) => t.addEventListener("click", () => activateTab(t.dataset.tab)));
  const currentTab = () => ($(".tab.active")?.dataset.tab || "sandbox");

  // ------------------------------------------------------------------ overview
  async function loadOverview() {
    const r = await api("GET", "/api/admin/overview");
    if (!r.ok) { toast("err", "Falha ao carregar o painel", r.error, r.ms); return false; }
    S.overview = r.data;
    S.tenants = r.data.tenants;
    const env = r.data.environment;
    $("#env-badge").className = `badge ${env === "production" ? "badge-ok" : "badge-info"}`;
    $("#env-badge").textContent = `🌐 ${env}`;
    $("#version-badge").textContent = `v${r.data.version}`;
    $("#auth-warning").classList.toggle("hidden", !!r.data.auth_required);
    setSandboxBadge(r.data.sandbox_active);
    r.data.integrations.forEach((i) => { S.integrations[i.name] = { ...(S.integrations[i.name] || {}), ...i }; });
    fillTenantSelects();
    renderIntegrations();
    return true;
  }

  function setSandboxBadge(active) {
    const b = $("#sandbox-badge");
    b.className = `badge ${active ? "badge-warn" : "badge-ok"}`;
    b.textContent = active ? "🔒 Sandbox ativo" : "🟢 Produção aberta";
  }

  function fillTenantSelects() {
    const opts = S.tenants.map((t) => `<option value="${esc(t.tenant_id)}">${esc(t.display_name)}${t.active ? "" : " (inativo)"}</option>`).join("");
    $$(".tenant-select").forEach((sel) => {
      const prev = sel.value;
      sel.innerHTML = opts;
      const fallback = S.tenants.find((t) => t.tenant_id === "companhia-dos-sonhos") ? "companhia-dos-sonhos" : S.tenants[0]?.tenant_id;
      sel.value = S.tenants.some((t) => t.tenant_id === prev) ? prev : (sel.id === "ts-tenant" && S.tenants.some((t) => t.tenant_id === "loboads") ? "loboads" : fallback) || "";
    });
    const cv = $("#cv-tenant");
    const prevCv = cv.value;
    cv.innerHTML = `<option value="">Todos os clientes</option>${opts}`;
    cv.value = prevCv;
    syncPlaygroundTenant();
    syncSendTenant();
  }
  const tenantSummary = (id) => S.tenants.find((t) => t.tenant_id === id);

  // ================================================================== ABA 1 — SANDBOX
  async function loadSandbox() {
    const r = await api("GET", "/api/admin/sandbox");
    if (!r.ok) { toast("err", "Falha ao carregar o Sandbox", r.error, r.ms); return; }
    S.sandbox = r.data;
    renderSandbox();
  }

  function renderSandbox() {
    const sb = S.sandbox;
    if (!sb) return;
    const cfg = sb.config;
    setSandboxBadge(sb.active);
    $("#sandbox-enabled").checked = cfg.enabled;
    $("#sandbox-switch-wrap").classList.toggle("forced", sb.forced_by_environment && !cfg.enabled);
    const title = sb.active ? "🔒 Modo Sandbox Seguro (Ativo)" : "🟢 Modo Produção Aberto (Atende Todos)";
    $("#sandbox-switch-title").textContent = title;
    $("#sandbox-switch-sub").textContent = sb.active
      ? (!cfg.enabled && sb.forced_by_environment
          ? "Switch desligado, mas a trava segue ativa por estar fora de produção."
          : "A IA só responde números liberados, sessões de teste ou quem enviar a palavra-chave.")
      : "A IA responde QUALQUER contato que escrever para o número conectado.";
    const chip = $("#sandbox-state-chip");
    chip.className = `chip ${sb.active ? "chip-warn" : "chip-ok"}`;
    chip.textContent = sb.active ? "PROTEGIDO" : "ABERTO";
    $("#sandbox-forced").classList.toggle("hidden", !sb.forced_by_environment);
    $("#sandbox-env").textContent = sb.environment;
    if (document.activeElement?.closest?.(".card-hero .form-grid") == null) {
      $("#sandbox-triggers").value = cfg.trigger_keywords.join(", ");
      $("#sandbox-stops").value = cfg.stop_keywords.join(", ");
      $("#sandbox-ttl").value = cfg.session_ttl_minutes;
      const sil = cfg.debounce_silence_seconds ?? S.overview?.debounce?.silence_seconds ?? 8;
      const win = cfg.debounce_max_window_seconds ?? S.overview?.debounce?.max_window_seconds ?? 20;
      const silEl = $("#sandbox-silence");
      const winEl = $("#sandbox-max-window");
      if (silEl) {
        silEl.value = sil;
        const lbl = $("#sandbox-silence-val");
        if (lbl) lbl.textContent = `${sil}s`;
      }
      if (winEl) {
        winEl.value = win;
        const lbl = $("#sandbox-max-window-val");
        if (lbl) lbl.textContent = `${win}s`;
      }
      const splitEl = $("#sandbox-split-messages");
      if (splitEl) splitEl.checked = cfg.split_messages !== false;
      const modelEl = $("#sandbox-llm-model");
      if (modelEl) modelEl.value = cfg.llm_model || S.overview?.gemini_model || "gemini-3.8-flash";
    }
    const phones = cfg.allowed_phones.map((p) => `<span class="chip chip-info">📱 ${esc(p)}<button data-phone="${esc(p)}" title="Remover">×</button></span>`);
    const seeds = sb.seed_phones.map((p) => `<span class="chip chip-lock" title="Definido em SANDBOX_ALLOWED_PHONES (.env)">🔒 ${esc(p)} · .env</span>`);
    $("#sandbox-phones").innerHTML = phones.concat(seeds).join("") || `<div class="empty">Nenhum número liberado ainda.</div>`;
    renderSessions();
  }

  function renderSessions() {
    const sessions = S.sandbox?.sessions || [];
    $("#sandbox-sessions").innerHTML = sessions.length
      ? sessions.map((s, i) => `
        <div class="list-item">
          <span class="chip chip-ok">ATIVA</span>
          <div class="grow"><strong>${esc(s.phone)}</strong><div class="muted small">${esc(tenantSummary(s.tenant_id)?.display_name || s.tenant_id)}</div></div>
          <span class="mono small" data-countdown="${i}">⏱ ${mmss(s.expires_in_seconds)}</span>
          <button class="btn btn-danger btn-sm" data-end-session="${i}">Encerrar / Resetar</button>
        </div>`).join("")
      : `<div class="empty">Nenhuma sessão de teste ativa. Envie <code>${esc(S.sandbox?.config.trigger_keywords[0] || "/lobo")}</code> no WhatsApp para abrir uma.</div>`;
  }

  setInterval(() => {
    const sessions = S.sandbox?.sessions;
    if (!sessions?.length) return;
    let expired = false;
    sessions.forEach((s, i) => {
      s.expires_in_seconds -= 1;
      if (s.expires_in_seconds <= 0) expired = true;
      const el = $(`[data-countdown="${i}"]`);
      if (el) el.textContent = `⏱ ${mmss(s.expires_in_seconds)}`;
    });
    if (expired) { S.sandbox.sessions = sessions.filter((s) => s.expires_in_seconds > 0); renderSessions(); }
  }, 1000);

  $("#sandbox-enabled").addEventListener("change", async (ev) => {
    const enabled = ev.target.checked;
    if (!enabled && S.sandbox && !S.sandbox.forced_by_environment
        && !confirm("Abrir o atendimento para TODOS os contatos do número conectado?\n\nA IA passará a responder clientes reais.")) {
      ev.target.checked = true;
      return;
    }
    const r = await act(null, enabled ? "🔒 Sandbox ativado" : "🟢 Modo produção salvo", () => api("PUT", "/api/admin/sandbox", { enabled }),
      { okMsg: (d) => (d.forced_by_environment && !enabled ? "Fora de produção a trava continua ativa." : "") });
    if (r.ok) { S.sandbox = r.data; } else { ev.target.checked = !enabled; }
    renderSandbox();
  });

  $("#sandbox-silence")?.addEventListener("input", (ev) => {
    const v = Number(ev.target.value);
    const lbl = $("#sandbox-silence-val");
    if (lbl) lbl.textContent = `${v}s`;
    const winEl = $("#sandbox-max-window");
    if (winEl && Number(winEl.value) <= v) {
      const next = Math.min(60, v + 5);
      winEl.value = next;
      const wLbl = $("#sandbox-max-window-val");
      if (wLbl) wLbl.textContent = `${next}s`;
    }
  });

  $("#sandbox-max-window")?.addEventListener("input", (ev) => {
    const v = Number(ev.target.value);
    const lbl = $("#sandbox-max-window-val");
    if (lbl) lbl.textContent = `${v}s`;
  });

  $("#btn-save-sandbox").addEventListener("click", async (ev) => {
    const split = (v) => v.split(/[,\s]+/).map((x) => x.trim()).filter(Boolean);
    const sil = Number($("#sandbox-silence")?.value || 8);
    let win = Number($("#sandbox-max-window")?.value || 20);
    if (win <= sil) win = Math.min(60, sil + 2);
    const body = {
      trigger_keywords: split($("#sandbox-triggers").value),
      stop_keywords: split($("#sandbox-stops").value),
      session_ttl_minutes: Number($("#sandbox-ttl").value),
      debounce_silence_seconds: sil,
      debounce_max_window_seconds: win,
      split_messages: $("#sandbox-split-messages") ? $("#sandbox-split-messages").checked : true,
      llm_model: $("#sandbox-llm-model") ? $("#sandbox-llm-model").value : "gemini-3.8-flash",
    };
    const r = await act(ev.currentTarget, "Regras do Sandbox, Debounce e Modelo salvas", () => api("PUT", "/api/admin/sandbox", body));
    if (r.ok) {
      S.sandbox = r.data;
      if (S.overview) {
        if (S.overview.debounce) {
          S.overview.debounce.silence_seconds = sil;
          S.overview.debounce.max_window_seconds = win;
        }
        if (body.llm_model) S.overview.gemini_model = body.llm_model;
      }
      renderSandbox();
    }
  });

  $("#form-phone").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const phone = $("#sandbox-phone").value.replace(/\D/g, "");
    if (!phone) { toast("err", "Informe um telefone", "Formato: DDI + DDD + número, ex.: 5591988887777"); return; }
    const r = await act(ev.submitter, "Número liberado na whitelist", () => api("POST", "/api/admin/sandbox/phones", { phone }), { okMsg: phone });
    if (r.ok) { S.sandbox = r.data; $("#sandbox-phone").value = ""; renderSandbox(); }
  });

  $("#sandbox-phones").addEventListener("click", async (ev) => {
    const btn = ev.target.closest("button[data-phone]");
    if (!btn) return;
    const phone = btn.dataset.phone;
    const r = await act(btn, "Número removido da whitelist", () => api("DELETE", `/api/admin/sandbox/phones/${encodeURIComponent(phone)}`), { okMsg: phone });
    if (r.ok) { S.sandbox = r.data; renderSandbox(); }
  });

  $("#sandbox-sessions").addEventListener("click", async (ev) => {
    const btn = ev.target.closest("button[data-end-session]");
    if (!btn) return;
    const s = S.sandbox.sessions[Number(btn.dataset.endSession)];
    const r = await act(btn, "Sessão encerrada e memória resetada",
      () => api("DELETE", `/api/admin/sandbox/sessions/${encodeURIComponent(s.tenant_id)}/${encodeURIComponent(s.phone)}`), { okMsg: s.phone });
    if (r.ok) { S.sandbox = r.data; renderSandbox(); }
  });
  $("#btn-refresh-sandbox").addEventListener("click", async (ev) => {
    await act(ev.currentTarget, "Sandbox atualizado", async () => { const r = await api("GET", "/api/admin/sandbox"); if (r.ok) { S.sandbox = r.data; renderSandbox(); } return r; });
  });

  // ------------------------------------------------------------------ integrações
  function renderIntegrations() {
    const order = ["gemini", "openai", "waha", "astracalls", "chatwoot", "ghl"];
    $("#integrations").innerHTML = order.filter((n) => S.integrations[n]).map((n) => {
      const it = S.integrations[n];
      let badge;
      if (it.status === "ok") badge = `<span class="badge badge-ok">🟢 SUCESSO · ${esc(it.latency_ms)} ms</span>`;
      else if (it.status === "error") badge = `<span class="badge badge-err">🔴 ERRO</span>`;
      else if (!it.configured) badge = `<span class="badge badge-ghost">⚪ Não configurado</span>`;
      else badge = `<span class="badge badge-ghost">Configurado · não testado</span>`;
      const detail = it.detail || (it.configured ? "Clique em testar para verificar a conexão real." : "Credenciais ausentes no ambiente.");
      return `<div class="integration ${esc(it.status || "")}" data-integration="${esc(n)}">
        <div class="integration-head">
          <div class="integration-icon">${INTEGRATION_ICONS[n] || "🔌"}</div>
          <div class="integration-title"><strong>${esc(it.label)}</strong><small>${esc(it.target || "—")}</small></div>
        </div>
        ${badge}
        <div class="integration-detail">${esc(detail)}</div>
        <button class="btn btn-sm" data-test="${esc(n)}">⚡ Testar Conexão</button>
      </div>`;
    }).join("");
  }

  function applyIntegrationResult(res) {
    S.integrations[res.name] = { ...(S.integrations[res.name] || {}), ...res };
  }

  $("#integrations").addEventListener("click", async (ev) => {
    const btn = ev.target.closest("button[data-test]");
    if (!btn) return;
    const name = btn.dataset.test;
    btn.disabled = true; btn.classList.add("loading");
    const r = await api("POST", `/api/admin/integrations/${name}/test`);
    if (!r.ok) { toast("err", "Falha ao testar integração", r.error, r.ms); renderIntegrations(); return; }
    applyIntegrationResult(r.data);
    renderIntegrations();
    toast(r.data.ok ? "ok" : "err", `${r.data.label}: ${r.data.ok ? "SUCESSO" : r.data.status === "not_configured" ? "não configurado" : "ERRO"}`, r.data.detail, r.data.latency_ms);
  });

  $("#btn-test-all").addEventListener("click", async (ev) => {
    const btn = ev.currentTarget;
    btn.disabled = true; btn.classList.add("loading");
    const r = await api("POST", "/api/admin/integrations/test-all");
    btn.disabled = false; btn.classList.remove("loading");
    if (!r.ok) { toast("err", "Falha ao testar integrações", r.error, r.ms); return; }
    r.data.forEach(applyIntegrationResult);
    renderIntegrations();
    const ok = r.data.filter((x) => x.ok).length;
    toast(ok === r.data.length ? "ok" : "err", `${ok}/${r.data.length} integrações com sucesso`,
      r.data.filter((x) => !x.ok).map((x) => `${x.label}: ${x.detail}`).join(" · "), r.ms);
  });

  // ================================================================== ABA 2 — PLAYGROUND
  const pgKey = () => `${$("#pg-tenant").value}|${$("#pg-phone").value.replace(/\D/g, "")}`;
  const pgChat = () => (S.pg.chats[pgKey()] ||= []);

  function syncPlaygroundTenant() {
    const t = tenantSummary($("#pg-tenant").value);
    if (!t) return;
    $("#pg-chat-title").textContent = t.display_name;
    $("#pg-chat-sub").textContent = `${t.segment} · simulação isolada (sem WhatsApp/CRM reais)`;
    const silence = t.debounce_silence_seconds ?? S.overview?.debounce?.silence_seconds ?? 4;
    $("#pg-silence").value = silence;
    const engine = $("#pg-engine");
    engine.querySelector('option[value="gemini"]').disabled = !S.overview?.playground_gemini_available;
    if (engine.value === "gemini" && !S.overview?.playground_gemini_available) engine.value = "deterministic";
    renderChat();
  }
  $("#pg-tenant").addEventListener("change", syncPlaygroundTenant);
  $("#pg-phone").addEventListener("change", renderChat);

  function renderChat() {
    const chat = pgChat();
    const body = $("#pg-chat");
    body.innerHTML = chat.length
      ? chat.map((m) => {
          if (m.role === "system") return `<div class="bubble system">${esc(m.text)}</div>`;
          const cls = `bubble ${m.role === "lead" ? "out" : "in"}${m.pending ? " pending" : ""}${m.handoff ? " handoff" : ""}`;
          const tick = m.role === "lead" ? (m.pending ? " 🕓" : " ✓✓") : "";
          return `<div class="${cls}">${md(m.text)}<span class="meta">${esc(m.time)}${tick}</span></div>`;
        }).join("") + (S.pg.busy ? `<div class="typing"><span></span><span></span><span></span></div>` : "")
      : `<div class="bubble system">Converse como se fosse o lead. Mensagens enviadas em sequência são agrupadas pelo debounce em 1 único turno.</div>`;
    body.scrollTop = body.scrollHeight;
  }

  function pgEnqueue(text) {
    const now = Date.now();
    if (!S.pg.queue.length) S.pg.firstAt = now;
    S.pg.lastAt = now;
    S.pg.queue.push(text);
    pgChat().push({ role: "lead", text, time: nowTime(), pending: true });
    renderChat();
    startDebounce();
  }

  function startDebounce() {
    $("#pg-debounce").classList.add("show");
    if (S.pg.timer) return;
    S.pg.timer = setInterval(() => {
      const silence = Math.max(1, Number($("#pg-silence").value) || 4) * 1000;
      const t = tenantSummary($("#pg-tenant").value);
      const maxWin = (t?.debounce_max_window_seconds ?? S.overview?.debounce?.max_window_seconds ?? 10) * 1000;
      const deadline = Math.min(S.pg.lastAt + silence, S.pg.firstAt + maxWin);
      const remaining = deadline - Date.now();
      $("#pg-debounce-fill").style.width = `${Math.max(0, Math.min(100, (remaining / silence) * 100))}%`;
      $("#pg-debounce-label").textContent = `⏳ Agrupando ${S.pg.queue.length} mensagem(ns) · envio em ${(Math.max(0, remaining) / 1000).toFixed(1)}s`;
      if (remaining <= 0 && !S.pg.busy) pgFlush();
    }, 100);
  }

  function stopDebounce() {
    clearInterval(S.pg.timer);
    S.pg.timer = null;
    $("#pg-debounce").classList.remove("show");
  }

  async function pgFlush(messagesOverride) {
    const messages = messagesOverride || S.pg.queue.splice(0);
    if (!messagesOverride) stopDebounce();
    if (!messages.length) return;
    pgChat().forEach((m) => { m.pending = false; });
    S.pg.busy = true;
    renderChat();
    const body = {
      tenant_id: $("#pg-tenant").value,
      engine: $("#pg-engine").value,
      phone: $("#pg-phone").value.replace(/\D/g, ""),
      name: $("#pg-name").value.trim() || null,
      messages,
    };
    $("#xray-status").className = "chip chip-info";
    $("#xray-status").textContent = "processando…";
    const r = await api("POST", "/api/admin/playground/simulate", body);
    S.pg.busy = false;
    if (!r.ok) {
      pgChat().push({ role: "system", text: `🔴 Erro: ${r.error}` });
      renderChat();
      renderXrayError(r);
      toast("err", "Erro ao processar o turno", r.error, r.ms);
      if (S.pg.queue.length) startDebounce();
      return;
    }
    const t = r.data;
    if (t.reply_text) pgChat().push({ role: "ia", text: t.reply_text, time: nowTime(), handoff: t.status === "handoff" });
    if (t.status === "silenced") pgChat().push({ role: "system", text: `🔇 IA silenciada (${t.silence_reason || "humano ativo"}) — use 🗑️ Limpar para recomeçar.` });
    if (t.status === "handoff") pgChat().push({ role: "system", text: `🟡 Handoff humano acionado (${t.handoff_reason}). IA pausada para este contato.` });
    renderChat();
    renderXray(t);
    const titles = { success: "🟢 Turno processado com sucesso", handoff: "🟡 Handoff acionado", silenced: "🔇 IA silenciada" };
    toast("ok", titles[t.status] || "Turno processado", `${t.batch_size} mensagem(ns) → 1 turno · ${t.tools.length} ferramenta(s)`, r.ms);
    if (S.pg.queue.length) startDebounce();
  }

  $("#pg-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const input = $("#pg-input");
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    if ($("#pg-immediate").checked && !S.pg.busy && !S.pg.queue.length) {
      pgChat().push({ role: "lead", text, time: nowTime() });
      pgFlush([text]);
    } else pgEnqueue(text);
  });
  $("#btn-pg-flush").addEventListener("click", () => { if (!S.pg.busy) pgFlush(); });

  async function pgReset(silent = false) {
    stopDebounce();
    S.pg.queue = [];
    const tenant = $("#pg-tenant").value;
    const phone = $("#pg-phone").value.replace(/\D/g, "");
    const r = await api("DELETE", `/api/admin/playground/conversations/${encodeURIComponent(tenant)}/${encodeURIComponent(phone)}`);
    S.pg.chats[pgKey()] = [];
    renderChat();
    $("#xray").innerHTML = `<div class="empty">Conversa resetada. Envie uma nova mensagem.</div>`;
    $("#xray-status").className = "chip";
    $("#xray-status").textContent = "aguardando";
    if (!silent) toast(r.ok ? "ok" : "err", r.ok ? "Conversa limpa e memória resetada" : "Falha ao resetar", r.ok ? "" : r.error, r.ms);
    return r;
  }
  $("#btn-pg-reset").addEventListener("click", () => pgReset());

  const SCENARIOS = {
    mattress: { tenant: "companhia-dos-sonhos", msgs: ["Olá, boa tarde", "Queria ver um colchão de casal com molas ensacadas", "Vocês entregam em Belém?"] },
    booking: { tenant: "companhia-dos-sonhos", msgs: ["Oi! Quero agendar uma visita à loja", "Pode ser amanhã?"] },
    discount: { tenant: "companhia-dos-sonhos", msgs: ["Vi o colchão de casal de molas ensacadas", "Consegue mais desconto? Faz mais barato que eu fecho hoje"] },
    emergency: { tenant: "vizzu", msgs: ["Oi", "Não aguento mais viver, estou pensando em me machucar"] },
  };
  $$("[data-scenario]").forEach((btn) => btn.addEventListener("click", async () => {
    const sc = SCENARIOS[btn.dataset.scenario];
    if (!tenantSummary(sc.tenant)) { toast("err", "Cliente do cenário não encontrado", sc.tenant); return; }
    $("#pg-tenant").value = sc.tenant;
    syncPlaygroundTenant();
    await pgReset(true);
    for (const m of sc.msgs) { pgEnqueue(m); await sleep(450); }
  }));

  function renderXrayError(r) {
    $("#xray-status").className = "chip chip-err";
    $("#xray-status").textContent = "🔴 erro";
    $("#xray").innerHTML = `<div class="notice notice-warn">🔴 ${esc(r.error)} <span class="mono small">(${esc(r.ms)} ms)</span></div>`;
  }

  function renderXray(t) {
    const statusMap = {
      success: ["chip-ok", "🟢 Turno processado com sucesso"],
      handoff: ["chip-warn", "🟡 Handoff acionado"],
      silenced: ["chip-info", "🔇 IA silenciada"],
    };
    const [cls, label] = statusMap[t.status] || ["chip-err", "🔴 Erro"];
    $("#xray-status").className = `chip ${cls}`;
    $("#xray-status").textContent = label;
    const attrs = Object.entries(t.lead.attributes || {});
    const tools = t.tools.length
      ? t.tools.map((tool, i) => `
        <details class="tool" ${i === 0 ? "open" : ""}>
          <summary><span class="chip ${tool.ok ? "chip-ok" : tool.requires_human ? "chip-warn" : "chip-err"}">${esc(tool.status)}</span>
            <span class="mono">${esc(tool.tool)}</span>${tool.requires_human ? ` <span class="chip chip-warn">requer humano</span>` : ""}</summary>
          <div class="tool-body">
            <small class="muted">Argumentos enviados pelo LLM</small>
            <pre class="json">${jsonHtml(tool.args ?? "(executada pelo orquestrador)")}</pre>
            <small class="muted">JSON retornado pela ferramenta</small>
            <pre class="json">${jsonHtml(tool.result)}</pre>
          </div>
        </details>`).join("")
      : `<div class="empty">Nenhuma ferramenta chamada neste turno (descoberta/triagem).</div>`;
    const jev = t.jev_decision;
    $("#xray").innerHTML = `
      <div class="kv">
        <div><small>Turn ID</small><strong class="mono">${esc(t.turn_id)}</strong></div>
        <div><small>Latência total</small><strong>${esc(t.latency_ms)} ms</strong></div>
        <div><small>Motor</small><strong>${t.engine === "gemini" ? "✨ Gemini" : "⚡ Determinístico"}</strong></div>
        <div><small>Estágio LangGraph</small><strong>${esc(t.stage)}</strong></div>
        <div><small>Intenção (JEV)</small><strong>${esc(t.intent)}</strong></div>
        <div><small>Lote agrupado</small><strong>${esc(t.batch_size)} msg → 1 turno</strong></div>
      </div>
      ${t.handoff_reason ? `<div class="notice notice-warn">🤝 Handoff: <strong>${esc(t.handoff_reason)}</strong> — IA pausada para este contato (nota de CRM não é enviada no Playground).</div>` : ""}
      ${jev ? `<div class="notice notice-info">🧠 JEV: ${esc(jev.summary || "")} <span class="muted">(confiança ${esc(Math.round((jev.confidence ?? 0) * 100))}%)</span></div>` : ""}
      <div><h3>🔧 Ferramentas executadas (${t.tools.length})</h3><div class="list">${tools}</div></div>
      <div><h3>🧾 Atributos extraídos do lead</h3>
        ${attrs.length ? `<div class="chips">${attrs.map(([k, v]) => `<span class="chip">${esc(k)}: <strong>${esc(v)}</strong></span>`).join("")}</div>` : `<div class="muted small">Nenhum atributo ainda.</div>`}
      </div>
      <div><h3>🏷️ Tags aplicadas · status <span class="chip">${esc(t.lead.status)}</span></h3>
        ${t.lead.tags.length ? `<div class="chips">${t.lead.tags.map((x) => `<span class="chip chip-info">${esc(x)}</span>`).join("")}</div>` : `<div class="muted small">Sem tags.</div>`}
      </div>`;
  }

  // ================================================================== ABA 3 — CLIENTES
  async function loadTenant(id, { silent = true } = {}) {
    if (!id) { $("#tn-editor").innerHTML = `<div class="empty">Nenhum cliente cadastrado.</div>`; return; }
    if (S.dirty && !confirm("Há alterações não salvas. Descartar e carregar outro cliente?")) {
      $("#tn-select").value = S.tenant?.tenant?.tenant_id || id;
      return;
    }
    const r = await api("GET", `/api/admin/tenants/${encodeURIComponent(id)}`);
    if (!r.ok) { toast("err", "Falha ao carregar cliente", r.error, r.ms); return; }
    S.tenant = r.data;
    S.draft = clone(r.data);
    setDirty(false);
    renderTenant();
    if (!silent) toast("ok", "Cliente recarregado", S.draft.tenant.display_name, r.ms);
  }
  $("#tn-select").addEventListener("change", (ev) => loadTenant(ev.target.value));
  $("#btn-tn-reload").addEventListener("click", () => { S.dirty = false; loadTenant($("#tn-select").value, { silent: false }); });
  $("#tn-json-mode").addEventListener("change", renderTenant);

  function setDirty(v) {
    S.dirty = v;
    const bar = $("#tn-savebar");
    bar.classList.toggle("dirty", v);
    bar.classList.toggle("show", currentTab() === "tenants" && !!S.draft);
    $("#tn-dirty").textContent = v ? "● Alterações não salvas" : "Sem alterações pendentes";
  }

  // Binding declarativo: data-path="tenant.display_name" data-type="str|str?|int|int?|num?|money|money?|bool|csv|hours|del"
  function getPath(obj, path) { return path.split(".").reduce((o, k) => (o == null ? undefined : o[k]), obj); }
  function setPath(obj, path, value) {
    const keys = path.split(".");
    let o = obj;
    keys.slice(0, -1).forEach((k, i) => {
      if (o[k] == null) o[k] = /^\d+$/.test(keys[i + 1]) ? [] : {};
      o = o[k];
    });
    const last = keys[keys.length - 1];
    if (value === undefined) delete o[last]; else o[last] = value;
  }
  function convert(el) {
    const t = el.dataset.type || "str";
    const v = el.type === "checkbox" ? el.checked : el.value;
    switch (t) {
      case "bool": return !!v;
      case "str?": return String(v).trim() || null;
      case "del": return String(v).trim() || undefined;
      case "int": return v === "" ? null : parseInt(v, 10);
      case "int?": return v === "" ? null : parseInt(v, 10);
      case "num?": return v === "" ? null : Number(String(v).replace(",", "."));
      case "money": return parseMoney(v) ?? "0.00";
      case "money?": return parseMoney(v);
      case "csv": return String(v).split(",").map((x) => x.trim()).filter(Boolean);
      case "hours": { const list = String(v).split(",").map((x) => x.trim()).filter(Boolean); return list.length ? list : undefined; }
      default: return v;
    }
  }
  const val = (path) => getPath(S.draft, path);
  const inp = (path, type = "str", attrs = "") => `<input data-path="${esc(path)}" data-type="${type}" value="${esc(Array.isArray(val(path)) ? val(path).join(", ") : val(path) ?? "")}" ${attrs}>`;
  const chk = (path, label, attrs = "") => `<label class="check"><input type="checkbox" data-path="${esc(path)}" data-type="bool" ${val(path) ? "checked" : ""} ${attrs}> ${esc(label)}</label>`;
  const sel = (path, options, attrs = "") => `<select data-path="${esc(path)}" ${attrs}>${options.map((o) => `<option value="${esc(o)}" ${val(path) === o ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
  const selPairs = (path, pairs, defaultVal = "", attrs = "") => {
    const cur = val(path) || defaultVal;
    return `<select data-path="${esc(path)}" ${attrs}>${pairs.map(([v, lbl]) => `<option value="${esc(v)}" ${cur === v ? "selected" : ""}>${esc(lbl)}</option>`).join("")}</select>`;
  };
  const field = (label, control, cls = "") => `<label class="field ${cls}"><span>${esc(label)}</span>${control}</label>`;

  function splitDelayUnit(mins) {
    const m = Math.max(1, Number(mins) || 120);
    if (m >= 1440 && m % 1440 === 0) return { num: m / 1440, unit: "days", human: `${m / 1440} dia(s)` };
    if (m >= 60 && m % 60 === 0) return { num: m / 60, unit: "hours", human: `${m / 60} hora(s)` };
    return { num: m, unit: "minutes", human: `${m} min` };
  }

  function ensureFollowupSteps(t) {
    if (!Array.isArray(t.followup_steps) || !t.followup_steps.length) {
      t.followup_steps = clone(DEFAULT_FOLLOWUP_STEPS);
    }
    return t.followup_steps;
  }

  function synonymsText() {
    return Object.entries(val("catalog.synonyms") || {}).map(([k, list]) => `${k}: ${(list || []).join(", ")}`).join("\n");
  }

  function renderTenant() {
    const d = S.draft;
    if (!d) return;
    if ($("#tn-json-mode").checked) {
      $("#tn-editor").innerHTML = `<article class="card"><div class="card-head"><h2>Editor JSON avançado</h2><small class="muted">Validado pelo backend (Pydantic) ao salvar</small></div>
        <textarea id="tn-json" class="mono" rows="30" spellcheck="false">${esc(JSON.stringify(d, null, 2))}</textarea>
        <div id="tn-json-error" class="notice notice-warn hidden"></div></article>`;
      return;
    }
    const t = d.tenant;
    const crmBlock = t.crm_provider === "chatwoot" ? `
        <div class="form-grid cols-4">
          ${field("Chatwoot account_id", inp("crm.chatwoot.account_id", "int", 'type="number" min="1"'))}
          ${field("inbox_id", inp("crm.chatwoot.inbox_id", "int?", 'type="number" min="1" placeholder="opcional"'))}
          ${field("Time de handoff (team_id)", inp("crm.chatwoot.handoff_team_id", "int?", 'type="number" min="1" placeholder="opcional"'))}
          ${field("Agente de handoff (assignee_id)", inp("crm.chatwoot.handoff_assignee_id", "int?", 'type="number" min="1" placeholder="opcional"'))}
        </div>
        <div class="chips" style="margin-top:10px">${chk("crm.chatwoot.assignee_means_human", "Atendente atribuído pausa a IA (assignee_means_human)")}${chk("crm.chatwoot.create_missing", "Criar contato/conversa se não existir")}</div>`
      : t.crm_provider === "ghl" ? `
        <div class="form-grid cols-4">
          ${field("GHL location_id", inp("crm.ghl.location_id", "str?"))}
          ${field("Campo do token (.env)", inp("crm.ghl.token_setting"))}
          ${field("Campo da location (.env)", inp("crm.ghl.location_setting"))}
          ${field("Usuário de handoff", inp("crm.ghl.handoff_user_id", "str?", 'placeholder="opcional"'))}
        </div>` : `<p class="muted small">Sem CRM: o handoff pausa a IA e avisa o lead, sem nota privada.</p>`;

    const items = d.catalog?.items || [];
    const catalogRows = items.length ? items.map((it, i) => {
      const p = it.price || {};
      const pct = p.amount && p.cash_amount ? Math.round((1 - Number(p.cash_amount) / Number(p.amount)) * 1000) / 10 : null;
      return `<tr>
        <td class="mono">${esc(it.item_id)}</td>
        <td><strong>${esc(it.name)}</strong><div class="muted small">${esc(it.category)}</div></td>
        <td class="num price">${p.amount == null ? `<span class="chip chip-warn">sob consulta</span>` : esc(brl(p.amount))}</td>
        <td class="num">${p.amount != null && (p.max_installments || 1) > 1 ? `${esc(p.max_installments)}x ${p.interest_free === false ? "c/ juros" : "s/ juros"}` : "—"}</td>
        <td class="num">${p.cash_amount != null ? `${esc(brl(p.cash_amount))}${pct ? `<div class="muted small">-${esc(pct)}%</div>` : ""}` : "—"}</td>
        <td>${it.in_stock === false ? `<span class="chip chip-err">sem estoque</span>` : `<span class="chip chip-ok">em estoque</span>`}${it.active === false ? ` <span class="chip">inativo</span>` : ""}</td>
        <td class="actions"><button class="btn btn-sm" data-act="edit-item" data-i="${i}">✏️ Editar</button> <button class="btn btn-sm btn-danger" data-act="del-item" data-i="${i}">🗑️</button></td>
      </tr>`;
    }).join("") : `<tr><td colspan="7"><div class="empty">Catálogo vazio. Adicione o primeiro produto/serviço.</div></td></tr>`;

    const zones = d.delivery?.zones || [];
    const zoneRows = zones.map((z, i) => `<tr>
        <td>${inp(`delivery.zones.${i}.cities`, "csv", 'placeholder="Belém, Ananindeua"')}</td>
        <td style="width:120px">${inp(`delivery.zones.${i}.fee`, "money", 'inputmode="decimal" placeholder="0 = Grátis"')}</td>
        <td style="width:90px">${inp(`delivery.zones.${i}.lead_time_days_min`, "int", 'type="number" min="0"')}</td>
        <td style="width:90px">${inp(`delivery.zones.${i}.lead_time_days_max`, "int", 'type="number" min="0"')}</td>
        <td style="width:110px"><label class="check"><input type="checkbox" data-path="delivery.zones.${i}.business_days" data-type="bool" ${z.business_days !== false ? "checked" : ""}> úteis</label></td>
        <td>${inp(`delivery.zones.${i}.notes`, "str?", 'placeholder="observação"')}</td>
        <td class="actions"><button class="btn btn-sm btn-danger" data-act="del-zone" data-i="${i}">🗑️</button></td>
      </tr>`).join("") || `<tr><td colspan="7"><div class="empty">Nenhuma cidade atendida cadastrada.</div></td></tr>`;

    const cal = d.calendar;
    const calendarBlock = cal ? `
        <div class="table-wrap"><table class="table">
          <thead><tr><th>service_id</th><th>Nome do serviço</th><th>Duração (min)</th><th>Capacidade</th><th>Descrição</th><th></th></tr></thead>
          <tbody>${(cal.services || []).map((s, i) => `<tr>
            <td style="width:170px">${inp(`calendar.services.${i}.service_id`, "str", 'placeholder="visita_loja"')}</td>
            <td>${inp(`calendar.services.${i}.name`)}</td>
            <td style="width:110px">${inp(`calendar.services.${i}.duration_minutes`, "int", 'type="number" min="5" max="480"')}</td>
            <td style="width:100px">${inp(`calendar.services.${i}.capacity`, "int", 'type="number" min="1" max="50"')}</td>
            <td>${inp(`calendar.services.${i}.description`)}</td>
            <td class="actions"><button class="btn btn-sm btn-danger" data-act="del-service" data-i="${i}">🗑️</button></td>
          </tr>`).join("")}</tbody></table></div>
        <button class="btn btn-sm" data-act="add-service" style="margin-top:8px">+ Serviço agendável</button>
        <div class="form-grid cols-4">
          ${WEEKDAYS.map(([k, label]) => field(`${label} (HH:MM-HH:MM, separados por vírgula)`, inp(`calendar.working_hours.${k}`, "hours", 'placeholder="fechado"'))).join("")}
          ${field("Intervalo entre horários (min)", inp("calendar.slot_step_minutes", "int", 'type="number" min="5" max="240"'))}
          ${field("Antecedência mínima (min)", inp("calendar.min_notice_minutes", "int", 'type="number" min="0"'))}
          ${field("Horizonte (dias)", inp("calendar.horizon_days", "int", 'type="number" min="1" max="120"'))}
          ${field("Datas bloqueadas (AAAA-MM-DD)", inp("calendar.blackout_dates", "csv", 'placeholder="2026-12-25, 2027-01-01"'))}
        </div>
        <button class="btn btn-sm btn-danger" data-act="disable-calendar" style="margin-top:12px">Desativar agenda</button>`
      : `<div class="empty">Agenda desativada para este cliente. <button class="btn btn-sm btn-primary" data-act="enable-calendar">Ativar agenda</button></div>`;

    const planTier = t.plan_tier || "pro";
    const planSpec = PLAN_SPECS[planTier] || PLAN_SPECS.pro;
    const fSteps = ensureFollowupSteps(t);
    const catCount = items.length;
    const catMax = planSpec.max_catalog_items;
    const catBadge = catMax ? `${catCount} / ${catMax} itens (${catCount > catMax ? "⚠️ acima do limite do plano" : "dentro do limite"})` : `${catCount} itens (Catálogo Ilimitado ✅)`;

    const planSummaryCard = `
      <div class="notice ${planTier === "pro" ? "notice-info" : ""}" style="margin:12px 0;padding:14px;border-radius:12px">
        <div class="row-between" style="flex-wrap:wrap;gap:8px;margin-bottom:8px">
          <div>
            <strong>💎 ${esc(planSpec.name)}</strong> — <span class="mono">${esc(planSpec.price)}</span>
            <span class="muted small">(${esc(planSpec.setup)} · ${esc(planSpec.subtitle)})</span>
          </div>
          <div class="chips">
            <span class="chip chip-info">📱 Até ${esc(planSpec.max_whatsapp_numbers)} WhatsApp(s)</span>
            <span class="chip chip-ok">💬 Franquia: ${esc((t.monthly_turn_limit || planSpec.monthly_turn_limit).toLocaleString("pt-BR"))} conversas/mês</span>
            <span class="chip ${catMax && catCount > catMax ? "chip-err" : "chip-info"}">🛍️ ${esc(catBadge)}</span>
            <span class="chip ${planSpec.followup_allowed ? "chip-ok" : "chip-warn"}">${planSpec.followup_allowed ? `🔄 Régua de Follow-up liberada (até ${planSpec.max_followup_steps} etapas)` : "🔒 Sem Follow-up Automático no Essencial"}</span>
          </div>
        </div>
        <div class="chips">${planSpec.features.map((f) => `<span class="chip">${esc(f)}</span>`).join("")}</div>
      </div>`;

    const followupRows = fSteps.map((st, idx) => {
      const du = splitDelayUnit(st.delay_minutes);
      return `<tr data-fu-row="${idx}">
        <td style="width:120px"><strong>${idx + 1}º Toque</strong><div class="muted small">${esc(du.human)} após anterior</div></td>
        <td style="width:220px">
          <div class="row-between" style="gap:6px">
            <input type="number" min="1" max="365" value="${esc(du.num)}" data-fu-num="${idx}" style="width:85px">
            <select data-fu-unit="${idx}">
              <option value="minutes" ${du.unit === "minutes" ? "selected" : ""}>Minutos</option>
              <option value="hours" ${du.unit === "hours" ? "selected" : ""}>Horas</option>
              <option value="days" ${du.unit === "days" ? "selected" : ""}>Dias</option>
            </select>
          </div>
        </td>
        <td style="width:260px">
          <select data-path="tenant.followup_steps.${idx}.strategy">
            ${FOLLOWUP_STRATEGIES.map(([valKey, lbl]) => `<option value="${esc(valKey)}" ${st.strategy === valKey ? "selected" : ""}>${esc(lbl)}</option>`).join("")}
          </select>
        </td>
        <td>
          <input data-path="tenant.followup_steps.${idx}.custom_message" data-type="str?" value="${esc(st.custom_message || "")}" placeholder="Automático conforme nicho/produto (ou digite usando {nome} e {empresa})">
        </td>
        <td class="actions"><button class="btn btn-sm btn-danger" data-act="del-fu-step" data-i="${idx}" title="Remover etapa">🗑️</button></td>
      </tr>`;
    }).join("");

    const followupCard = `
      <article class="card section">
        <div class="card-head">
          <div>
            <h2>🔄 Régua de Follow-up Progressiva (Recuperação de Vendas)</h2>
            <small class="muted">Cada etapa aguarda o tempo configurado após o envio anterior (ex.: 2h → +1 dia → +3 dias → +7 dias). Só dispara se o lead parar de responder e a IA não estiver pausada.</small>
          </div>
          <div class="chips">
            <button class="btn btn-sm" data-act="reset-fu-steps">↺ Padrão (2h → 1d → 3d → 7d)</button>
            <button class="btn btn-sm btn-primary" data-act="test-fu-dryrun">⚡ Simular Régua Agora</button>
          </div>
        </div>
        ${!planSpec.followup_allowed ? `<div class="notice notice-warn" style="margin-bottom:12px">🔒 <strong>Recurso bloqueado no Plano Essencial:</strong> A Régua de Follow-up Automático está disponível a partir do <strong>Plano Pro Vendas (R$ 1.690/mês)</strong>. Altere o Plano Comercial acima para ativar.</div>` : ""}
        ${planSpec.followup_allowed && !t.followup_enabled ? `<div class="notice notice-warn" style="margin-bottom:12px">⏸️ <strong>Régua desligada para este cliente.</strong> As etapas abaixo só são enviadas depois que você marcar a opção “Ativar Régua de Follow-up Automático” e salvar.</div>` : ""}
        ${planSpec.followup_allowed && t.followup_enabled ? `<div class="notice notice-info" style="margin-bottom:12px">🟢 <strong>Régua ativa.</strong> O servidor verifica a cada minuto e envia pelo mesmo número em que o lead conversou. Acompanhe a fila em <a href="#engine" data-goto-tab="engine">Motor do Agente</a>.</div>` : ""}
        <div class="chips" style="margin-bottom:12px">
          ${chk("tenant.followup_enabled", "Ativar Régua de Follow-up Automático para leads inativos deste cliente", !planSpec.followup_allowed ? "disabled" : "")}
        </div>
        <div class="table-wrap">
          <table class="table">
            <thead><tr><th>Etapa</th><th>Tempo de espera após último envio</th><th>Objetivo / Estratégia</th><th>Mensagem personalizada (opcional)</th><th></th></tr></thead>
            <tbody>${followupRows}</tbody>
          </table>
        </div>
        <div class="row-between" style="margin-top:10px">
          <button class="btn btn-sm btn-success" data-act="add-fu-step" ${!planSpec.followup_allowed || fSteps.length >= (planSpec.max_followup_steps || 6) ? "disabled" : ""}>+ Adicionar Etapa na Régua</button>
          <span id="fu-dryrun-result" class="muted small"></span>
        </div>
      </article>`;

    const segLabel = (SEGMENT_OPTIONS.find(([v]) => v === t.segment) || [t.segment, t.segment])[1];
    const l3Len = (t.custom_instructions || "").length;
    const promptCard = `
      <article class="card section">
        <div class="card-head"><h2>🧠 Prompt deste cliente (camadas 2 e 3)</h2><button class="btn btn-primary btn-sm" data-act="open-prompt">🧠 Ver e editar prompt</button></div>
        <div class="prompt-card-layers prompt-card-layers-2">
          <div class="layer"><strong>✏️ Camada 2 · Playbook</strong><span>${t.playbook_override ? "Personalizado para este cliente" : `Padrão do segmento: ${esc(segLabel)}`}</span></div>
          <div class="layer"><strong>✏️ Camada 3 · Instruções da empresa</strong><span>${l3Len ? `${l3Len.toLocaleString("pt-BR")} caracteres cadastrados` : "Nenhuma instrução cadastrada"}</span></div>
        </div>
        <p class="muted small" style="margin-top:10px">As alterações pedem confirmação, valem na hora para as conversas reais e a versão anterior fica no histórico. As regras globais (Kernel) ficam na aba <a href="#engine" data-goto-tab="engine">Motor do Agente</a>.</p>
      </article>`;

    $("#tn-editor").innerHTML = `
      <article class="card">
        <div class="card-head"><h2>⚙️ Configurações gerais &amp; Plano Comercial</h2>${d.demo_data ? `<span class="chip tag-demo">DADOS DE DEMONSTRAÇÃO</span>` : `<span class="chip chip-ok">dados reais</span>`}</div>
        <div class="form-grid cols-4">
          ${field("tenant_id", `<input value="${esc(t.tenant_id)}" disabled>`)}
          ${field("Nome de exibição", inp("tenant.display_name"), "span-2")}
          ${field("Segmento / Categoria", selPairs("tenant.segment", SEGMENT_OPTIONS, "generic"))}
          ${field("Nicho / Especialidade Livre", inp("tenant.niche_name", "str?", 'placeholder="ex.: Energia Solar, implantes, móveis planejados"'), "span-2")}
          ${field("Plano Comercial", selPairs("tenant.plan_tier", PLAN_OPTIONS, "pro", 'data-act="plan-tier"'), "span-2")}
          ${field("Franquia mensal (conversas/mês)", inp("tenant.monthly_turn_limit", "int?", 'type="number" min="100" placeholder="2500"'))}
          ${field("Fuso horário", inp("tenant.timezone", "str", 'placeholder="America/Belem"'))}
          ${field("Provedor WhatsApp", sel("tenant.whatsapp_provider", ["waha", "astracalls"]))}
          ${field("Sessão WhatsApp", inp("tenant.whatsapp_session_id", "str?", 'placeholder="ex.: Danyllo"'))}
          ${field("CRM Nativo", sel("tenant.crm_provider", ["chatwoot", "ghl", "none"], 'data-act="crm-provider"'))}
          ${field("Modelo de IA", selPairs("tenant.llm_model", LLM_MODEL_OPTIONS, "gemini-3.8-flash"), "span-2")}
          ${field("Chave HMAC / Secret do Webhook", inp("tenant.webhook_secret", "str?", 'placeholder="opcional (sobrescreve .env)"'), "span-2")}
          <label class="field span-2">
            <span>⏱️ Silêncio para fechar o lote (debounce): <strong id="tn-silence-val" class="mono">${esc(t.debounce_silence_seconds ?? S.overview?.debounce?.silence_seconds ?? 8)}s</strong></span>
            <input type="range" data-path="tenant.debounce_silence_seconds" data-type="num?" min="2" max="30" step="1" value="${esc(t.debounce_silence_seconds ?? S.overview?.debounce?.silence_seconds ?? 8)}">
          </label>
          <label class="field span-2">
            <span>⏳ Teto máximo da janela de espera: <strong id="tn-max-window-val" class="mono">${esc(t.debounce_max_window_seconds ?? S.overview?.debounce?.max_window_seconds ?? 20)}s</strong></span>
            <input type="range" data-path="tenant.debounce_max_window_seconds" data-type="num?" min="5" max="60" step="1" value="${esc(t.debounce_max_window_seconds ?? S.overview?.debounce?.max_window_seconds ?? 20)}">
          </label>
        </div>
        ${planSummaryCard}
        <div class="chips" style="margin-top:12px">${chk("tenant.active", "Cliente ativo (webhook atende)")}${chk("tenant.split_messages", "Ativar Envio Humanizado em Partes (picotar mensagens longas)")}${chk("demo_data", "Dados de demonstração (demo_data)")}</div>
        <div class="divider"></div>
        <h3>🔌 CRM (${esc(t.crm_provider)}) &amp; Conectores Externos via API (ERP / Bling / Asaas / n8n)</h3>
        ${crmBlock}
        <div class="form-grid cols-3" style="margin-top:12px">
          ${field("Webhook de Saída / API Externa (ERP, n8n, Make, Asaas)", inp("tenant.external_webhook_url", "str?", 'placeholder="https://seu-erp-ou-n8n.com/webhook/lobo"'), "span-2")}
          ${field("Token Bearer da API Externa (opcional)", inp("tenant.external_api_token", "str?", 'type="password" placeholder="Bearer token opcional"'))}
        </div>
      </article>

      ${promptCard}

      ${followupCard}

      <article class="card section">
        <div class="card-head"><h2>🛍️ Catálogo &amp; Preços</h2><button class="btn btn-success btn-sm" data-act="new-item">+ Novo Produto/Serviço</button></div>
        <div class="table-wrap"><table class="table">
          <thead><tr><th>Código</th><th>Nome / categoria</th><th class="num">Preço</th><th class="num">Parcelas</th><th class="num">À vista</th><th>Estoque</th><th></th></tr></thead>
          <tbody>${catalogRows}</tbody>
        </table></div>
        <div class="form-grid cols-3">
          ${field("Validade máxima do preço (dias)", inp("catalog.price_max_age_days", "int?", 'type="number" min="1" placeholder="sem validade"'))}
          <label class="field span-2"><span>Sinônimos de busca (um por linha — termo: sinônimo1, sinônimo2)</span>
            <textarea id="tn-synonyms" rows="4" class="mono" placeholder="casal: 138x188, duplo">${esc(synonymsText())}</textarea></label>
        </div>
      </article>

      <article class="card section">
        <div class="card-head"><h2>🚚 Entregas / Frete por cidade</h2><button class="btn btn-success btn-sm" data-act="add-zone">+ Adicionar cidade/zona</button></div>
        <div class="table-wrap"><table class="table">
          <thead><tr><th>Cidades</th><th>Frete (R$)</th><th>Prazo mín.</th><th>Prazo máx.</th><th>Dias</th><th>Observação</th><th></th></tr></thead>
          <tbody>${zoneRows}</tbody>
        </table></div>
      </article>

      <article class="card section">
        <div class="card-head"><h2>🏪 Informações do negócio</h2></div>
        <div class="form-grid cols-2">
          ${field("Endereço / localização", `<textarea data-path="business_info.location" data-type="del" rows="2">${esc(val("business_info.location") || "")}</textarea>`)}
          ${field("Horário de funcionamento", `<textarea data-path="business_info.hours" data-type="del" rows="2">${esc(val("business_info.hours") || "")}</textarea>`)}
          ${field("Garantia", `<textarea data-path="business_info.warranty" data-type="del" rows="2">${esc(val("business_info.warranty") || "")}</textarea>`)}
          ${field("Formas de pagamento", `<textarea data-path="business_info.payment_methods" data-type="del" rows="2">${esc(val("business_info.payment_methods") || "")}</textarea>`)}
        </div>
      </article>

      <article class="card section">
        <div class="card-head"><h2>📅 Agenda &amp; serviços agendáveis</h2></div>
        ${calendarBlock}
      </article>`;
  }

  const editor = $("#tn-editor");
  editor.addEventListener("input", (ev) => {
    const el = ev.target;
    if (el.id === "tn-json") {
      try { S.draft = JSON.parse(el.value); S.jsonError = null; $("#tn-json-error").classList.add("hidden"); }
      catch (e) { S.jsonError = e.message; const box = $("#tn-json-error"); box.textContent = `JSON inválido: ${e.message}`; box.classList.remove("hidden"); }
      setDirty(true);
      return;
    }
    if (el.id === "tn-synonyms") {
      const syn = {};
      el.value.split("\n").forEach((line) => {
        const [k, rest] = line.split(":");
        if (k && k.trim() && rest !== undefined) syn[k.trim()] = rest.split(",").map((x) => x.trim()).filter(Boolean);
      });
      setPath(S.draft, "catalog.synonyms", syn);
      setDirty(true);
      return;
    }
    if (el.dataset.fuNum !== undefined || el.dataset.fuUnit !== undefined) {
      const idx = Number(el.dataset.fuNum ?? el.dataset.fuUnit);
      const row = el.closest("tr[data-fu-row]");
      if (row) {
        const num = Math.max(1, parseInt(row.querySelector(`[data-fu-num="${idx}"]`).value, 10) || 1);
        const unit = row.querySelector(`[data-fu-unit="${idx}"]`).value;
        const mult = unit === "days" ? 1440 : unit === "hours" ? 60 : 1;
        const mins = Math.min(43200, num * mult);
        const steps = ensureFollowupSteps(S.draft.tenant);
        if (steps[idx]) {
          steps[idx].delay_minutes = mins;
          if (idx === 0) S.draft.tenant.followup_delay_minutes = mins;
        }
        setDirty(true);
      }
      return;
    }
    if (!el.dataset.path) return;
    if (el.dataset.path === "tenant.debounce_silence_seconds") {
      const lbl = $("#tn-silence-val");
      if (lbl) lbl.textContent = `${el.value}s`;
      const sNum = Number(el.value);
      const curMax = Number(S.draft.tenant.debounce_max_window_seconds ?? S.overview?.debounce?.max_window_seconds ?? 20);
      if (curMax <= sNum) {
        const nextMax = Math.min(60, sNum + 5);
        S.draft.tenant.debounce_max_window_seconds = nextMax;
        const maxInput = editor.querySelector('[data-path="tenant.debounce_max_window_seconds"]');
        if (maxInput) maxInput.value = nextMax;
        const maxLbl = $("#tn-max-window-val");
        if (maxLbl) maxLbl.textContent = `${nextMax}s`;
      }
    } else if (el.dataset.path === "tenant.debounce_max_window_seconds") {
      const lbl = $("#tn-max-window-val");
      if (lbl) lbl.textContent = `${el.value}s`;
    }
    setPath(S.draft, el.dataset.path, convert(el));
    setDirty(true);
  });
  editor.addEventListener("change", (ev) => {
    const el = ev.target;
    if (el.dataset.act === "crm-provider") {
      if (el.value === "chatwoot" && !S.draft.crm?.chatwoot) setPath(S.draft, "crm.chatwoot", { account_id: 1, assignee_means_human: false });
      if (el.value === "ghl" && !S.draft.crm?.ghl) setPath(S.draft, "crm.ghl", { token_setting: "ghl_vizzu_api_token", location_setting: "ghl_vizzu_location_id" });
      renderTenant();
    } else if (el.dataset.act === "plan-tier") {
      const spec = PLAN_SPECS[el.value] || PLAN_SPECS.pro;
      S.draft.tenant.plan_tier = el.value;
      S.draft.tenant.monthly_turn_limit = spec.monthly_turn_limit;
      if (!spec.followup_allowed) {
        S.draft.tenant.followup_enabled = false;
      }
      setDirty(true);
      renderTenant();
      toast("ok", `💎 ${spec.name} selecionado`, `${spec.price} · Franquia ajustada para ${spec.monthly_turn_limit.toLocaleString("pt-BR")} conversas/mês`);
    } else if (el.dataset.fuUnit !== undefined) {
      renderTenant();
    }
  });
  editor.addEventListener("click", async (ev) => {
    const btn = ev.target.closest("button[data-act]");
    if (!btn) return;
    const i = Number(btn.dataset.i);
    const d = S.draft;
    switch (btn.dataset.act) {
      case "add-fu-step": {
        const steps = ensureFollowupSteps(d.tenant);
        const nextNum = steps.length + 1;
        steps.push({ step: nextNum, delay_minutes: 4320, label: `${nextNum}º Toque`, strategy: "objection_break", custom_message: null });
        d.tenant.followup_max_attempts = steps.length;
        break;
      }
      case "del-fu-step": {
        const steps = ensureFollowupSteps(d.tenant);
        if (steps.length <= 1) { toast("err", "A régua precisa de ao menos 1 etapa", "Para desligar, desmarque a opção de Follow-up Automático."); return; }
        steps.splice(i, 1);
        steps.forEach((s, idx) => { s.step = idx + 1; });
        d.tenant.followup_max_attempts = steps.length;
        break;
      }
      case "reset-fu-steps": {
        d.tenant.followup_steps = clone(DEFAULT_FOLLOWUP_STEPS);
        d.tenant.followup_delay_minutes = 120;
        d.tenant.followup_max_attempts = 4;
        toast("ok", "Régua restaurada", "Cadência progressiva: 2h → +1 dia → +3 dias → +7 dias");
        break;
      }
      case "test-fu-dryrun": {
        const r = await act(btn, "Simulação da Régua de Follow-up concluída", () => api("POST", "/api/admin/followup/run", { tenant_id: d.tenant.tenant_id, dry_run: true, force: true }));
        const outEl = $("#fu-dryrun-result");
        if (r.ok && outEl) {
          outEl.innerHTML = r.data.eligible_count > 0
            ? `🟢 <strong>${r.data.eligible_count} contato(s)</strong> receberiam follow-up agora: <em>"${esc(r.data.eligible[0].message.slice(0, 110))}…"</em>`
            : `ℹ️ <strong>${r.data.evaluated} conversa(s) avaliada(s)</strong> — nenhum lead aguardando retorno neste momento.`;
        }
        return;
      }
      case "add-zone": (d.delivery ||= { zones: [] }).zones.push({ cities: [], fee: "0.00", lead_time_days_min: 1, lead_time_days_max: 2, business_days: true }); break;
      case "del-zone": d.delivery.zones.splice(i, 1); break;
      case "add-service": d.calendar.services.push({ service_id: `servico_${d.calendar.services.length + 1}`, name: "Novo serviço", duration_minutes: 30, capacity: 1 }); break;
      case "del-service":
        if (d.calendar.services.length <= 1) { toast("err", "A agenda precisa de ao menos 1 serviço", "Use “Desativar agenda” para remover tudo."); return; }
        d.calendar.services.splice(i, 1); break;
      case "enable-calendar":
        d.calendar = { services: [{ service_id: "visita", name: "Visita / reunião", duration_minutes: 30, capacity: 1 }],
          working_hours: { mon: ["09:00-18:00"], tue: ["09:00-18:00"], wed: ["09:00-18:00"], thu: ["09:00-18:00"], fri: ["09:00-18:00"] },
          slot_step_minutes: 30, min_notice_minutes: 120, horizon_days: 14, blackout_dates: [] };
        break;
      case "disable-calendar": if (!confirm("Desativar a agenda deste cliente?")) return; d.calendar = null; break;
      case "new-item": openItem(null); return;
      case "open-prompt": openPrompt(); return;
      case "edit-item": openItem(i); return;
      case "del-item": {
        const it = d.catalog.items[i];
        if (!confirm(`Excluir "${it.name}" do catálogo?`)) return;
        d.catalog.items.splice(i, 1);
        setDirty(true);
        await saveTenant(null, `Item “${it.name}” excluído e aplicado`);
        return;
      }
      default: return;
    }
    setDirty(true);
    renderTenant();
  });

  async function saveTenant(btn, okTitle = "Cliente salvo e aplicado imediatamente") {
    if (!S.draft) return { ok: false };
    if (S.jsonError) { toast("err", "Corrija o JSON antes de salvar", S.jsonError); return { ok: false }; }
    const id = S.tenant.tenant.tenant_id;
    const r = await act(btn, okTitle, () => api("PUT", `/api/admin/tenants/${encodeURIComponent(id)}`, S.draft),
      { okMsg: "Validado (Pydantic), gravado no JSON e recarregado em memória.", errTitle: "Erro ao salvar cliente" });
    if (r.ok) {
      S.tenant = r.data.tenant;
      S.draft = clone(r.data.tenant);
      setDirty(false);
      const idx = S.tenants.findIndex((t) => t.tenant_id === id);
      if (idx >= 0) S.tenants[idx] = r.data.summary;
      fillTenantSelects();
      $("#tn-select").value = id;
      renderTenant();
    }
    return r;
  }
  $("#btn-tn-save").addEventListener("click", (ev) => saveTenant(ev.currentTarget));
  $("#btn-tn-discard").addEventListener("click", () => {
    S.draft = clone(S.tenant);
    S.jsonError = null;
    setDirty(false);
    renderTenant();
    toast("ok", "Alterações descartadas");
  });

  // ------------------------------------------------------------------ editor do System Prompt (camadas 2/3)
  const PR = { data: null, original: { l2: null, l3: null }, tenantId: null, pendingSave: null };
  const prNorm = (v) => (String(v ?? "").replace(/\r\n/g, "\n").trim() || null);

  function prDraft() {
    return {
      playbook_override: $("#pr-l2-custom").checked ? prNorm($("#pr-l2").value) : null,
      custom_instructions: prNorm($("#pr-l3").value),
    };
  }
  function prChanged() {
    const d = prDraft();
    return d.playbook_override !== PR.original.l2 || d.custom_instructions !== PR.original.l3;
  }
  function setPrTab(name) {
    $$("#dlg-prompt .subtab").forEach((b) => b.classList.toggle("active", b.dataset.prtab === name));
    $$("#dlg-prompt .prpane").forEach((p) => p.classList.toggle("active", p.dataset.prpane === name));
  }
  function updatePrCounts() {
    const max = PR.data?.max_layer_chars || 12000;
    const l2 = $("#pr-l2").value.length, l3 = $("#pr-l3").value.length;
    $("#pr-l2-count").textContent = `${l2.toLocaleString("pt-BR")} / ${max.toLocaleString("pt-BR")}`;
    $("#pr-l3-count").textContent = `${l3.toLocaleString("pt-BR")} / ${max.toLocaleString("pt-BR")}`;
    $("#pr-dirty").textContent = prChanged() ? "● Alterações não salvas no prompt" : "";
  }
  function syncPrL2() {
    const custom = $("#pr-l2-custom").checked;
    const ta = $("#pr-l2");
    ta.readOnly = !custom;
    if (!custom) ta.value = PR.data?.segment_default_playbook || "";
    $("#pr-l2-hint").textContent = custom
      ? "Playbook personalizado: vale somente para este cliente. Desmarque para voltar ao padrão do segmento."
      : `Mostrando o padrão do segmento "${PR.data?.segment || ""}" (somente leitura). Marque a opção acima para personalizar a partir dele.`;
    updatePrCounts();
  }
  function renderPrHistory(revs) {
    const box = $("#pr-history");
    if (!revs || !revs.length) { box.innerHTML = `<div class="empty">Nenhuma versão anterior salva ainda.</div>`; return; }
    box.innerHTML = revs.map((r, i) => `
      <div class="list-item">
        <div class="grow"><strong>${esc(r.saved_at ? new Date(r.saved_at).toLocaleString("pt-BR") : r.revision)}</strong>
          <div class="muted small">Camada 2: ${r.playbook_override ? `personalizada (${r.playbook_override.length} car.)` : "padrão do segmento"} · Camada 3: ${r.custom_instructions ? `${r.custom_instructions.length} car.` : "vazia"}</div></div>
        <button class="btn btn-sm" data-pr-rev="${i}">⤴ Carregar no editor</button>
      </div>`).join("");
  }
  function applyPromptState(state) {
    PR.data = state;
    PR.original = { l2: prNorm(state.playbook_override), l3: prNorm(state.custom_instructions) };
    $("#pr-l2-custom").checked = !!PR.original.l2;
    $("#pr-l2").value = PR.original.l2 || state.segment_default_playbook || "";
    $("#pr-l3").value = PR.original.l3 || "";
    $("#pr-compiled").textContent = state.compiled_preview || "";
    $("#pr-compiled-meta").textContent = state.compiled_chars ? `· ${state.compiled_chars.toLocaleString("pt-BR")} caracteres` : "";
    renderPrHistory(state.revisions);
    syncPrL2();
  }
  async function openPrompt() {
    if (!S.tenant) return;
    const id = S.tenant.tenant.tenant_id;
    const r = await api("GET", `/api/admin/tenants/${encodeURIComponent(id)}/prompt`);
    if (!r.ok) { toast("err", "Falha ao carregar o System Prompt", r.error, r.ms); return; }
    PR.tenantId = id;
    $("#pr-tenant-label").textContent = `· ${S.tenant.tenant.display_name} (${id})`;
    applyPromptState(r.data);
    setPrTab("l2");
    $("#dlg-prompt").showModal();
  }
  function closePrompt() {
    if (prChanged() && !confirm("Há alterações não salvas no prompt. Descartar?")) return;
    $("#dlg-prompt").close();
  }
  function lineDelta(before, after) {
    const a = new Set(String(before || "").split("\n").map((l) => l.trim()).filter(Boolean));
    const b = new Set(String(after || "").split("\n").map((l) => l.trim()).filter(Boolean));
    let added = 0, removed = 0;
    b.forEach((l) => { if (!a.has(l)) added++; });
    a.forEach((l) => { if (!b.has(l)) removed++; });
    return `+${added} / −${removed} linhas`;
  }

  $("#dlg-prompt").addEventListener("click", (ev) => {
    const tab = ev.target.closest("[data-prtab]");
    if (tab) { setPrTab(tab.dataset.prtab); return; }
    const rev = ev.target.closest("[data-pr-rev]");
    if (rev) {
      const r = PR.data?.revisions?.[Number(rev.dataset.prRev)];
      if (!r) return;
      $("#pr-l2-custom").checked = !!r.playbook_override;
      $("#pr-l2").value = r.playbook_override || PR.data.segment_default_playbook || "";
      $("#pr-l3").value = r.custom_instructions || "";
      syncPrL2();
      setPrTab("l2");
      toast("ok", "Versão carregada no editor", "Revise e clique em “Salvar alterações…” para aplicar.");
    }
  });
  $("#dlg-prompt").addEventListener("cancel", (ev) => { ev.preventDefault(); closePrompt(); });
  $("#pr-close").addEventListener("click", closePrompt);
  $("#pr-cancel").addEventListener("click", closePrompt);
  $("#pr-l2-custom").addEventListener("change", syncPrL2);
  $("#pr-l2").addEventListener("input", updatePrCounts);
  $("#pr-l3").addEventListener("input", updatePrCounts);
  $("#pr-l2-copy").addEventListener("click", () => {
    $("#pr-l2-custom").checked = true;
    $("#pr-l2").value = PR.data?.segment_default_playbook || "";
    syncPrL2();
    $("#pr-l2").focus();
  });
  $("#pr-preview").addEventListener("click", async (ev) => {
    const r = await act(ev.currentTarget, "Prévia do prompt atualizada",
      () => api("POST", `/api/admin/tenants/${encodeURIComponent(PR.tenantId)}/prompt/preview`, prDraft()),
      { okMsg: "Compilado com o rascunho (nada foi gravado)." });
    if (r.ok) {
      $("#pr-compiled").textContent = r.data.compiled_preview;
      $("#pr-compiled-meta").textContent = `· ${r.data.compiled_chars.toLocaleString("pt-BR")} caracteres (rascunho)`;
    }
  });
  $("#pr-save").addEventListener("click", () => {
    if (!prChanged()) { toast("ok", "Nada para salvar", "O prompt está igual à versão aplicada."); return; }
    const d = prDraft();
    const items = [];
    if (d.playbook_override !== PR.original.l2) {
      const from = PR.original.l2 ? "personalizado" : "padrão do segmento";
      const to = d.playbook_override ? `personalizado (${d.playbook_override.length.toLocaleString("pt-BR")} car.)` : "padrão do segmento";
      items.push(`<li><strong>Camada 2 · Playbook:</strong> ${esc(from)} → ${esc(to)} <span class="muted">(${esc(lineDelta(PR.original.l2 || PR.data.segment_default_playbook, d.playbook_override || PR.data.segment_default_playbook))})</span></li>`);
    }
    if (d.custom_instructions !== PR.original.l3) {
      items.push(`<li><strong>Camada 3 · Instruções:</strong> ${(PR.original.l3 || "").length.toLocaleString("pt-BR")} → ${(d.custom_instructions || "").length.toLocaleString("pt-BR")} caracteres <span class="muted">(${esc(lineDelta(PR.original.l3, d.custom_instructions))})</span></li>`);
    }
    $("#prc-summary").innerHTML = items.join("");
    $("#prc-expected").textContent = PR.tenantId;
    $("#prc-input").value = "";
    $("#prc-ok").disabled = true;
    PR.pendingSave = d;
    $("#dlg-prompt-confirm").showModal();
    $("#prc-input").focus();
  });
  $("#prc-input").addEventListener("input", (ev) => { $("#prc-ok").disabled = ev.target.value.trim() !== PR.tenantId; });
  $("#dlg-prompt-confirm").addEventListener("close", async () => {
    const dlg = $("#dlg-prompt-confirm");
    if (dlg.returnValue !== "confirm" || !PR.pendingSave) { PR.pendingSave = null; return; }
    dlg.returnValue = "";
    const body = { ...PR.pendingSave, confirm_tenant_id: $("#prc-input").value.trim() };
    PR.pendingSave = null;
    const r = await act($("#pr-save"), "System Prompt atualizado e aplicado",
      () => api("PUT", `/api/admin/tenants/${encodeURIComponent(PR.tenantId)}/prompt`, body),
      { okMsg: (data) => (data.backup_revision ? `Versão anterior salva no histórico (${data.backup_revision}).` : "Aplicado imediatamente."), errTitle: "Erro ao salvar o prompt" });
    if (!r.ok) return;
    applyPromptState(r.data.prompt);
    // Mantém o editor do cliente coerente (evita sobrescrever o prompt com um rascunho antigo)
    ["playbook_override", "custom_instructions"].forEach((k) => {
      if (S.tenant?.tenant) S.tenant.tenant[k] = r.data.tenant.tenant[k] ?? null;
      if (S.draft?.tenant) S.draft.tenant[k] = r.data.tenant.tenant[k] ?? null;
    });
    const idx = S.tenants.findIndex((t) => t.tenant_id === PR.tenantId);
    if (idx >= 0 && r.data.summary) S.tenants[idx] = r.data.summary;
    renderTenant();
  });

  // ------------------------------------------------------------------ modal item
  const F = (form, name) => form.elements.namedItem(name);
  let editingIndex = null;
  function openItem(index) {
    editingIndex = index;
    const it = index === null ? { item_id: "", name: "", category: "", price: { amount: null, max_installments: 1, interest_free: true }, in_stock: true, active: true }
      : clone(S.draft.catalog.items[index]);
    const f = $("#form-item");
    const p = it.price || {};
    $("#dlg-item-title").textContent = index === null ? "Novo produto / serviço" : `Editar ${it.name}`;
    F(f, "item_id").value = it.item_id || "";
    F(f, "name").value = it.name || "";
    F(f, "category").value = it.category || "";
    F(f, "tags").value = (it.tags || []).join(", ");
    F(f, "description").value = it.description || "";
    F(f, "attributes").value = Object.entries(it.attributes || {}).map(([k, v]) => `${k}: ${v}`).join("\n");
    F(f, "amount").value = p.amount ?? "";
    F(f, "on_request").checked = p.amount == null && index !== null;
    F(f, "max_installments").value = p.max_installments || 1;
    F(f, "interest_free").checked = p.interest_free !== false;
    F(f, "cash_amount").value = p.cash_amount ?? "";
    F(f, "cash_pct").value = p.amount && p.cash_amount ? (Math.round((1 - p.cash_amount / p.amount) * 1000) / 10) : "";
    F(f, "note").value = p.note || "";
    F(f, "in_stock").checked = it.in_stock !== false;
    F(f, "active").checked = it.active !== false;
    F(f, "variants").value = it.variants?.length ? JSON.stringify(it.variants, null, 2) : "";
    f.dataset.original = JSON.stringify(it);
    toggleOnRequest();
    $("#dlg-item").showModal();
  }
  function toggleOnRequest() {
    const f = $("#form-item");
    ["amount", "cash_pct", "cash_amount", "max_installments"].forEach((n) => { f[n].disabled = F(f, "on_request").checked; });
  }
  F($("#form-item"), "on_request").addEventListener("change", toggleOnRequest);
  F($("#form-item"), "cash_pct").addEventListener("input", () => {
    const f = $("#form-item");
    const amount = Number(parseMoney(F(f, "amount").value));
    const pct = Number(String(F(f, "cash_pct").value).replace(",", "."));
    F(f, "cash_amount").value = amount && pct > 0 && pct < 100 ? (Math.round(amount * (1 - pct / 100) * 100) / 100).toFixed(2) : "";
  });
  F($("#form-item"), "cash_amount").addEventListener("input", () => {
    const f = $("#form-item");
    const amount = Number(parseMoney(F(f, "amount").value));
    const cash = Number(parseMoney(F(f, "cash_amount").value));
    F(f, "cash_pct").value = amount && cash ? Math.round((1 - cash / amount) * 1000) / 10 : "";
  });

  $("#form-item").addEventListener("submit", async (ev) => {
    if (ev.submitter?.value !== "save") return;
    ev.preventDefault();
    const f = ev.target;
    const original = JSON.parse(f.dataset.original || "{}");
    let variants = [];
    if (F(f, "variants").value.trim()) {
      try { variants = JSON.parse(F(f, "variants").value); if (!Array.isArray(variants)) throw new Error("use uma lista []"); }
      catch (e) { toast("err", "Variantes inválidas", e.message); return; }
    }
    const attributes = {};
    F(f, "attributes").value.split("\n").forEach((line) => {
      const idx = line.indexOf(":");
      if (idx > 0) attributes[line.slice(0, idx).trim()] = line.slice(idx + 1).trim();
    });
    const onRequest = F(f, "on_request").checked;
    const price = {
      ...(original.price || {}),
      amount: onRequest ? null : parseMoney(F(f, "amount").value),
      cash_amount: onRequest ? null : parseMoney(F(f, "cash_amount").value),
      max_installments: Number(F(f, "max_installments").value) || 1,
      interest_free: F(f, "interest_free").checked,
      note: F(f, "note").value.trim() || null,
    };
    if (!onRequest && price.amount == null) { toast("err", "Informe o preço", "Ou marque “Preço sob consulta”."); return; }
    const op = original.price || {};
    if (price.amount !== op.amount || price.cash_amount !== op.cash_amount || price.max_installments !== op.max_installments || !op.updated_at) {
      price.updated_at = new Date().toISOString();
    }
    Object.keys(price).forEach((k) => { if (price[k] === null) delete price[k]; });
    const item = {
      ...original,
      item_id: F(f, "item_id").value.trim(),
      name: F(f, "name").value.trim(),
      category: F(f, "category").value.trim(),
      description: F(f, "description").value.trim(),
      attributes,
      tags: F(f, "tags").value.split(",").map((x) => x.trim()).filter(Boolean),
      price,
      variants,
      in_stock: F(f, "in_stock").checked,
      active: F(f, "active").checked,
    };
    const items = (S.draft.catalog ||= { items: [] }).items;
    const backup = clone(items);
    if (editingIndex === null) items.push(item); else items[editingIndex] = item;
    setDirty(true);
    const r = await saveTenant($("#btn-item-save"), `Item “${item.name}” salvo e aplicado`);
    if (r.ok) $("#dlg-item").close();
    else { S.draft.catalog.items = backup; }
  });

  // ------------------------------------------------------------------ novo cliente
  $("#btn-tn-new").addEventListener("click", () => { $("#form-new").reset(); $("#dlg-new").showModal(); });
  $("#form-new").addEventListener("submit", async (ev) => {
    if (ev.submitter?.value !== "save") return;
    ev.preventDefault();
    const f = ev.target;
    const crm = {};
    const body = {
      demo_data: false,
      tenant: {
        tenant_id: F(f, "tenant_id").value.trim().toLowerCase(),
        display_name: F(f, "display_name").value.trim(),
        segment: F(f, "segment").value,
        niche_name: F(f, "niche_name")?.value.trim() || null,
        plan_tier: F(f, "plan_tier")?.value || "pro",
        timezone: F(f, "timezone").value.trim() || "America/Belem",
        whatsapp_provider: "waha",
        crm_provider: "none",
      },
      business_info: {},
      catalog: { items: [], synonyms: {} },
      delivery: { zones: [] },
      crm,
    };
    const r = await act(ev.submitter, "Cliente criado", () => api("POST", "/api/admin/tenants", body), { okMsg: body.tenant.display_name });
    if (!r.ok) return;
    $("#dlg-new").close();
    S.tenants.push(r.data.summary);
    fillTenantSelects();
    S.dirty = false;
    $("#tn-select").value = body.tenant.tenant_id;
    await loadTenant(body.tenant.tenant_id);
  });

  // ================================================================== ABA 4 — CONVERSAS
  async function loadConversations(btn) {
    const tenant = $("#cv-tenant").value;
    const r = await api("GET", `/api/admin/conversations${tenant ? `?tenant_id=${encodeURIComponent(tenant)}` : ""}`);
    if (btn) toast(r.ok ? "ok" : "err", r.ok ? "Conversas atualizadas" : "Falha ao listar conversas", r.ok ? `${r.data.length} contato(s)` : r.error, r.ms);
    if (!r.ok) return;
    const list = r.data;
    const paused = list.filter((c) => c.ai_paused).length;
    $("#cv-counter").textContent = `${list.length} contato(s) · ${paused} com IA pausada`;
    $("#cv-list").innerHTML = list.length ? list.map((c, i) => {
      const initial = esc((c.name || "?").trim().charAt(0).toUpperCase() || "?");
      const attrs = Object.entries(c.attributes || {}).filter(([k]) => k !== "ctwa_source");
      const until = c.pause?.until ? new Date(c.pause.until).toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short" }) : null;
      return `<article class="card conv ${c.ai_paused ? "paused" : ""}">
        <div class="conv-head">
          <div class="conv-avatar">${initial}</div>
          <div class="grow" style="flex:1;min-width:0"><strong>${esc(c.name || "Sem nome")}</strong><div class="muted small mono">${esc(c.phone)}</div></div>
          ${c.ai_paused ? `<span class="chip chip-warn">⏸️ IA pausada</span>` : `<span class="chip chip-ok">🟢 IA ativa</span>`}
        </div>
        <div class="chips">
          <span class="chip">${esc(tenantSummary(c.tenant_id)?.display_name || c.tenant_id)}</span>
          <span class="chip chip-info">${esc(c.status)}</span>
          ${c.tags.map((t) => `<span class="chip ${t === "atendimento_humano" ? "chip-warn" : ""}">${esc(t)}</span>`).join("")}
          ${c.source ? `<span class="chip">origem: ${esc(c.source)}</span>` : ""}
        </div>
        ${attrs.length ? `<div class="muted small">${attrs.map(([k, v]) => `${esc(k)}: <strong>${esc(v)}</strong>`).join(" · ")}</div>` : ""}
        ${c.last_message ? `<div class="conv-last">${c.last_role === "ia" ? "🤖" : "👤"} ${esc(c.last_message)}</div>` : ""}
        ${c.ai_paused && c.pause ? `<div class="muted small">Motivo: <strong>${esc(c.pause.reason)}</strong>${until ? ` · até ${esc(until)}` : ""}</div>` : ""}
        <div class="row-between"><span class="muted small">${esc(c.message_count)} msg · ${esc(relTime(c.last_interaction_at))}</span>
          <div class="conv-actions">
            ${c.ai_paused ? `<button class="btn btn-success btn-sm" data-cv="resume" data-i="${i}">🔄 Reativar IA</button>`
              : `<button class="btn btn-sm" data-cv="pause" data-i="${i}">⏸️ Pausar IA Manualmente</button>`}
            <button class="btn btn-ghost btn-sm" data-cv="reset" data-i="${i}" title="Apagar memória da conversa">🧹 Resetar</button>
          </div></div>
      </article>`;
    }).join("") : `<div class="empty">Nenhuma conversa real ainda. Elas aparecem aqui quando contatos liberados pelo Sandbox falam com o agente.</div>`;
    $("#cv-list").dataset.items = JSON.stringify(list.map((c) => [c.tenant_id, c.phone]));
  }
  $("#btn-cv-refresh").addEventListener("click", (ev) => loadConversations(ev.currentTarget));
  $("#cv-tenant").addEventListener("change", () => loadConversations());
  $("#cv-list").addEventListener("click", async (ev) => {
    const btn = ev.target.closest("button[data-cv]");
    if (!btn) return;
    const [tenant, phone] = JSON.parse($("#cv-list").dataset.items || "[]")[Number(btn.dataset.i)] || [];
    if (!tenant) return;
    const base = `/api/admin/conversations/${encodeURIComponent(tenant)}/${encodeURIComponent(phone)}`;
    let r;
    if (btn.dataset.cv === "resume") r = await act(btn, "🔄 IA reativada", () => api("POST", `${base}/resume`), { okMsg: (d) => (d.crm_synced ? "Pausa removida e tag removida no CRM." : "Pausa e tag atendimento_humano removidas.") });
    else if (btn.dataset.cv === "pause") r = await act(btn, "⏸️ IA pausada manualmente", () => api("POST", `${base}/pause`, {}), { okMsg: phone });
    else {
      if (!confirm("Apagar memória/histórico deste contato e reativar a IA?")) return;
      r = await act(btn, "🧹 Conversa resetada", () => api("DELETE", base), { okMsg: phone });
    }
    if (r.ok) loadConversations();
  });
  S.cvTimer = setInterval(() => {
    if (currentTab() === "conversations" && $("#cv-auto").checked && !document.hidden) loadConversations();
  }, 10000);

  // ================================================================== ABA 5 — WEBHOOKS
  async function loadWebhooks() {
    const r = await api("GET", "/api/admin/webhooks");
    if (!r.ok) { toast("err", "Falha ao carregar webhooks", r.error, r.ms); return; }
    const origin = window.location.origin;
    $("#wh-base").textContent = `Domínio detectado: ${origin}`;
    const trigger = r.data.trigger_keywords?.[0] || "/lobo-sales-agent";
    $("#wh-trigger-example").textContent = `${trigger} Olá`;
    $("#wh-ttl").textContent = S.sandbox?.config.session_ttl_minutes ?? 30;
    const urlCell = (path) => `<div class="row-between"><span class="url">${esc(origin + path)}</span><button class="btn btn-sm" data-copy="${esc(origin + path)}">📋 Copiar</button></div>`;
    $("#wh-table").innerHTML = `<thead><tr><th>Cliente</th><th>Canal &amp; Sessão</th><th>WAHA</th><th>AstraCalls</th><th>HMAC / Secret &amp; Ações</th></tr></thead><tbody>${
      r.data.tenants.map((t) => `<tr data-wh-tenant="${esc(t.tenant_id)}">
        <td><strong>${esc(t.display_name)}</strong><div class="muted small mono">${esc(t.tenant_id)}</div></td>
        <td style="min-width:190px">
          <select data-wh="provider" class="wh-input" style="margin-bottom:4px">
            <option value="waha" ${t.whatsapp_provider === "waha" ? "selected" : ""}>waha</option>
            <option value="astracalls" ${t.whatsapp_provider === "astracalls" ? "selected" : ""}>astracalls</option>
          </select>
          <input data-wh="session" value="${esc(t.whatsapp_session_id || "")}" placeholder="sessão (ex.: Danyllo)">
        </td>
        <td style="min-width:260px">${urlCell(t.paths.waha)}</td>
        <td style="min-width:260px">${urlCell(t.paths.astracalls)}</td>
        <td style="min-width:230px">
          <div class="row-between" style="gap:6px">
            <input data-wh="secret" type="password" value="${esc(t.webhook_secret_masked || "")}" placeholder="HMAC Secret (opcional)">
            <button class="btn btn-primary btn-sm" data-save-wh="${esc(t.tenant_id)}">💾 Salvar</button>
          </div>
        </td>
      </tr>`).join("")}</tbody>`;
  }
  $("#wh-table").addEventListener("click", async (ev) => {
    const saveBtn = ev.target.closest("button[data-save-wh]");
    if (saveBtn) {
      const tenantId = saveBtn.dataset.saveWh;
      const row = saveBtn.closest("tr[data-wh-tenant]");
      if (!row) return;
      const body = {
        whatsapp_provider: row.querySelector('[data-wh="provider"]').value,
        whatsapp_session_id: row.querySelector('[data-wh="session"]').value.trim() || null,
        webhook_secret: row.querySelector('[data-wh="secret"]').value.trim() || null,
      };
      const r = await act(saveBtn, "Webhook do cliente salvo", () => api("PUT", `/api/admin/webhooks/${encodeURIComponent(tenantId)}`, body), { okMsg: tenantId });
      if (r.ok) {
        const idx = S.tenants.findIndex((t) => t.tenant_id === tenantId);
        if (idx >= 0) S.tenants[idx] = { ...S.tenants[idx], ...r.data };
      }
      return;
    }
    const btn = ev.target.closest("button[data-copy]");
    if (!btn) return;
    const text = btn.dataset.copy;
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      const ta = document.createElement("textarea");
      ta.value = text; document.body.appendChild(ta); ta.select();
      const ok = document.execCommand("copy"); ta.remove();
      if (!ok) { toast("err", "Não foi possível copiar", text); return; }
    }
    toast("ok", "📋 URL copiada", text);
  });

  function syncSendTenant() {
    const t = tenantSummary($("#ts-tenant").value);
    if (!t) return;
    $("#ts-provider").value = t.whatsapp_provider;
    $("#ts-session").placeholder = t.whatsapp_session_id ? `padrão: ${t.whatsapp_session_id}` : "padrão do .env";
  }
  $("#ts-tenant").addEventListener("change", syncSendTenant);
  $("#form-send").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const phone = $("#ts-phone").value.replace(/\D/g, "");
    const body = {
      tenant_id: $("#ts-tenant").value,
      provider: $("#ts-provider").value,
      phone,
      text: $("#ts-text").value.trim(),
      session_id: $("#ts-session").value.trim() || null,
    };
    if (!confirm(`Enviar uma mensagem REAL pelo ${body.provider.toUpperCase()} para ${phone}?`)) return;
    const r = await act(ev.submitter, "📲 Mensagem enviada pelo gateway", () => api("POST", "/api/admin/gateway/test-send", body),
      { okMsg: (d) => `${d.provider.toUpperCase()} · sessão ${d.session_id} · id ${d.message_id || "—"}`, errTitle: "Gateway recusou o envio" });
    $("#ts-result").innerHTML = r.ok
      ? `🟢 <strong>Sucesso</strong> — ${esc(r.data.provider)} / ${esc(r.data.session_id)} · id <span class="mono">${esc(r.data.message_id || "—")}</span> · ${esc(r.ms)} ms`
      : `🔴 <strong>Erro</strong> — ${esc(r.error)} · ${esc(r.ms)} ms`;
  });

  // ------------------------------------------------------------------ boot
  window.addEventListener("beforeunload", (ev) => { if (S.dirty) { ev.preventDefault(); ev.returnValue = ""; } });

  async function boot() {
    const ok = await loadOverview();
    if (!ok) return;
    await loadSandbox();
    activateTab((location.hash || "#sandbox").slice(1));
  }
  boot();
})();
