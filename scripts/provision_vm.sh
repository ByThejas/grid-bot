#!/usr/bin/env bash
#
# provision_vm.sh — Phase 11C VM bootstrap.
#
# Idempotent. Re-running this on an already-provisioned VM should be a
# no-op (skips users that exist, skips installs that are present,
# upgrades the venv only if requirements.txt changed).
#
# USAGE
#     # On a fresh DigitalOcean droplet, logged in as root:
#     curl -fsSL https://raw.githubusercontent.com/<user>/grid-bot/master/scripts/provision_vm.sh | bash -s
#     # OR clone the repo first and run:
#     bash scripts/provision_vm.sh
#
# WHAT IT DOES
#     1. Creates the `bot` user (non-root) with passwordless sudo.
#     2. Hardens SSH: disables root login + password auth, enables ufw.
#     3. Installs system packages: python3.11, git, build deps, fail2ban.
#     4. Clones the repo into /home/bot/grid-bot (skips if present).
#     5. Creates the venv and installs requirements.
#     6. Creates runtime/, logs/, keys/, data/ with bot ownership.
#     7. Installs the systemd unit files but does NOT start them — the
#        operator hand-launches the bot for the first testnet soak.
#
# WHAT IT DOES NOT DO
#     * Provision the droplet (operator does that on the DigitalOcean
#       console, attaches the reserved IP, uploads SSH key).
#     * Configure .env (operator does that interactively after this
#       script finishes — see docs/runbook/cloud_deploy.md step 3).
#     * Generate the Ed25519 keypair (separate step; runs as `bot`
#       user, not root).
#     * Update Binance API whitelist (manual on the Binance website).
#     * Start the bot (operator does it manually for the first launch).

set -euo pipefail

# ---------------------------------------------------------------------------
# Tunables (override via env vars before running, e.g.
#     REPO_URL=git@github.com:foo/grid-bot.git bash scripts/provision_vm.sh)
# ---------------------------------------------------------------------------

BOT_USER="${BOT_USER:-bot}"
BOT_HOME="/home/${BOT_USER}"
REPO_DIR="${BOT_HOME}/grid-bot"
REPO_URL="${REPO_URL:-https://github.com/REPLACE-ME/grid-bot.git}"
REPO_BRANCH="${REPO_BRANCH:-master}"
PYTHON_BIN="${PYTHON_BIN:-python3.11}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log()  { printf '\n\033[1;36m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '\n\033[1;33m[%s WARN]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
die()  { printf '\n\033[1;31m[%s FATAL]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; exit 1; }

require_root() {
    if [[ $EUID -ne 0 ]]; then
        die "this script must be run as root (got uid $EUID)"
    fi
}

# Run a command as the bot user, preserving its env / shell context.
as_bot() {
    sudo -u "${BOT_USER}" -H bash -lc "$*"
}

# ---------------------------------------------------------------------------
# Step 1 — bot user with passwordless sudo
# ---------------------------------------------------------------------------

step_create_bot_user() {
    log "Creating user '${BOT_USER}' (idempotent)"
    if id "${BOT_USER}" &>/dev/null; then
        log "  user exists, skipping"
    else
        useradd --create-home --shell /bin/bash "${BOT_USER}"
        log "  user created"
    fi
    # Passwordless sudo so the operator can SSH in as bot and still run
    # systemctl. Drop into /etc/sudoers.d so it's reversible.
    local sudoers="/etc/sudoers.d/${BOT_USER}"
    if [[ ! -f "${sudoers}" ]]; then
        echo "${BOT_USER} ALL=(ALL) NOPASSWD: ALL" > "${sudoers}"
        chmod 0440 "${sudoers}"
        log "  passwordless sudo configured at ${sudoers}"
    fi
    # Mirror root's authorized_keys so the SSH key the operator added
    # at droplet creation time also works for the bot user.
    if [[ -f /root/.ssh/authorized_keys ]]; then
        install -o "${BOT_USER}" -g "${BOT_USER}" -m 0700 -d "${BOT_HOME}/.ssh"
        install -o "${BOT_USER}" -g "${BOT_USER}" -m 0600 \
            /root/.ssh/authorized_keys "${BOT_HOME}/.ssh/authorized_keys"
        log "  SSH authorized_keys mirrored to ${BOT_USER}"
    else
        warn "no /root/.ssh/authorized_keys found — make sure you can ssh as ${BOT_USER} BEFORE you disable root login"
    fi
}

# ---------------------------------------------------------------------------
# Step 2 — system packages
# ---------------------------------------------------------------------------

step_install_packages() {
    log "Installing system packages (apt-get)"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -q
    apt-get install -y -q --no-install-recommends \
        git \
        ca-certificates \
        build-essential \
        software-properties-common \
        ufw \
        fail2ban \
        tmux \
        curl
    # Python 3.11 — Ubuntu 24.04 ships 3.12 by default; we want exactly
    # the version the project was developed against. add-apt-repository
    # for deadsnakes is the standard path. If the symlink already
    # resolves to the right version, skip.
    if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
        add-apt-repository -y ppa:deadsnakes/ppa
        apt-get update -q
        apt-get install -y -q --no-install-recommends \
            python3.11 python3.11-venv python3.11-dev
    fi
    log "  python: $(${PYTHON_BIN} --version)"
}

# ---------------------------------------------------------------------------
# Step 3 — SSH + UFW + fail2ban hardening
# ---------------------------------------------------------------------------

step_ssh_hardening() {
    log "Hardening sshd (disable root login + password auth)"
    local sshd_conf="/etc/ssh/sshd_config.d/00-grid-bot-hardening.conf"
    if [[ ! -f "${sshd_conf}" ]]; then
        cat > "${sshd_conf}" <<'EOF'
# Phase 11C SSH hardening. Drop-in overrides; the main sshd_config still
# applies for everything else.
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
EOF
        systemctl restart ssh || systemctl restart sshd
        log "  sshd restarted with hardened config"
    else
        log "  ${sshd_conf} already present, skipping"
    fi
}

step_ufw() {
    log "Configuring ufw (deny incoming except SSH)"
    ufw --force reset >/dev/null
    ufw default deny incoming
    ufw default allow outgoing
    ufw allow 22/tcp comment 'SSH'
    ufw --force enable
    log "  ufw enabled"
}

step_fail2ban() {
    log "Enabling fail2ban (default jails — sshd protection out of the box)"
    systemctl enable --now fail2ban
}

# ---------------------------------------------------------------------------
# Step 4 — clone repo + venv
# ---------------------------------------------------------------------------

step_clone_repo() {
    log "Cloning repo into ${REPO_DIR}"
    if [[ -d "${REPO_DIR}/.git" ]]; then
        log "  repo present, fetching latest on ${REPO_BRANCH}"
        as_bot "cd ${REPO_DIR} && git fetch --quiet origin && git checkout --quiet ${REPO_BRANCH} && git pull --quiet --ff-only origin ${REPO_BRANCH}"
    else
        if [[ "${REPO_URL}" == *"REPLACE-ME"* ]]; then
            die "REPO_URL is not configured. Set REPO_URL=... before running, or edit this script."
        fi
        as_bot "git clone --branch ${REPO_BRANCH} --depth 50 ${REPO_URL} ${REPO_DIR}"
    fi
}

step_create_venv() {
    log "Creating venv at ${REPO_DIR}/.venv"
    if [[ -f "${REPO_DIR}/.venv/bin/python" ]]; then
        log "  venv exists; upgrading pip + reinstalling requirements"
    else
        as_bot "${PYTHON_BIN} -m venv ${REPO_DIR}/.venv"
    fi
    as_bot "${REPO_DIR}/.venv/bin/pip install --quiet --upgrade pip"
    as_bot "${REPO_DIR}/.venv/bin/pip install --quiet -r ${REPO_DIR}/requirements.txt"
    log "  venv ready"
}

step_create_runtime_dirs() {
    log "Creating runtime/, logs/, keys/, data/ with bot ownership"
    as_bot "mkdir -p ${REPO_DIR}/runtime ${REPO_DIR}/logs ${REPO_DIR}/keys ${REPO_DIR}/data"
    chmod 700 "${REPO_DIR}/keys"
}

# ---------------------------------------------------------------------------
# Step 5 — systemd unit files (install, do not start)
# ---------------------------------------------------------------------------

step_install_systemd_units() {
    log "Installing systemd unit files (install only, NOT enabled)"
    install -m 0644 "${REPO_DIR}/deploy/grid-bot.service"  /etc/systemd/system/grid-bot.service
    install -m 0644 "${REPO_DIR}/deploy/dashboard.service" /etc/systemd/system/dashboard.service
    systemctl daemon-reload
    log "  units installed at /etc/systemd/system/{grid-bot,dashboard}.service"
    log "  NOT started — operator does manual testnet soak first per runbook"
}

# ---------------------------------------------------------------------------
# Closing summary
# ---------------------------------------------------------------------------

print_next_steps() {
    cat <<EOF

============================================================
PROVISIONING COMPLETE.

Next steps (manual, follow docs/runbook/cloud_deploy.md):

  1. ssh ${BOT_USER}@<this-vm-ip>
  2. cd ${REPO_DIR}
  3. cp .env.example .env && nano .env
       Fill in BINANCE_MAINNET_API_KEY, _API_SECRET, _WS_API_KEY,
              TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_CHAT_IDS
  4. chmod 600 .env
  5. source .venv/bin/activate
  6. python -m scripts.generate_ed25519_keypair --env mainnet
       (paste the public key into Binance)
  7. Update Binance HMAC API key whitelist with this VM's IP
  8. tmux
       python -m src.grid.grid_runner --env testnet
       (let it soak ~24h on testnet)
  9. After testnet soak: sudo systemctl enable --now dashboard grid-bot
============================================================
EOF
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    require_root
    log "phase 11C provisioning — bot_user=${BOT_USER} repo=${REPO_URL}"

    step_create_bot_user
    step_install_packages
    step_ssh_hardening
    step_ufw
    step_fail2ban
    step_clone_repo
    step_create_venv
    step_create_runtime_dirs
    step_install_systemd_units

    print_next_steps
}

main "$@"
