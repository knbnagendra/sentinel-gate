# Deployment

Sentinel Gate needs two things running continuously through the competition
window (2026-08-28 11:00 AM ET -- 2026-09-04 11:00 AM ET):

1. **The trading loop** (`python -m agent.loop`) -- must stay alive through
   every market-hours cycle. A missed cycle is a missed (or unrecorded)
   trading opportunity, and P&L is the primary judging criterion.
2. **A public dashboard** -- the "Application URL" the hackathon submission
   requires, so judges can see the agent's decisions and P&L without SSH
   access.

Both run on the same host so the dashboard can read `state/` directly --
no state syncing between machines.

## Status

- [x] Alpaca paper account created dedicated to this hackathon, verified
      fresh: `$100,000` equity, 0 open positions.
- [x] Anthropic API key verified with a live request.
- [x] Alpaca MCP server verified over stdio: 47 tools exposed, no
      trading/order-placement tools present (`ALPACA_TOOLSETS` correctly
      excludes the `trading` category).
- [x] `propose_trade` fixed to use `@beta_async_tool` (the sync `@beta_tool`
      wasn't compatible with `AsyncAnthropic`'s tool runner -- see git log).
- [x] `LICENSE` (MIT) added.
- [x] `.gitignore` covers `.env`, `.env.test`, and `state/`.
- [x] Dashboard rewritten in Streamlit (self-hosted, not Streamlit Community
      Cloud, so it reads `state/` directly and isn't subject to Community
      Cloud's idle-sleep behavior).
- [x] Code pushed to a public GitHub repository:
      https://github.com/knbnagendra/sentinel-gate
- [x] Isolated deployment directory + venv on the target host
      (`~/sentinel-gate`, separate from the host's other project).
- [x] systemd services for the loop and the dashboard, live.
- [x] Firewall rules opened for the dashboard's port (both GCP's cloud
      firewall *and* the host's own `ufw` -- both had to be opened
      separately, see Operational notes).
- [x] Dashboard verified reachable externally.
- [ ] End-to-end cycle verified against a throwaway test account
      (`.env.test`) rather than the hackathon account, so the hackathon
      account's history stays clean until the real run.

## Target host

A small always-on VM, chosen over free-tier PaaS options (Streamlit
Community Cloud / Replit free tier / Vercel) because those sleep idle
processes -- unacceptable for a scheduler that must run unattended across
market hours for a week. The host already runs an unrelated service; this
deployment is isolated from it (separate directory, separate venv, separate
systemd units, separate `.env`) to avoid any credential or state overlap.

Access is via SSH using an existing deploy key. **Gotcha:** if the VM uses
OS Login, a key added only through the cloud console's instance metadata
won't work for plain `ssh -i` -- it must be registered via the OS Login
SSH-keys command, and the login username OS Login assigns may not match your
local username. Confirm both before assuming a plain `ssh -i key user@ip`
will work.

## Deployment steps (as actually run)

1. SSH into the host: `ssh -i ~/.ssh/jd-deploy/gcp_jd_relay knbnagendra_gmail_com@<HOST_EXTERNAL_IP>`
2. Clone into an isolated directory, separate from anything else on the host:
   ```
   git clone https://github.com/knbnagendra/sentinel-gate.git ~/sentinel-gate
   cd ~/sentinel-gate
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
3. Copy **both** env files to the host out-of-band (`scp`, never committed
   to git), lock them down, and point the active `.env` at the throwaway
   test account first -- **not** the hackathon account:
   ```
   scp .env.test <host>:~/sentinel-gate/.env.test   # throwaway sandbox account
   scp .env      <host>:~/sentinel-gate/.env.hackathon  # real hackathon account, staged but inactive
   ssh <host> "cd ~/sentinel-gate && chmod 600 .env.test .env.hackathon && ln -sf .env.test .env"
   ```
   The active `.env` is a **symlink**, not a copy -- swapping accounts later
   is just repointing the symlink + restarting the services, not re-copying
   files.
4. Two systemd services, `WorkingDirectory=/home/<user>/sentinel-gate`,
   `Restart=on-failure`, `RestartSec=10`, **and `Environment=PYTHONUNBUFFERED=1`**:
   - `sentinel-gate-loop.service` -- `ExecStart=.../.venv/bin/python -m agent.loop`
   - `sentinel-gate-dashboard.service` -- `ExecStart=.../.venv/bin/streamlit run dashboard/app.py --server.port 8501 --server.address 0.0.0.0 --server.headless true`

   Neither uses `EnvironmentFile=` -- `load_dotenv()` inside `loop.py` /
   `dashboard/app.py` reads the `.env` symlink directly at process start,
   which is what makes the symlink-swap approach work without editing the
   unit files.

   **Gotcha, caught live:** without `PYTHONUNBUFFERED=1`, Python
   block-buffers stdout when it isn't attached to a TTY (exactly the case
   under systemd) -- `print()` output can sit in the buffer indefinitely
   instead of reaching `journalctl` in real time, giving zero live
   visibility into what the loop is doing until something crashes hard
   enough to force a flush. Confirmed via `journalctl -u sentinel-gate-loop`
   showing nothing but service start/stop lines -- not even the
   every-15-second "outside market hours, skipping" print -- until this was
   added.
5. Open the dashboard's port on **both** firewalls -- this host has two
   independent layers and both blocked it by default:
   ```
   gcloud compute firewall-rules create sentinel-gate-dashboard \
     --network=default --direction=INGRESS --action=ALLOW \
     --rules=tcp:8501 --source-ranges=0.0.0.0/0
   sudo ufw allow 8501/tcp comment 'Sentinel Gate dashboard'
   ```
   (`gcloud` was already authenticated on the host itself via its instance
   service account -- no separate credential setup needed.)
6. `sudo systemctl enable --now sentinel-gate-loop sentinel-gate-dashboard`
7. Verify: `sudo systemctl status <unit>` shows `active (running)`,
   `curl http://localhost:8501` returns 200 on the host, and
   `http://<HOST_EXTERNAL_IP>:8501` loads from an outside machine.

## Scheduled account swap (test -> hackathon)

To test safely against the sandbox account first and switch to the real
hackathon account exactly at kickoff without a human needing to be present:

```
cat > ~/sentinel-gate/swap_to_hackathon.sh <<'EOF'
#!/bin/bash
set -e
cd /home/<user>/sentinel-gate
ln -sf .env.hackathon .env
sudo systemctl restart sentinel-gate-loop sentinel-gate-dashboard
echo "$(date -u): swapped to hackathon .env" >> swap.log
EOF
chmod +x ~/sentinel-gate/swap_to_hackathon.sh

sudo systemd-run --on-calendar='2026-08-28 15:00:00 UTC' \
  --unit=sentinel-gate-account-swap \
  /home/<user>/sentinel-gate/swap_to_hackathon.sh
```

`15:00:00 UTC` = 11:00 AM **EDT** (America/New_York is UTC-4 in August, not
UTC-5 -- verified with `zoneinfo`, not assumed). Check with
`systemctl list-timers | grep sentinel-gate`.

**Caveat**: this is a *transient* systemd timer -- it does not survive a
host reboot. The VM had 55+ days of uptime at deploy time, so the risk is
low, but if the swap doesn't fire, run `swap_to_hackathon.sh` manually and
verify `readlink ~/sentinel-gate/.env` points at `.env.hackathon` before
the competition needs real trading activity.

## Operational notes

- **The external IP is ephemeral**, not static -- stable while the VM keeps
  running, but can change on restart. Don't restart the VM once live;
  re-confirm the IP immediately before submitting the Application URL.
  Reserving a static IP removes this risk entirely at a small ongoing cost,
  if preferred.
- **Resource sharing**: this is a small (`e2-micro`-class, ~1GB RAM)
  shared-vCPU host already running other projects. At deploy time, ~536MB
  was available and the loop + dashboard together use well under 50MB, so
  headroom is fine -- but this is not a dedicated box, monitor `free -h` if
  anything looks slow.
- **Rollback** (mid-competition, without touching anything else on the host):
  `sudo systemctl stop sentinel-gate-loop sentinel-gate-dashboard`
- **Testing vs. the real run**: the full cycle (MCP connection, Claude
  reasoning, gated execution, `close_position`) should be verified against
  `.env.test` before the scheduled swap fires, so the judged account's
  history starts clean from the actual competition rather than from
  whatever happened during testing.

## Post-competition cleanup (after 2026-09-04 submission)

This is **shared infrastructure** running other unrelated projects --
cleanup must remove everything Sentinel Gate added without touching
anything else on the host.

```
# Stop and remove the services
sudo systemctl disable --now sentinel-gate-loop sentinel-gate-dashboard
sudo rm /etc/systemd/system/sentinel-gate-loop.service \
        /etc/systemd/system/sentinel-gate-dashboard.service
sudo systemctl daemon-reload

# Remove the account-swap timer if it's still around for any reason
sudo systemctl stop sentinel-gate-account-swap.timer 2>/dev/null || true

# Close both firewalls back up
gcloud compute firewall-rules delete sentinel-gate-dashboard --quiet
sudo ufw delete allow 8501/tcp

# Remove the code, venv, and both .env files (real Alpaca + Anthropic
# credentials live in .env.hackathon -- delete it, don't leave it on a
# shared host after the competition ends)
rm -rf ~/sentinel-gate
```

Also worth doing once the competition is over and results are recorded:
revoke or rotate the Anthropic API key and Alpaca hackathon-account keys
used here, since they were staged in plaintext on a shared host (locked to
`chmod 600`, but still worth rotating out of caution once no longer needed).
