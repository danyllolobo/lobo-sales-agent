# 🚀 Guia de Deploy no Easypanel — Lobo AI Sales Agent

Este projeto já possui `Dockerfile` e `.dockerignore` prontos para deploy imediato no **Easypanel**.

---

## 1. Subir para o GitHub

No terminal dentro da pasta `lobo-sales-agent`, crie um repositório privado no seu GitHub (ex.: `danyllolobo/lobo-sales-agent`) e envie o código:

```powershell
git remote add origin https://github.com/danyllolobo/lobo-sales-agent.git
git branch -M main
git push -u origin main
```
*(O arquivo `.env` já está protegido no `.gitignore` e nunca vai para o GitHub).*

---

## 2. Criar o Serviço no Easypanel

1. Acesse o painel do seu **Easypanel**.
2. Entre no projeto desejado (ou crie um projeto `lobo-sales-agent`).
3. Clique em **+ Service** -> **App**.
4. Em **Source (Origem)**:
   - Selecione **GitHub**.
   - Repositório: `danyllolobo/lobo-sales-agent`
   - Branch: `main`
   - Build Method: **Dockerfile** (caminho `/Dockerfile`).
5. Em **Domains / Porta**:
   - Crie o domínio (ex.: `salesagent.loboads.top` ou o domínio padrão do Easypanel).
   - Porta interna do container: **`8000`**.

---

## 3. Variáveis de Ambiente no Easypanel (`Environment`)

No Easypanel, vá na aba **Environment** do serviço:
1. Abra o seu arquivo local [`.env`](file:///c:/Users/Micro/Documents/SITE%20COM%20TRAE/lobo-sales-agent/.env) (que já tem todas as suas chaves reais configuradas).
2. Copie todo o conteúdo do seu `.env` e cole na caixa **Environment** do Easypanel.
3. **Importante:** Enquanto estiver testando com os preços fictícios de demonstração (`demo_data: true`), deixe `ENVIRONMENT="staging"` (ou `development`). Quando cadastrar os produtos reais e mudar `demo_data` para `false`, troque para `ENVIRONMENT="production"`.

> **Sobre o Redis no Easypanel:**  
> Com `REDIS_BACKEND=auto` (padrão), o container já funciona imediatamente usando o Redis em memória (`fakeredis`), sem precisar criar banco extra agora. Se quiser persistência entre reinicializações, basta clicar em **+ Service -> Redis** no mesmo projeto do Easypanel e apontar `REDIS_URL="redis://nome-do-servico-redis:6379/0"`.

Clique em **Save** e depois em **Deploy**!

---

## 4. Testar e Conectar o Webhook no WAHA ou AstraCalls

1. Assim que o container ficar verde no Easypanel, abra no navegador:
   - `https://SEU-DOMINIO-EASYPANEL/health` -> Deve retornar `{"status": "ok", ...}`.
   - `https://SEU-DOMINIO-EASYPANEL/docs` -> Abre o Swagger interativo.
2. Para testar no WhatsApp via **WAHA** (`https://waha.loboads.top`):
   - Na sua sessão de testes do WAHA, configure o Webhook (`message`) para:
     ```
     https://SEU-DOMINIO-EASYPANEL/webhook/companhia-dos-sonhos/waha
     ```
3. Para testar via **AstraCalls** (`https://chamadas.loboads.top`):
   - Configure o Webhook da instância para:
     ```
     https://SEU-DOMINIO-EASYPANEL/webhook/companhia-dos-sonhos/astracalls
     ```
   - Para testar a Vizzu: `/webhook/vizzu/astracalls` (ou `/waha`).
   - Para testar a Splash: `/webhook/splash-piscinas/astracalls` (ou `/waha`).
