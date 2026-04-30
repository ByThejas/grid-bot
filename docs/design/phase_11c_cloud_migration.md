# Phase 11C — Cloud VM Migration

**Status:** design — drafted 2026-04-29
**Owner:** Thejas U
**Depends on:** Phase 11A (Telegram alerts), Phase 11B (local dashboard)
**Successor:** Phase 11D (dynamic balance-adjusted sizing)

---

## 1. Goals & Non-Goals

### Goals
- Bot runs on a small cloud VM, not on the operator's laptop.
- Bot auto-restarts on crash via `systemd`.
- The VM has a **static elastic IP** that is whitelisted on Binance exactly once. No more IP rotation churn (the 2026-04-28 + 2026-04-29 incidents go away permanently).
- Operator can put the laptop to sleep, travel, lose power — the bot keeps trading.
- Existing kill switches (Telegram `/halt`, dashboard halt button, `runtime/HALT` sentinel) all continue to work unchanged.
- Dashboard reachable from the laptop via SSH tunnel; no public HTTP exposure.

### Non-Goals
- **HA / multi-region** — one VM is enough. If it dies, restore from a daily snapshot. Crypto grid bots are not life-critical infrastructure.
- **Public web dashboard** — SSH tunnel keeps the attack surface zero. A public dashboard would need TLS + auth + rate limiting; out of scope.
- **Container orchestration** — no Kubernetes, no Docker Swarm. Single Python process under `systemd`. The original master prompt's EKS plan was rejected on 2026-04-23 for being comically over-engineered for one bot.
- **AWS Secrets Manager / Vault / KMS** — `.env` with `chmod 600` is the right level of security for a single-operator setup. Revisit if/when the bot manages serious capital.
- **Auto-scaling / horizontal sharding** — one bot, one process, one VM, forever.
- **Continuous deployment / CI** — manual deploys are fine at this scale. Adding GitHub Actions for one Python process is overhead without payoff.
- **Database migration** — `data/marketdata.db` (SQLite) lives on the VM filesystem. If we lose it, the ingestor backfills.

---

## 2. Provider Choice — DigitalOcean

**Decision:** DigitalOcean Basic Droplet, 1GB RAM / 1 vCPU / 25GB SSD, $6/month.

| Provider | 1GB Plan | Static IP | Pros | Cons |
|---|---|---|---|---|
| **DigitalOcean** | $6/mo basic droplet (1GB/1vCPU/25GB) | Free (Reserved IP) | Simplest UI, predictable pricing, fast SSD, good docs | Smaller datacenter footprint than AWS |
| AWS t4g.small | ~$10/mo (1yr reserved) | Free (Elastic IP, while attached) | Largest ecosystem, AWS familiarity if needed later | More complex IAM/VPC setup, billing surprises |
| Hetzner Cloud | €4.51/mo CX11 (2GB) | Free | Cheapest in EU | Shorter track record outside Europe |
| Linode (Akamai) | $5/mo Nanode (1GB) | Free | Solid alternative | Acquired by Akamai, future direction unclear |

**Why DigitalOcean specifically:**
- Single-VM ops are exactly what they're optimized for.
- Reserved IP is free as long as it's attached (unattached: $4/mo) — no IP rotation possible.
- Daily snapshots cost ~$1.20/mo on this droplet size — automatic disaster recovery.
- 1-click DigitalOcean monitoring is enough for a single Python process.
- No need to model VPCs, security groups, NAT gateways, or other AWS ceremony for one VM.

**Region:** `BLR1` (Bangalore) since the operator is in India and Binance has multiple low-latency endpoints. Asia-Pacific in general; secondary choice `SGP1` (Singapore) if BLR1 has any issues.

**Total monthly cost estimate:** $6 droplet + ~$0.30–0.50 daily snapshots ≈ **$6.50/mo** ≈ ₹540/mo. Well inside the "comically cheaper than EKS" budget.

**Why 1GB and not the cheaper 512MB tier:** the bot's RSS during the 25.5h Phase 10C soak peaked at ~120MB, plus Ubuntu base + dashboard adds ~210MB. 512MB after OS overhead leaves no room for numpy spikes during S&D zone recomputation, SQLite buffer cache, or Phase 11D's balance-poller. OOM-killer terminates Python without a [CRASH] alert (the kernel doesn't go through the language runtime), so the failure mode is silent. The extra $2/mo is cheap insurance.

---

## 3. Architecture

### What runs on the VM

```
   ┌─────────────────────────────────────────┐
   │ DigitalOcean droplet (Bangalore)        │
   │  static IP: <reserved-ip>               │
   │                                          │
   │  ┌──────────────────────┐                │
   │  │ systemd: grid-bot    │ ──→ Binance API
   │  │  python -m src.grid… │ ←── Binance WS
   │  └──────────────────────┘                │
   │           │                              │
   │           ▼                              │
   │  ┌──────────────────────┐                │
   │  │ runtime/STATUS.json  │                │
   │  │ runtime/HALT         │                │
   │  │ logs/grid_bot.log    │                │
   │  │ data/marketdata.db   │                │
   │  └──────────────────────┘                │
   │           ▲                              │
   │           │                              │
   │  ┌──────────────────────┐                │
   │  │ systemd: dashboard   │ ──→ 127.0.0.1:8080
   │  │  python -m src.dash… │                │
   │  └──────────────────────┘                │
   └─────────────────────────────────────────┘
                 ▲
                 │ SSH tunnel: laptop:8080 → vm:8080
                 │
   ┌─────────────────────────────────────────┐
   │ Operator's laptop (any time, anywhere)  │
   │  browser: http://localhost:8080         │
   └─────────────────────────────────────────┘
                 ▲
                 │ Telegram /status /halt /help
                 │
   ┌─────────────────────────────────────────┐
   │ Operator's phone                        │
   └─────────────────────────────────────────┘
```

Every observation channel from Phases 11A and 11B keeps working unchanged. The laptop is no longer in the trading hot path.

### systemd units

Two units, both as a non-root user (`bot`):

```
deploy/grid-bot.service     — the trading process, Restart=always, RestartSec=5
deploy/dashboard.service    — the dashboard, Restart=always, listens on 127.0.0.1:8080
```

Both:
- `User=bot` (not root)
- `WorkingDirectory=/home/bot/grid-bot`
- `EnvironmentFile=/home/bot/grid-bot/.env`
- `ExecStart=/home/bot/grid-bot/.venv/bin/python -m src.grid.grid_runner --env mainnet --live` (or `... -m src.dashboard.server`)
- `Restart=always` so a transient crash recovers without operator intervention
- `RestartSec=5` so a crashloop doesn't pin CPU

Enable both at boot: `systemctl enable grid-bot dashboard`.

### How the operator interacts with the VM

| Action | Channel |
|---|---|
| Start / stop bot | `ssh bot@<ip>` then `systemctl start grid-bot` (one-time after migration) |
| View status from anywhere | Telegram `/status` |
| Halt from anywhere | Telegram `/halt` |
| View dashboard | SSH tunnel, then `http://localhost:8080` in laptop browser |
| Tail logs | `ssh bot@<ip> tail -f grid-bot/logs/grid_bot.log` |
| Update code | `ssh bot@<ip>`, `cd grid-bot`, `git pull`, `systemctl restart grid-bot` |

---

## 4. Security Model

### Threat model
- **Compromised laptop / SSH key** — attacker can SSH to VM, has full bot access. Mitigated by SSH key passphrase + `authorized_keys` allow-list (no password auth).
- **Compromised Binance API key** — attacker can place trades on the operator's account. Mitigated by IP whitelist (only the VM's elastic IP), withdrawal disabled on the API key.
- **Compromised VM** — attacker reads `.env`, has Binance keys + Telegram bot token. Mitigated by `chmod 600 .env`, root login disabled, fail2ban on SSH, regular `apt-get upgrade`.
- **DigitalOcean account compromise** — attacker can destroy the VM or read snapshots. Mitigated by 2FA on DO account.

### What we explicitly don't do in 11C
- Public dashboard (no public HTTPS, no auth layer to maintain).
- Encryption at rest beyond filesystem defaults (DO encrypts droplet storage by default).
- Network policies beyond the default UFW rules (described below).
- Key rotation automation (manual, when needed).

### Firewall (UFW) rules
```
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp        # SSH (consider rate-limiting or non-default port)
ufw enable
```

Port 8080 (dashboard) is NOT exposed externally — it binds to 127.0.0.1 only, unchanged from Phase 11B. Access is via SSH tunnel, which is authenticated by the SSH key.

### `.env` handling
- Created on the VM directly (do NOT scp the laptop's `.env` — generate fresh keys for the VM where possible).
- `chmod 600 /home/bot/grid-bot/.env` immediately after creation.
- Owned by `bot:bot`, not root.
- Never logged. Never committed (`.gitignore` already covers it).

### Ed25519 keypair handling
- Generate a fresh keypair **on the VM** via `python -m scripts.generate_ed25519_keypair --env mainnet`.
- Upload the public key to Binance (replaces the laptop's public key).
- Private key lives at `/home/bot/grid-bot/keys/ed25519_mainnet_priv.pem` with `chmod 600`.
- Never copy the private key off the VM.

### Binance API key
- Existing mainnet HMAC key gets a new IP whitelist: replace the laptop's home IP with the VM's elastic IP.
- Withdrawal permission stays disabled (already true).
- Restricted to spot trading + reading market data.

---

## 5. Failure Modes

| Scenario | Behavior |
|---|---|
| Bot process crashes | `systemd Restart=always` brings it back within 5s. `[CRASH]` Telegram alert fires before exit (Phase 11A). |
| VM restarts (DO maintenance, kernel update) | Both services come up on boot via `systemctl enable`. ~30s downtime. Operator gets `[BOOT]` alert. |
| VM destroyed | Restore from latest daily snapshot. ~5min RTO. Telegram silence is the operator's signal — Phase 11C+ could add an external dead-man's-switch (out of scope for 11C v1). |
| Disk fills (logs, marketdata.db) | Logs already rotate at 50MB × 5 (Phase 10D). marketdata.db is ~10MB/day; would take months to fill 25GB. `du` cron alert is a future nice-to-have. |
| Network partition (DO ↔ Binance) | Same as Phase 10D's laptop network drops, but much rarer. Bot retries via existing reconnection logic. |
| Binance IP whitelist mismatch | Manual fix on Binance + restart. Should never happen with elastic IP unless DO explicitly reassigns it (which they don't on Reserved IPs). |
| SSH key lost | Operator can recover via DigitalOcean console (still requires DO account auth). |

---

## 6. Deployment Plan

### Prerequisites (operator does first)
1. DigitalOcean account with billing + 2FA enabled.
2. SSH keypair on laptop (`ssh-keygen -t ed25519`) — public key gets uploaded to DO during droplet creation.
3. Telegram bot already working (Phase 11A).
4. Mainnet Binance API key with current laptop IP whitelisted (existing).

### Step-by-step migration (each step in a separate session)

**Phase 11C-1: Provision VM** (no bot work yet)
```
1.1  Create droplet (Bangalore, basic 1GB, Ubuntu 24.04 LTS)
1.2  Create reserved IP, attach to droplet
1.3  Add SSH public key to droplet
1.4  Test SSH login: ssh root@<ip>
1.5  Note the IP for Binance whitelist update later
```

**Phase 11C-2: VM bootstrap** (run scripts/provision_vm.sh)
```
2.1  Create non-root user `bot` with passwordless sudo
2.2  Disable root SSH login, install fail2ban, configure UFW
2.3  Install Python 3.11+, git, build deps
2.4  Clone the repo into /home/bot/grid-bot
2.5  Create venv, pip install -r requirements.txt
2.6  Create empty runtime/, logs/, keys/ dirs with bot ownership
```

**Phase 11C-3: Configure secrets**
```
3.1  ssh bot@<ip>
3.2  Create .env from .env.example, fill in:
     - BINANCE_MAINNET_API_KEY (existing HMAC, will re-whitelist)
     - BINANCE_MAINNET_API_SECRET
     - BINANCE_MAINNET_WS_API_KEY (placeholder, will replace after step 4)
     - BINANCE_MAINNET_WS_PRIVATE_KEY_PATH=keys/ed25519_mainnet_priv.pem
     - BINANCE_ENV=mainnet
     - TELEGRAM_BOT_TOKEN
     - TELEGRAM_ALLOWED_CHAT_IDS
3.3  chmod 600 .env
```

**Phase 11C-4: Generate fresh Ed25519 keypair on the VM**
```
4.1  cd /home/bot/grid-bot
4.2  source .venv/bin/activate
4.3  python -m scripts.generate_ed25519_keypair --env mainnet
4.4  Copy the printed public key
4.5  Update the existing Ed25519 key on Binance (or create a new one) with this public key
4.6  Update BINANCE_MAINNET_WS_API_KEY in .env if Binance issued a new key id
```

**Phase 11C-5: Update Binance HMAC key whitelist**
```
5.1  Binance API management page
5.2  Edit existing mainnet HMAC key
5.3  Replace home IP with VM elastic IP
5.4  Save (3 min propagation)
```

**Phase 11C-6: Install systemd units + first launch (testnet first)**
```
6.1  sudo cp deploy/grid-bot.service /etc/systemd/system/
6.2  sudo cp deploy/dashboard.service /etc/systemd/system/
6.3  sudo systemctl daemon-reload
6.4  Edit grid-bot.service to use --env testnet for the first launch
6.5  sudo systemctl start dashboard
6.6  sudo systemctl start grid-bot
6.7  journalctl -u grid-bot -f       # watch logs
6.8  Verify [BOOT] arrives on Telegram, /status works
6.9  Open SSH tunnel from laptop, hit dashboard at http://localhost:8080
6.10 Send /halt from phone, verify clean shutdown
6.11 Flip --env back to mainnet --live, restart grid-bot
6.12 Watch first hour carefully
```

**Phase 11C-7: 24-hour soak with laptop bot still running** (overlap, not cutover)
```
7.1  VM bot trades on mainnet
7.2  Laptop bot is HALTED (or never restarted after this session)
7.3  Watch for fills, halts, recenters
7.4  Verify Telegram alerts arrive from VM bot
7.5  Verify dashboard via SSH tunnel
```

**Phase 11C-8: Decommission laptop deployment**
```
8.1  Confirm VM has been stable for 24h+
8.2  Stop laptop bot if still running
8.3  Archive laptop .env (back up to a password manager or encrypted file)
8.4  Remove or comment out laptop scheduling (if any)
8.5  Update memory: bot now runs on VM, not laptop
```

**Phase 11C-9: Set up daily snapshots**
```
9.1  DigitalOcean control panel → droplet → Snapshots → enable daily snapshots
9.2  Cost: ~$1.20/mo at this droplet size
9.3  Test restore once (create new droplet from snapshot, verify it boots)
```

---

## 7. Rollback

If something goes wrong on the VM, three escape hatches in order of preference:

1. **Halt the VM bot via Telegram /halt.** Same one-line trigger as Phase 11A. Bot cancels orders, exits cleanly. systemd will respawn it; if you don't want that, `sudo systemctl stop grid-bot && sudo systemctl disable grid-bot`.
2. **Re-launch the laptop bot.** Update Binance whitelist back to laptop home IP, run `python -m src.grid.grid_runner --env mainnet --live` in CMD as before. Cost: ~3 min IP propagation. Acceptable for any non-emergency.
3. **Destroy the VM.** Last resort. Destroys all logs and the marketdata.db, but the .env on disk is gone too — clean security state. Restore from snapshot to recover.

The laptop bot config remains a working fallback for at least the first month of VM operation. Don't delete it.

---

## 8. Open Decisions Before Implementation

These need a yes/no from the operator before we provision. Defaults shown in **bold** are what we'll use if no input.

1. **Provider — DigitalOcean vs. AWS vs. Hetzner?** Default: **DigitalOcean**. Cheapest with simplest UX for one VM.
2. **Droplet region — BLR1 (Bangalore) vs. SGP1 (Singapore)?** Default: **BLR1**. Lowest latency from operator's location.
3. **Droplet size — basic 1GB vs. 2GB?** Default: **1GB ($4/mo)**. Bot's RSS during the 25.5h Phase 10C soak peaked at ~120MB. 1GB is plenty.
4. **Daily snapshots on or off?** Default: **on**. ~$1.20/mo for 1-click disaster recovery.
5. **Generate fresh Ed25519 keypair on VM, or copy existing from laptop?** Default: **generate fresh**. Avoids private key crossing untrusted network. Old key gets retired on Binance.
6. **Use the existing mainnet HMAC API key (just re-whitelist), or create a new one?** Default: **existing key**. Less rotation churn; whitelist update is fast.
7. **First VM launch on testnet or straight to mainnet?** Default: **testnet first**. One-day testnet smoke proves the systemd setup before any real money rides on it.

---

## 9. Implementation Plan (After Decisions Locked)

```
Step 1  Design doc — THIS DOC                                                ← 11C-Plan
Step 2  systemd unit files (deploy/grid-bot.service, deploy/dashboard.service)
Step 3  Bootstrap script (scripts/provision_vm.sh) — idempotent
Step 4  Cloud deploy runbook (docs/runbook/cloud_deploy.md)
Step 5  Operator provisions DO droplet + reserved IP                          (manual)
Step 6  Operator runs bootstrap, configures secrets                           (manual)
Step 7  First testnet launch on VM                                            (operator + me)
Step 8  Mainnet on VM with laptop bot halted                                  (operator + me)
Step 9  24-hour mainnet soak                                                  (passive)
Step 10 Decommission laptop, snapshot enabled                                 (operator)
```

Steps 1–4 are file-only and can be drafted while the laptop canary keeps soaking. Steps 5–10 require the operator's hands.

---

## 10. Out of Scope for 11C (Captured for Future Phases)

- **External dead-man's-switch** — pager/email if no Telegram heartbeat in 30min. Belongs to Phase 12 (production observability).
- **Public dashboard with TLS + auth** — only if the operator wants to monitor from a phone outside Telegram. SSH tunnel covers the laptop case.
- **Multi-VM HA** — overkill for one bot. Revisit if the bot manages serious capital.
- **Terraform / IaC** — manual provisioning is fine for one VM. Add IaC if 11C ever needs to be replicated.
- **Centralized logging (Loki, CloudWatch)** — `journalctl` + tailing the log file is enough.
- **AWS Secrets Manager / Vault** — `.env` with `chmod 600` is the right level for v1.
- **Phase 11D dynamic balance-adjusted sizing** — comes after the cloud bot has been stable for at least a week.
