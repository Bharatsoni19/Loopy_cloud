#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
#  deploy_ec2.sh
#  Pull the latest code on an Amazon Linux 2023 EC2 box and (re)deploy the
#  full Loopy stack with docker compose. Idempotent — safe to re-run from
#  the GitHub Actions deploy job or by hand over SSH.
#
#  Required env:
#    REPO_URL          e.g. https://github.com/<you>/loopy-cloud.git
#    LOOPY_JWT_SECRET  HS256 secret for the payments JWT
#    LOOPY_RAW_BUCKET  S3 bucket name where payment events get archived
#    AWS_REGION        e.g. ap-south-1
#  Optional:
#    LOOPY_LLM=bedrock to enable Bedrock-backed RAG answers
#    APP_DIR=/opt/loopy           clone destination
#    BRANCH=main                  branch to deploy
# ═══════════════════════════════════════════════════════════════════════
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/loopy}"
BRANCH="${BRANCH:-main}"
REPO_URL="${REPO_URL:?REPO_URL is required}"

log() { printf '\n[deploy] %s\n' "$*"; }

# 1. Prerequisites (only the first run does any work) ────────────────────
if ! command -v docker >/dev/null 2>&1; then
  log "installing docker + git"
  sudo dnf install -y docker git
  sudo systemctl enable --now docker
  sudo usermod -aG docker "$(whoami)" || true
fi
if ! docker compose version >/dev/null 2>&1; then
  log "installing docker compose plugin"
  sudo dnf install -y docker-compose-plugin || {
    sudo mkdir -p /usr/local/lib/docker/cli-plugins
    sudo curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 \
      -o /usr/local/lib/docker/cli-plugins/docker-compose
    sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
  }
fi

# 2. Sync source ─────────────────────────────────────────────────────────
if [ ! -d "$APP_DIR/.git" ]; then
  log "cloning $REPO_URL -> $APP_DIR"
  sudo mkdir -p "$APP_DIR" && sudo chown "$(whoami)" "$APP_DIR"
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
else
  log "pulling latest on $BRANCH"
  git -C "$APP_DIR" fetch --depth=1 origin "$BRANCH"
  git -C "$APP_DIR" reset --hard "origin/$BRANCH"
fi
cd "$APP_DIR"

# 3. Render env file (compose reads .env automatically) ──────────────────
log "writing .env"
cat > .env <<EOF
LOOPY_JWT_SECRET=${LOOPY_JWT_SECRET:-please-change-me}
LOOPY_RAW_BUCKET=${LOOPY_RAW_BUCKET:-}
AWS_REGION=${AWS_REGION:-ap-south-1}
LOOPY_LLM=${LOOPY_LLM:-none}
EOF
chmod 600 .env

# 4. Bring the stack up ──────────────────────────────────────────────────
log "docker compose up (rebuild & restart)"
sg docker -c "docker compose pull --ignore-pull-failures || true"
sg docker -c "docker compose up -d --build --remove-orphans"

# 5. Smoke checks ────────────────────────────────────────────────────────
log "waiting for health endpoints"
ok=0
for i in $(seq 1 30); do
  if curl -fs http://localhost/healthz >/dev/null \
     && curl -fs http://localhost/api/pay/health >/dev/null; then
    ok=1; break
  fi
  sleep 2
done
if [ "$ok" -ne 1 ]; then
  log "HEALTH CHECK FAILED — last logs:"
  sg docker -c "docker compose logs --tail=80"
  exit 1
fi

log "deployed OK ✔  →  http://$(curl -s ifconfig.me || echo this-host)/"
sg docker -c "docker compose ps"
