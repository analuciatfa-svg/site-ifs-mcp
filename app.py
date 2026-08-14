#!/usr/bin/env python3
"""
app.py — Site das IFs MCP, versão de PRODUÇÃO (Heroku), tudo num app só.

Serve o site estático (pasta ./public) E as rotas /mcp/* na MESMA ORIGEM. Assim
o navegador chama /mcp/handshake e /mcp/jornada no próprio domínio HTTPS — sem
CORS, sem http://localhost, sem mixed content. O segredo do External Client App
vive SÓ nas Config Vars do Heroku (MCP_CLIENT_ID / MCP_CLIENT_SECRET); nunca no
repo e nunca no navegador.

Por que Python e não o server.js de Node: o handshake do MCP precisa do cliente
mcp_client.py (stdlib). O server.js só serve estático. Aqui um único processo faz
as duas coisas. (Diferente da ponte local servidor_ponte.py, este app NÃO fala
MIAW nem usa o sf CLI — o Heroku não tem o CLI; aqui só roteia dados via MCP.)

Rotas:
  GET  /                 → public/index.html
  GET  /<arquivo>        → estático de ./public (com proteção a path traversal)
  GET  /mcp/status       → { configurado, endpoint, instanceUrl }
  POST /mcp/handshake    → OAuth + initialize + tools/list (trilha, sem token)
  POST /mcp/jornada      → { jornada: leads|consignado|atendimento } → trace+registros
  OPTIONS *              → 204 (preflight CORS liberado)
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from mcp_client import MCPClient, MCPError, rodar_jornada  # stdlib puro

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"
PORT = int(os.environ.get("PORT", "8795"))

MIME = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8", ".json": "application/json; charset=utf-8",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml", ".ico": "image/x-icon",
    ".woff": "font/woff", ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8",
}

# Um cliente MCP por processo (o token/sessão são reusados; protegidos por lock).
_mcp = None
try:
    _c = MCPClient()
    if _c.configurado:
        _mcp = _c
except Exception:
    _mcp = None
_mcp_lock = threading.Lock()


def _handshake():
    if not _mcp:
        raise RuntimeError("MCP não configurado: defina MCP_CLIENT_ID e "
                           "MCP_CLIENT_SECRET nas Config Vars do app.")
    with _mcp_lock:
        return _mcp.handshake()


def _jornada(nome):
    if not _mcp:
        return {"ok": False, "erro": "MCP não configurado no servidor "
                "(defina MCP_CLIENT_ID e MCP_CLIENT_SECRET nas Config Vars)."}
    with _mcp_lock:
        return rodar_jornada(_mcp, nome)


def _status():
    return {"configurado": bool(_mcp),
            "endpoint": (_mcp.endpoint if _mcp else None),
            "instanceUrl": (_mcp.instance_url if _mcp else None)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/mcp/status":
            return self._send(200, json.dumps(_status()))
        # estático
        rel = path.lstrip("/") or "index.html"
        alvo = (PUBLIC / rel).resolve()
        if not str(alvo).startswith(str(PUBLIC)):
            return self._send(403, "Forbidden", "text/plain; charset=utf-8")
        if alvo.is_dir():
            alvo = alvo / "index.html"
        if not alvo.exists():
            return self._send(404, "<h1>404</h1><p>Página não encontrada.</p>",
                              "text/html; charset=utf-8")
        ctype = MIME.get(alvo.suffix.lower(), "application/octet-stream")
        return self._send(200, alvo.read_bytes(), ctype)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/mcp/handshake":
            try:
                return self._send(200, json.dumps(_handshake()))
            except Exception as e:
                return self._send(502, json.dumps({"ok": False, "erro": str(e)}))
        if path == "/mcp/jornada":
            nome = (self._body().get("jornada") or "leads").strip()
            res = _jornada(nome)
            return self._send(200 if res.get("ok") else 502, json.dumps(res))
        return self._send(404, json.dumps({"erro": "nao encontrado"}))


def main():
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Site das IFs MCP (produção) ouvindo na porta {PORT} · "
          f"MCP {'CONFIGURADO' if _mcp else 'sem credencial'}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
