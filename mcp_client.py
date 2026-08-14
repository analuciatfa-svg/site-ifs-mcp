#!/usr/bin/env python3
"""
mcp_client — cliente do **Salesforce Platform MCP** (Hosted MCP Server, headless-360).

É o irmão do miaw_client.py: enquanto o MIAW conversa com um Agentforce Service
Agent, este módulo fala o **Model Context Protocol** com o servidor MCP *hospedado
pela própria Salesforce* (não é MuleSoft Agent Fabric). O navegador NUNCA fala
direto com o MCP — quem fala é a ponte (evita CORS e, sobretudo, impede que o
segredo do app apareça no browser).

Provado ponta a ponta nesta org (somaOrg):
  OAuth client_credentials  → token com escopo `mcp_api id`
  initialize                → HTTP 200, serverInfo=headless-360 v1.0.0, mcp-session-id
  notifications/initialized → 202/200
  tools/list                → 4 tools: discover, describe, dispatch, dispatch_readonly
  tools/call dispatch       → POST /services/data/vXX/graphql leu Leads REAIS

O MCP é um ROTEADOR CURADO, não um proxy de API cru:
  · discover(query)          — busca semântica de "recipes" (o que dá p/ fazer)
  · describe(id)             — detalhe/passos/path de uma recipe (chamar ANTES de dispatch)
  · dispatch(method,path,..) — executa chamadas que MUDAM estado (POST/PUT/PATCH/DELETE)
  · dispatch_readonly(...)   — só GET
Rota comprovadamente roteável: `/services/data/vXX/graphql` (GraphQL — lê e grava
Lead/Case via UIAPI). SOQL cru (`/query/?q=`) e Apex REST custom NÃO são roteáveis
(ROUTE_NOT_FOUND) — por isso as jornadas de dados vão por GraphQL, não por redirect.

⚠ SEGREDO: client_id/secret vêm SÓ de variáveis de ambiente
(`MCP_CLIENT_ID` / `MCP_CLIENT_SECRET`). NADA é gravado no repo, nem impresso.
O token de acesso vive só em memória durante a execução.

Config por variável de ambiente (defaults para a somaOrg):
  export MCP_CLIENT_ID="..."          # do External Client App (Workshop_MCP_Client_ALTFA)
  export MCP_CLIENT_SECRET="..."      # idem — NUNCA versionar
  export MCP_INSTANCE_URL="https://trailsignup-2979e1c6606a3b.my.salesforce.com"
  export MCP_ENDPOINT="https://api.salesforce.com/platform/mcp/v1/platform/headless-360"

Uso como TESTE DE FOGO (nada é impresso do segredo):
  MCP_CLIENT_ID=... MCP_CLIENT_SECRET=... python3 core/mcp_client.py

Uso como biblioteca (na ponte):
  from mcp_client import MCPClient, MCPError
  mcp = MCPClient()                       # lê env; erra se faltar credencial
  trace = mcp.handshake()                 # OAuth + initialize + tools/list (p/ a tela)
  leads = mcp.graphql(QUERY_LEADS)        # discover→describe→dispatch GraphQL
"""

import json
import os
import urllib.parse
import urllib.request
import urllib.error


# --- Configuração (env; defaults só para host/endpoint, NUNCA p/ segredo) -----
INSTANCE_URL = os.environ.get(
    "MCP_INSTANCE_URL", "https://trailsignup-2979e1c6606a3b.my.salesforce.com"
).rstrip("/")
MCP_ENDPOINT = os.environ.get(
    "MCP_ENDPOINT",
    "https://api.salesforce.com/platform/mcp/v1/platform/headless-360",
).rstrip("/")
GRAPHQL_PATH = os.environ.get("MCP_GRAPHQL_PATH", "/services/data/v67.0/graphql")
PROTOCOL_VERSION = os.environ.get("MCP_PROTOCOL_VERSION", "2025-06-18")


class MCPError(RuntimeError):
    """Erro de qualquer etapa (OAuth ou JSON-RPC do MCP), com corpo se houver."""


def _http(url, data=None, headers=None, method="POST", timeout=60):
    """POST/GET helper. Retorna (status, headers_dict, corpo_texto). Erra em HTTPError.

    Não faz json.loads: o MCP responde ora JSON puro, ora SSE (`data: {...}`),
    então quem chama decide como interpretar o corpo.
    """
    req = urllib.request.Request(url, data=data, headers=dict(headers or {}),
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, dict(r.headers), raw
    except urllib.error.HTTPError as e:
        detalhe = ""
        try:
            detalhe = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        raise MCPError(f"HTTP {e.code} em {url}: {detalhe[:500]}") from e
    except urllib.error.URLError as e:
        raise MCPError(f"Falha de rede em {url}: {e.reason}") from e


def _parse_corpo_rpc(texto):
    """Extrai o objeto JSON-RPC de uma resposta que pode ser JSON puro OU SSE.

    SSE chega como linhas `event: message` / `data: {...}`. Pegamos o último
    `data:` com JSON válido (a resposta final do método).
    """
    texto = (texto or "").strip()
    if not texto:
        return {}
    # JSON puro
    if texto[0] in "{[":
        try:
            return json.loads(texto)
        except json.JSONDecodeError:
            pass
    # SSE: varre as linhas data: e fica com o último JSON válido
    ultimo = None
    for linha in texto.splitlines():
        linha = linha.strip()
        if linha.startswith("data:"):
            corpo = linha[len("data:"):].strip()
            if corpo and corpo[0] in "{[":
                try:
                    ultimo = json.loads(corpo)
                except json.JSONDecodeError:
                    continue
    if ultimo is None:
        raise MCPError(f"Resposta MCP não-JSON/SSE: {texto[:300]}")
    return ultimo


class MCPClient:
    """Sessão viva com o Salesforce Platform MCP (token + mcp-session-id)."""

    def __init__(self, client_id=None, client_secret=None):
        self._client_id = client_id or os.environ.get("MCP_CLIENT_ID", "")
        self._client_secret = client_secret or os.environ.get("MCP_CLIENT_SECRET", "")
        self.instance_url = INSTANCE_URL
        self.endpoint = MCP_ENDPOINT
        self.token = None
        self.token_scope = None
        self.session_id = None
        self.server_info = None
        self.tools = None
        self._rpc_id = 0

    @property
    def configurado(self):
        """True se há credencial para tentar a conexão (sem revelar o segredo)."""
        return bool(self._client_id and self._client_secret)

    # --- passo 0: OAuth client_credentials ----------------------------------
    def mint_token(self):
        """Troca client_id/secret por um access token com escopo mcp_api."""
        if not self.configurado:
            raise MCPError("MCP não configurado: defina MCP_CLIENT_ID e "
                           "MCP_CLIENT_SECRET no ambiente (nunca no repo).")
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        }).encode("utf-8")
        status, _h, texto = _http(
            f"{self.instance_url}/services/oauth2/token",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Accept": "application/json"},
        )
        try:
            data = json.loads(texto)
        except json.JSONDecodeError:
            raise MCPError(f"OAuth resposta não-JSON: {texto[:300]}")
        token = data.get("access_token")
        if not token:
            # erro do OAuth vem como {error, error_description}
            raise MCPError(f"OAuth sem access_token: "
                           f"{data.get('error')} — {data.get('error_description')}")
        self.token = token
        self.token_scope = data.get("scope")
        return self.token_scope

    # --- transporte JSON-RPC sobre HTTP ------------------------------------
    def _headers(self):
        h = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            h["mcp-session-id"] = self.session_id
        return h

    def _rpc(self, method, params=None, is_notification=False):
        """Manda um JSON-RPC ao endpoint MCP. Devolve o dict de resposta (ou {}).

        Captura o header mcp-session-id (aparece na resposta do initialize).
        """
        payload = {"jsonrpc": "2.0", "method": method}
        if not is_notification:
            self._rpc_id += 1
            payload["id"] = self._rpc_id
        if params is not None:
            payload["params"] = params
        data = json.dumps(payload).encode("utf-8")
        status, headers, texto = _http(self.endpoint, data=data,
                                       headers=self._headers())
        # o session-id chega em header (case-insensitive)
        for k, v in headers.items():
            if k.lower() == "mcp-session-id" and v:
                self.session_id = v
        if is_notification:
            return {}
        resposta = _parse_corpo_rpc(texto)
        if isinstance(resposta, dict) and resposta.get("error"):
            err = resposta["error"]
            raise MCPError(f"{method}: {err.get('code')} — {err.get('message')}")
        return resposta.get("result", resposta) if isinstance(resposta, dict) else {}

    # --- handshake MCP ------------------------------------------------------
    def initialize(self):
        result = self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "site-ifs-mcp", "version": "1.0.0"},
        })
        self.server_info = result.get("serverInfo") if isinstance(result, dict) else None
        # o handshake exige a notificação de "initialized" antes de usar tools
        self._rpc("notifications/initialized", is_notification=True)
        return result

    def list_tools(self):
        result = self._rpc("tools/list")
        self.tools = result.get("tools", []) if isinstance(result, dict) else []
        return self.tools

    def call_tool(self, name, arguments=None):
        """tools/call cru. Devolve o `result` do JSON-RPC (content + structured)."""
        return self._rpc("tools/call", {"name": name,
                                        "arguments": arguments or {}})

    # --- alto nível: um handshake completo para a TELA ----------------------
    def handshake(self):
        """OAuth + initialize + tools/list, com trilha estruturada p/ exibir.

        Devolve um dict com cada passo (rótulo, ok, detalhe) — é o que a aba
        'Simulações Reais MCP' desenha como o handshake AO VIVO. NÃO inclui o
        token nem o segredo.
        """
        trilha = []
        escopo = self.mint_token()
        trilha.append({
            "passo": "oauth", "titulo": "OAuth 2.0 — client_credentials",
            "ok": True,
            "detalhe": {"grant": "client_credentials", "scope": escopo,
                        "token": "obtido (oculto por segurança)"}})
        init = self.initialize()
        trilha.append({
            "passo": "initialize", "titulo": "MCP initialize",
            "ok": True,
            "detalhe": {"serverInfo": self.server_info,
                        "protocolVersion": (init or {}).get("protocolVersion"),
                        "sessionId": self.session_id}})
        tools = self.list_tools()
        trilha.append({
            "passo": "tools/list", "titulo": "Descoberta de ferramentas",
            "ok": True,
            "detalhe": {"tools": [{"name": t.get("name"),
                                   "description": t.get("description")}
                                  for t in (tools or [])]}})
        return {
            "ok": True,
            "endpoint": self.endpoint,
            "instanceUrl": self.instance_url,
            "serverInfo": self.server_info,
            "sessionId": self.session_id,
            "scope": escopo,
            "tools": tools,
            "trilha": trilha,
        }

    def garantir_sessao(self):
        """Idempotente: garante token + sessão MCP inicializados."""
        if not self.token:
            self.mint_token()
        if not self.session_id or self.tools is None:
            self.initialize()
            self.list_tools()

    # --- alto nível: GraphQL roteado pelo MCP -------------------------------
    def _extrair_json_do_content(self, result):
        """Do result de tools/call, tenta obter o corpo JSON da API roteada.

        O MCP devolve `content: [{type:'text', text:'...'}]` e/ou
        `structuredContent`. GraphQL volta como JSON dentro do text (ou já
        estruturado). Devolve (dict_ou_none, texto_bruto).
        """
        if not isinstance(result, dict):
            return None, str(result)
        # 1) structuredContent tem prioridade quando presente
        estrut = result.get("structuredContent")
        if isinstance(estrut, dict) and estrut:
            return estrut, json.dumps(estrut)
        # 2) content[].text — concatena os textos e tenta json.loads
        partes = []
        for item in (result.get("content") or []):
            if isinstance(item, dict) and item.get("type") == "text":
                partes.append(item.get("text") or "")
        texto = "\n".join(partes).strip()
        if texto:
            try:
                return json.loads(texto), texto
            except json.JSONDecodeError:
                return None, texto
        return None, json.dumps(result)

    def dispatch(self, method, path, body=None, query_params=None):
        """Chama a tool dispatch/dispatch_readonly conforme o inputSchema real do MCP.

        Contrato descoberto em tools/list (NÃO inventado): as duas tools exigem
        `url` (o path relativo, exatamente como veio do describe) + `method`;
        aceitam `body` e `queryParams`. GET vai por `dispatch_readonly`; qualquer
        método que muda estado (POST/PUT/PATCH/DELETE) vai por `dispatch`.
        """
        self.garantir_sessao()
        tool = "dispatch_readonly" if method.upper() == "GET" else "dispatch"
        args = {"url": path, "method": method.upper()}
        if body is not None:
            args["body"] = body
        if query_params:
            args["queryParams"] = query_params
        result = self.call_tool(tool, args)
        dados, bruto = self._extrair_json_do_content(result)
        # A API roteada volta num ENVELOPE {status_code, body, url}. Desembrulha
        # e faz o erro (400/ROUTE_NOT_FOUND/error_code) VIRAR erro — sem isso um
        # corpo de erro (que é JSON válido) passaria como "dados" silenciosos.
        status_api = None
        corpo = dados
        if isinstance(dados, dict) and "status_code" in dados and "body" in dados:
            status_api = dados.get("status_code")
            corpo = dados.get("body")
        erro = None
        if isinstance(dados, dict) and dados.get("error_code"):
            erro = f"{dados.get('error_code')}: {dados.get('message')}"
        elif isinstance(status_api, int) and status_api >= 400:
            erro = f"HTTP {status_api} na rota {path}: {json.dumps(corpo)[:300]}"
        return {"tool": tool, "args": {"method": method.upper(), "url": path},
                "status_api": status_api, "corpo": corpo, "erro": erro,
                "dados": dados, "bruto": bruto, "raw_result": result}

    def graphql(self, query, variables=None):
        """Executa uma query GraphQL via MCP (rota comprovadamente roteável).

        GraphQL é sempre POST → roteia pela tool `dispatch` (a `dispatch_readonly`
        só aceita GET).
        """
        body = {"query": query}
        if variables:
            body["variables"] = variables
        return self.dispatch("POST", GRAPHQL_PATH, body=body)

    def jornada_graphql(self, query, busca_recipe=None, variables=None):
        """A jornada GENUÍNA do MCP como um roteador curado, para exibir na tela:

        discover(busca) → describe(recipe) → dispatch(GraphQL) → dados da org.

        Faz de verdade os 3 passos (não é encenação): o `discover` prova que o MCP
        indexa a receita de GraphQL; o `describe` traz os passos/entrada; o
        `dispatch` executa. Devolve uma trilha estruturada + o resultado final.
        Se o discover/describe falhar por qualquer motivo, o dispatch ainda roda
        (a rota GraphQL é conhecida) e a trilha registra o que houve — sem fingir.
        """
        self.garantir_sessao()
        busca = busca_recipe or "run a GraphQL query to read records"
        trilha = []

        # 1) discover — busca semântica das receitas
        recipe_id = None
        try:
            r = self.call_tool("discover", {"query": busca, "limit": 5})
            dados, _ = self._extrair_json_do_content(r)
            resultados = (dados.get("results") if isinstance(dados, dict) else []) or []
            cards = [{"sor_id": c.get("sor_id"), "score": c.get("score"),
                      "description": c.get("description")} for c in resultados[:5]]
            # a receita de GraphQL é a de maior afinidade com "GraphQL query"
            for c in resultados:
                if "graphql" in (c.get("sor_id") or "").lower():
                    recipe_id = c.get("sor_id")
                    break
            trilha.append({"passo": "discover", "titulo": "discover — busca de receita",
                           "ok": True, "detalhe": {"query": busca, "cards": cards,
                                                   "escolhida": recipe_id}})
        except MCPError as e:
            trilha.append({"passo": "discover", "titulo": "discover — busca de receita",
                           "ok": False, "detalhe": {"erro": str(e)}})

        # 2) describe — detalha a receita escolhida (passos + entrada)
        if recipe_id:
            try:
                r = self.call_tool("describe", {"id": recipe_id})
                dados, _ = self._extrair_json_do_content(r)
                sor = dados.get("sor") if isinstance(dados, dict) else None
                passos = [{"id": s.get("id"), "description": s.get("description")}
                          for s in ((sor or {}).get("steps") or [])]
                trilha.append({"passo": "describe", "titulo": "describe — passos da receita",
                               "ok": True,
                               "detalhe": {"sor_id": recipe_id, "path": GRAPHQL_PATH,
                                           "steps": passos}})
            except MCPError as e:
                trilha.append({"passo": "describe", "titulo": "describe — passos da receita",
                               "ok": False, "detalhe": {"erro": str(e)}})

        # 3) dispatch — executa o GraphQL de verdade
        res = self.graphql(query, variables=variables)
        trilha.append({
            "passo": "dispatch", "titulo": "dispatch — executa GraphQL na org",
            "ok": not res.get("erro"),
            "detalhe": {"tool": res.get("tool"), "method": "POST", "url": GRAPHQL_PATH,
                        "status_api": res.get("status_api"), "erro": res.get("erro")}})
        return {"ok": not res.get("erro"), "trilha": trilha,
                "corpo": res.get("corpo"), "erro": res.get("erro"),
                "recipe_id": recipe_id}


# --- Jornadas de dados (fonte da verdade única p/ a ponte E o app Heroku) ----
# Cada jornada é uma leitura real via GraphQL/UIAPI roteada pelo MCP curado. Os
# campos batem com o que o site desenha (OBJ_META em simulacoes-mcp.html).
JORNADAS = {
    "leads": {
        "obj": "Lead",
        "busca": "run a GraphQL query to read recent Lead records",
        "query": """
query {
  uiapi { query {
    Lead(first: 6, orderBy: { CreatedDate: { order: DESC } }) {
      edges { node {
        Id Name { value } Company { value } Status { value }
        Email { value } CreatedDate { value }
      } }
    }
  } }
}""".strip(),
    },
    "consignado": {
        "obj": "Opportunity",
        "busca": "run a GraphQL query to read Opportunity records",
        "query": """
query {
  uiapi { query {
    Opportunity(first: 6, orderBy: { CreatedDate: { order: DESC } }) {
      edges { node {
        Id Name { value } StageName { value }
        Amount { value displayValue } CreatedDate { value }
      } }
    }
  } }
}""".strip(),
    },
    "atendimento": {
        "obj": "Case",
        "busca": "run a GraphQL query to read Case records",
        "query": """
query {
  uiapi { query {
    Case(first: 6, orderBy: { CreatedDate: { order: DESC } }) {
      edges { node {
        Id CaseNumber { value } Subject { value } Status { value }
        Origin { value } CreatedDate { value }
      } }
    }
  } }
}""".strip(),
    },
}

# alias mantido p/ o teste de fogo (_cli)
QUERY_LEADS = JORNADAS["leads"]["query"]


def flatten_edges(corpo, obj):
    """Achata o corpo GraphQL/UIAPI em uma lista de nós {campo: valor}."""
    registros = []
    if isinstance(corpo, dict):
        edges = (((corpo.get("data") or {}).get("uiapi") or {})
                 .get("query", {}).get(obj, {}) or {}).get("edges", [])
        for e in edges:
            node = (e or {}).get("node") or {}
            plano = {}
            for k, v in node.items():
                plano[k] = v.get("value") if isinstance(v, dict) else v
            registros.append(plano)
    return registros


def rodar_jornada(client, nome):
    """Roda uma jornada de dados via MCP (discover→describe→dispatch GraphQL).

    Devolve {ok, obj, trilha, registros, erro, recipe_id, endpoint}. `registros`
    já vem achatado, pronto p/ o site desenhar. Nada inventado: erro da rota vira
    erro aqui. Usado tanto pela ponte local quanto pelo app de produção (Heroku).
    """
    cfg = JORNADAS.get(nome)
    if not cfg:
        return {"ok": False, "erro": f"jornada MCP desconhecida: {nome}"}
    try:
        j = client.jornada_graphql(cfg["query"], busca_recipe=cfg["busca"])
    except MCPError as e:
        return {"ok": False, "erro": str(e)}
    return {"ok": j.get("ok"), "obj": cfg["obj"], "trilha": j.get("trilha"),
            "registros": flatten_edges(j.get("corpo"), cfg["obj"]),
            "erro": j.get("erro"), "recipe_id": j.get("recipe_id"),
            "endpoint": client.endpoint}


def _cli():
    """Teste de fogo do MCP. Não imprime token nem segredo — só o que retorna."""
    print("=" * 64)
    print("TESTE DE FOGO — Salesforce Platform MCP (headless-360)")
    print(f"  Endpoint : {MCP_ENDPOINT}")
    print(f"  Instance : {INSTANCE_URL}")
    print("=" * 64)
    try:
        mcp = MCPClient()
        if not mcp.configurado:
            print("✗ Defina MCP_CLIENT_ID e MCP_CLIENT_SECRET no ambiente.")
            raise SystemExit(2)
        hs = mcp.handshake()
        print(f"✓ OAuth ok — scope: {hs['scope']}")
        print(f"✓ initialize ok — server: {hs['serverInfo']} · session: "
              f"{(hs['sessionId'] or '')[:8]}…")
        print(f"✓ tools/list — {len(hs['tools'])} ferramentas: "
              f"{', '.join(t.get('name') for t in hs['tools'])}")
        print("\n— dispatch GraphQL (Leads reais) —")
        res = mcp.graphql(QUERY_LEADS)
        if res.get("erro"):
            print(f"⚠ GraphQL retornou erro da rota: {res['erro']}")
        corpo = res.get("corpo")
        edges = (((((corpo or {}).get("data") or {}).get("uiapi") or {})
                  .get("query", {}).get("Lead", {}) or {}).get("edges", [])
                 if isinstance(corpo, dict) else [])
        if edges:
            print(f"✓ GraphQL ok — {len(edges)} leads:")
            for e in edges:
                n = e.get("node", {})
                print(f"   · {n.get('Name', {}).get('value')} "
                      f"({n.get('Company', {}).get('value')}) "
                      f"[{n.get('Status', {}).get('value')}]")
        elif not res.get("erro"):
            print(f"⚠ GraphQL sem edges. Corpo: {json.dumps(corpo)[:300]}")
        print("\n✓ MCP FUNCIONANDO — Salesforce Platform MCP respondeu ao vivo. 🎉")
    except MCPError as e:
        print(f"\n✗ FALHOU: {e}\n")
        raise SystemExit(1)


if __name__ == "__main__":
    _cli()
