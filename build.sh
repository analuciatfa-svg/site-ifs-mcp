#!/usr/bin/env bash
#
# build.sh — monta o artefato de deploy a partir das FONTES (fonte única da
# verdade; nada é editado à mão aqui). Copia:
#   · o site MCP   (hub-instituicoes-agenticas-mcp/)  → ./public
#   · o cliente MCP (demo-kit/core/mcp_client.py)      → ./mcp_client.py
# Rode antes de qualquer deploy (o deploy-heroku.sh já chama isto).
#
set -e
cd "$(dirname "$0")"
RAIZ="$(cd .. && pwd)"

SITE="$RAIZ/hub-instituicoes-agenticas-mcp"
MCP="$RAIZ/demo-kit/core/mcp_client.py"

echo "▶ Limpando ./public e copiando o site MCP..."
rm -rf public && mkdir -p public
# copia o site inteiro, menos artefatos de deploy/node do próprio site
cp -Rp "$SITE/index.html" "$SITE/assets" "$SITE/simulacoes" public/
echo "  ✓ site copiado ($(find public -type f | wc -l | tr -d ' ') arquivos)"

echo "▶ Copiando o cliente MCP (stdlib)..."
cp -p "$MCP" ./mcp_client.py
echo "  ✓ mcp_client.py atualizado"

echo "✓ build pronto. Artefato: $(pwd)/public + app.py + mcp_client.py"
