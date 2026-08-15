#!/usr/bin/env bash
#
# Deploy do "Site das IFs MCP" no Heroku — um app só que serve o site E as
# rotas /mcp/* na mesma origem (handshake MCP ao vivo, sem CORS/mixed-content).
#
# Pré-requisitos (uma vez):
#   1. Conta no Heroku + login:  heroku login   (interativo — abre o navegador)
#   2. As credenciais do External Client App do MCP em variáveis de ambiente
#      DESTA sessão de shell (NUNCA no repo):
#        export MCP_CLIENT_ID='...'
#        export MCP_CLIENT_SECRET='...'
#
# Uso:
#   ./deploy-heroku.sh [nome-do-app]
#
set -e
cd "$(dirname "$0")"

export PATH="/Users/aferraz/.devbar/pkgs/npm/24.18.0/node-v24.18.0-darwin-arm64/bin:$PATH"
APP_NAME="${1:-}"
# Contas Salesforce internas NÃO podem ter app pessoal — todo app precisa de um
# team. Passe o team via 2º argumento ou HEROKU_TEAM (ex.: se-latam).
TEAM="${2:-${HEROKU_TEAM:-}}"
# Alguns teams só rodam app dentro de um Private Space. Passe o space via 3º
# argumento ou HEROKU_SPACE (ex.: se-latam-internal). Com space, o team é inferido.
SPACE="${3:-${HEROKU_SPACE:-}}"

echo "▶ Montando o artefato (build.sh)..."
./build.sh

echo "▶ Verificando login no Heroku..."
if ! heroku auth:whoami >/dev/null 2>&1; then
  echo "✗ Você não está logado. Rode primeiro:  heroku login"
  exit 1
fi
echo "✓ Logado como: $(heroku auth:whoami)"

if [ -z "$MCP_CLIENT_ID" ] || [ -z "$MCP_CLIENT_SECRET" ]; then
  echo "✗ Defina MCP_CLIENT_ID e MCP_CLIENT_SECRET no ambiente antes de rodar."
  echo "  (Sem eles a aba MCP sobe, mas mostra 'MCP não configurado' — sem handshake.)"
  exit 1
fi

# git local isolado (só desta pasta de deploy)
if [ ! -d .git ]; then
  echo "▶ Inicializando repositório git local..."
  git init -q
  git add -A
  git -c commit.gpgsign=false commit -q -m "Site das IFs MCP — deploy inicial"
else
  git add -A
  git -c commit.gpgsign=false commit -q -m "Site das IFs MCP — atualização" || true
fi

# cria/associa o app. Se houver space, cria dentro dele (o team é inferido do
# space); senão, cria no team. Conta Salesforce interna exige team/space.
CREATEFLAGS=""
if [ -n "$SPACE" ]; then
  CREATEFLAGS="--space $SPACE"
elif [ -n "$TEAM" ]; then
  CREATEFLAGS="--team $TEAM"
fi
if [ -n "$APP_NAME" ]; then
  echo "▶ Criando/associando app: $APP_NAME ${SPACE:+(space: $SPACE)}${TEAM:+ ${SPACE:+· }team: $TEAM}"
  heroku apps:create "$APP_NAME" $CREATEFLAGS || heroku git:remote -a "$APP_NAME"
else
  echo "▶ Criando app com nome aleatório ${SPACE:+(space: $SPACE)}..."
  heroku apps:create $CREATEFLAGS
fi

# segredo → Config Vars (nunca no repo). --confirm-less: já estamos no app certo.
echo "▶ Enviando as credenciais MCP para as Config Vars (fora do repo)..."
heroku config:set MCP_CLIENT_ID="$MCP_CLIENT_ID" MCP_CLIENT_SECRET="$MCP_CLIENT_SECRET" \
  MCP_INSTANCE_URL="${MCP_INSTANCE_URL:-https://trailsignup-2979e1c6606a3b.my.salesforce.com}"

echo "▶ Enviando para o Heroku (git push)..."
git push heroku HEAD:refs/heads/main -f

echo "▶ Abrindo o app..."
heroku open

echo "✓ Deploy concluído."
