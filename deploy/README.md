# Deploy artifacts

Files in this directory are copied onto the cloud VM during Phase 11C
provisioning. They do not run on the operator's laptop and are never
imported by the bot itself — they exist purely to be installed by
`scripts/provision_vm.sh` and the operator's deployment runbook.

## Files

| File | Installed at | Purpose |
|---|---|---|
| `grid-bot.service` | `/etc/systemd/system/grid-bot.service` | systemd unit for the trading process |
| `dashboard.service` | `/etc/systemd/system/dashboard.service` | systemd unit for the local HTTP dashboard |

## Common operations on the VM

```bash
# Status
sudo systemctl status grid-bot dashboard

# Live logs (journal — bot also writes structured logs to logs/grid_bot.log)
sudo journalctl -u grid-bot -f
sudo journalctl -u dashboard -f

# Start / stop / restart
sudo systemctl start grid-bot
sudo systemctl stop grid-bot
sudo systemctl restart grid-bot

# Enable on boot (one-time after install)
sudo systemctl enable grid-bot dashboard

# Layer a per-launch override (e.g. flip mainnet → testnet) without
# editing the file in this repo:
sudo systemctl edit grid-bot
# (drops you into an editor for /etc/systemd/system/grid-bot.service.d/override.conf)
```

## Why no environment-variable templating

systemd's `ExecStart=` does not expand env vars from `EnvironmentFile=` in
the way most people expect. We hardcode `--env mainnet --live` in the unit
file and let the operator either edit it or use `systemctl edit` to
override. That's simpler than maintaining a wrapper script just to expand
two arguments.

## See also

* [`docs/design/phase_11c_cloud_migration.md`](../docs/design/phase_11c_cloud_migration.md) — full design context
* `docs/runbook/cloud_deploy.md` — step-by-step migration (drafted in step 4)
* `scripts/provision_vm.sh` — idempotent VM bootstrap (drafted in step 3)
