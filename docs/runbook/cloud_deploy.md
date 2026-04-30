# Phase 11C Cloud Deploy Runbook

**Audience:** the operator (you). Self-contained walkthrough for migrating the bot from the laptop to a DigitalOcean droplet. Designed to be followed top-to-bottom across one or two sessions; every step has explicit commands and a "did it work" check.

**Prerequisites:**
- DigitalOcean account with billing + 2FA enabled
- SSH keypair on the laptop (`ssh-keygen -t ed25519` if you don't have one)
- Phone Telegram with the existing bot accessible
- Binance account with the current mainnet HMAC API key whose IP whitelist you have permission to edit
- ~30 minutes for steps 1–6, then a 24-hour testnet soak before step 7

**Estimated cost:** **~$6.50/mo** (1GB droplet at $6 + ~$0.30–0.50/mo daily snapshots).

**See also:** [docs/design/phase_11c_cloud_migration.md](../design/phase_11c_cloud_migration.md) for the design rationale; [deploy/README.md](../../deploy/README.md) for systemd quick-reference.

---

## Step 1 — Provision the droplet (DigitalOcean console)

You only do this once. Total time: ~5 minutes.

1. Sign in to https://cloud.digitalocean.com.
2. **Create → Droplets**.
3. Choose:
   - **Region:** Bangalore (BLR1)
   - **Image:** Ubuntu 24.04 (LTS) x64
   - **Size:** Basic → Regular → **$6/mo (1 GB / 1 vCPU / 25 GB SSD)**
   - **Authentication:** SSH Key. Paste your laptop's `~/.ssh/id_ed25519.pub` (or upload via the UI).
   - **Hostname:** `grid-bot`
   - **Backups:** OFF for now (we'll enable cheaper snapshots in step 9).
4. Click **Create Droplet**, wait ~60s for it to boot. Note the **public IPv4** — we'll replace this with a reserved IP next.
5. **Networking → Reserved IPs → Reserve IP → Assign to grid-bot droplet.** Note the reserved IP — this is what goes in the Binance whitelist. **Never delete this IP** as long as you're running the bot, even briefly: re-creating it gives you a different address, defeating the entire point of this phase.
6. Test SSH: `ssh root@<RESERVED_IP>` from your laptop. If you can't log in, fix this before proceeding — it's only going to get harder once we lock down sshd.

✅ **Done when:** you can `ssh root@<RESERVED_IP>` and see a fresh Ubuntu shell.

---

## Step 2 — Run the provisioning script

Total time: ~5 minutes (mostly apt download).

On the droplet, as **root**:

```bash
# Clone the repo (uses HTTPS; no SSH key on the VM yet).
apt-get update && apt-get install -y git
git clone https://github.com/<your-user>/grid-bot.git /tmp/grid-bot

# Run the bootstrap. Override REPO_URL to point at your fork if needed.
REPO_URL=https://github.com/<your-user>/grid-bot.git \
  bash /tmp/grid-bot/scripts/provision_vm.sh
```

The script:
- Creates user `bot` with passwordless sudo
- Mirrors your SSH key so you can log in as bot
- Disables root login + password auth on sshd
- Configures UFW (allow :22 only)
- Enables fail2ban
- Installs Python 3.11 from deadsnakes
- Clones the repo into `/home/bot/grid-bot`
- Creates the venv and installs requirements
- Installs both systemd unit files (does NOT enable them yet)

✅ **Done when:** the script prints `PROVISIONING COMPLETE`. From a NEW terminal on your laptop, verify:

```bash
ssh bot@<RESERVED_IP>
```

You should land in `/home/bot` as user `bot`. **Don't disconnect the root SSH session yet** — keep it open as a fallback in case the bot login is broken. Once you've verified bot login works in a separate terminal, the root session is no longer needed.

---

## Step 3 — Configure secrets

Total time: ~5 minutes.

As `bot` on the droplet:

```bash
cd ~/grid-bot
cp .env.example .env
nano .env
```

Fill in (exact same values as your laptop's `.env`, plus a placeholder for the WS key — we generate it fresh in step 4):

```
BINANCE_MAINNET_API_KEY=...                       # existing HMAC, will re-whitelist
BINANCE_MAINNET_API_SECRET=...                    # existing HMAC secret
BINANCE_MAINNET_WS_API_KEY=PLACEHOLDER            # filled in step 4
BINANCE_MAINNET_WS_PRIVATE_KEY_PATH=keys/ed25519_mainnet_priv.pem
BINANCE_ENV=mainnet
BINANCE_LIVE=                                     # leave blank; --live flag is in the unit file
TELEGRAM_BOT_TOKEN=...                            # same as laptop
TELEGRAM_ALLOWED_CHAT_IDS=...                     # your chat id
```

Then lock the file:

```bash
chmod 600 .env
ls -la .env       # should show -rw------- bot bot
```

✅ **Done when:** `cat .env | wc -l` reports >10 lines and the file is `chmod 600`.

---

## Step 4 — Generate fresh Ed25519 keypair on the VM

Total time: ~3 minutes.

The Ed25519 private key signs Binance's `session.logon`. We generate a NEW one on the VM rather than copying the laptop's, because copying private keys across networks is bad hygiene. Old key gets retired on Binance.

```bash
cd ~/grid-bot
source .venv/bin/activate
python -m scripts.generate_ed25519_keypair --env mainnet
```

The script prints the **public key** (a base64 string). Copy it — you'll paste it on Binance.

On the Binance website:
1. **API Management** → find your existing mainnet WS API key (the Ed25519-typed one)
2. Either edit it (if Binance allows public-key replacement) or create a new Ed25519 key
3. Paste the new public key
4. Save. Note the resulting **API key id** (Binance shows it after creation).

Back on the VM:

```bash
nano .env
# Replace BINANCE_MAINNET_WS_API_KEY=PLACEHOLDER with the new key id Binance gave you.
```

✅ **Done when:** `cat keys/ed25519_mainnet_priv.pem | head -1` shows `-----BEGIN PRIVATE KEY-----` and the corresponding public key is registered on Binance.

---

## Step 5 — Update Binance HMAC IP whitelist

Total time: ~2 minutes + 3-min propagation wait.

On the Binance website:
1. **API Management** → find the mainnet HMAC key (the one used for REST orders)
2. Click **Edit restrictions**
3. **IP access restrictions:** remove your laptop's home IP, add the **reserved IP** from step 1
4. Save and **wait 3 minutes** for propagation. Bots that try to call REST during propagation get -2015.

✅ **Done when:** the only IP whitelisted on the HMAC key is the droplet's reserved IP.

---

## Step 6 — First launch on testnet (manual, in tmux)

Total time: ~5 minutes to start, then leave running for 24 hours.

We deliberately don't enable the systemd unit yet. The first launch is hand-run on testnet inside `tmux` so you can watch logs scroll, kill it cleanly with Ctrl+C, and verify everything works without auto-restart getting in the way.

```bash
ssh bot@<RESERVED_IP>
tmux new -s testnet-soak

# Inside the tmux session:
cd ~/grid-bot
source .venv/bin/activate
python -m src.grid.grid_runner --env testnet
```

You should see:
- `MAINNET / TESTNET BANNER` — confirm `testnet=True`
- `telegram alerter started ... listener=True`
- `connected to binance testnet=True`
- `grid placed ... symbol=BTCUSDT placed=4 ...`
- `grid placed ... symbol=ETHUSDT placed=4 ...`

Within ~10s you should get a `[BOOT] testnet ...` Telegram alert. Send `/help` and `/status` from your phone to verify the listener is receiving from the VM.

**Detach tmux** with `Ctrl+b d`. The bot keeps running. Reconnect later with `tmux attach -t testnet-soak`.

✅ **Done when:** `[BOOT]` arrived, `/status` reply works, no errors in the first ~5 minutes of logs.

**Wait at least 24 hours** before step 7. Watch for fills via Telegram. If anything blows up, kill the testnet bot (`/halt` from phone) and triage before going to mainnet.

---

## Step 7 — Cutover to mainnet (with laptop bot already halted)

Total time: ~5 minutes.

**Pre-cutover checklist:**
- Laptop bot is HALTED (`/halt` from phone if still running)
- Laptop bot's open orders are all cancelled (verify on Binance "Open Orders")
- 24h+ on testnet on the VM with no errors
- Wallet has enough BTC + ETH for the SELL side (≥ 0.0007 BTC + ≥ 0.011 ETH after the Convert-fee bite)

On the VM:

```bash
ssh bot@<RESERVED_IP>
tmux attach -t testnet-soak
# Inside tmux:
Ctrl+C       # stop the testnet bot
exit         # exit the tmux session

# Now enable the systemd units for mainnet (the ExecStart hardcodes
# --env mainnet --live so you don't have to remember any flags).
sudo systemctl enable --now dashboard
sudo systemctl enable --now grid-bot
sudo systemctl status grid-bot --no-pager
```

Watch the journal:

```bash
sudo journalctl -u grid-bot -f
```

You should see the `MAINNET` banner, `[BOOT] mainnet ...` Telegram alert, and grid placement within ~30 seconds.

In a separate laptop terminal, **open the SSH tunnel** so you can hit the dashboard:

```bash
ssh -N -L 8080:127.0.0.1:8080 bot@<RESERVED_IP>
# leave this terminal open; it forwards laptop:8080 → vm:8080
```

In your laptop browser: `http://localhost:8080`. You should see the dashboard rendering the VM's STATUS.json, including the HALT button.

✅ **Done when:** `[BOOT] mainnet` arrived, dashboard reachable via tunnel, journal shows orders placed, Binance "Open Orders" shows the new orders coming from the VM's IP.

---

## Step 8 — Decommission the laptop deployment

Total time: ~2 minutes.

After 24h+ of clean mainnet operation on the VM:

1. **Archive the laptop's `.env`** to a password manager or encrypted vault (it has working API keys; don't lose it as a fallback).
2. The laptop's bot can stay on disk — no need to delete the repo. Treat it as a fallback that needs:
   - Re-whitelist your home IP on Binance (3 min wait)
   - `python -m src.grid.grid_runner --env mainnet --live`
3. Update the project memory: bot now runs on the VM, not the laptop.

✅ **Done when:** you have a documented rollback path (re-whitelist + run on laptop) and you're not relying on the laptop being on for the bot to work.

---

## Step 9 — Enable daily snapshots

Total time: ~1 minute.

DigitalOcean control panel → your droplet → **Snapshots** → enable **automatic backups** (or use the "Snapshots" feature for cheaper weekly+manual snapshots).

- Cost: ~$0.30–0.50/mo at this droplet's used-disk size
- Retention: 7 days for automatic snapshots

Test restore once, ideally a week or two in: create a new droplet from the latest snapshot, ssh in, verify the bot can boot and that `runtime/`, `logs/`, `data/` are all present. Then destroy the test droplet.

✅ **Done when:** at least one snapshot is listed in the DO console.

---

## Failure modes encountered during deployment

### `ssh: connection refused` after running provision_vm.sh

You disabled root login but couldn't SSH as `bot`. Recovery options, easiest first:
1. **DigitalOcean web console** (Droplet → Console). Logs you in as root via the OOB serial console. Then:
   ```bash
   cat /home/bot/.ssh/authorized_keys    # is it empty?
   ```
   If empty, copy `/root/.ssh/authorized_keys` into it manually.
2. **Last resort:** rebuild the droplet from scratch. Reserved IP stays attached; just re-attach to the new droplet.

### Bot keeps `[CRASH]`-alerting and restarting in a loop

`systemd` is doing its job — auto-restart on crash. Find the root cause:

```bash
sudo journalctl -u grid-bot --since "10 minutes ago" | grep -E "ERROR|Traceback"
```

Common culprits:
- `-2015` — Binance whitelist still has the old IP. Wait the 3 min or re-save.
- `-2010` — insufficient base-asset balance (Phase 10D Convert-fee bite). Top up via Binance Convert.
- `getaddrinfo failed` — DigitalOcean network blip. Should self-heal within a minute.

If the same crash repeats >5 times in 5 min, `systemd` stops restarting (per `StartLimitBurst=5`). `sudo systemctl status grid-bot` will show "failed". Fix the root cause, then `sudo systemctl reset-failed grid-bot && sudo systemctl start grid-bot`.

### Telegram /halt doesn't trigger shutdown

Verify the listener is still alive:

```bash
sudo journalctl -u grid-bot | grep -i telegram | tail -20
```

If you see `telegram listener disabled — too many crashes`, the listener restart-governance kicked in. The sender still works, but the bot isn't receiving. Fallback halt path:

```bash
ssh bot@<RESERVED_IP> 'touch ~/grid-bot/runtime/HALT'
```

Same effect as `/halt`. The risk monitor polls the file every `risk.poll_interval_s` and trips the kill switch.

### Dashboard SSH tunnel feels unreliable

The default OpenSSH client times out idle tunnels. Add this to `~/.ssh/config` on the laptop:

```
Host grid-bot
  HostName <RESERVED_IP>
  User bot
  ServerAliveInterval 60
  ServerAliveCountMax 3
  LocalForward 8080 127.0.0.1:8080
```

Then `ssh grid-bot` opens both the shell and the tunnel.

---

## Rollback (if cutover went wrong)

In order of preference:

1. **Halt the VM bot via Telegram /halt.** Bot cancels orders + exits. systemd respawns it; if you don't want that, `sudo systemctl stop grid-bot && sudo systemctl disable grid-bot`. The laptop can take over after re-whitelisting your home IP on Binance.
2. **Destroy the VM, restore from snapshot.** Reserved IP stays. New droplet boots with the last good state of `runtime/`, `logs/`, `data/`. Bot resumes.
3. **Permanent retreat to laptop.** Update Binance whitelist back to home IP, run `python -m src.grid.grid_runner --env mainnet --live` on the laptop. Cost: ~3 min IP propagation. Not great but a working escape.

The laptop bot config is your safety net for at least the first month of VM operation. Don't delete it.
