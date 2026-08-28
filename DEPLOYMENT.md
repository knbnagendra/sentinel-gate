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
- [ ] Dashboard rewritten in Streamlit (currently FastAPI in
      `dashboard/app.py` -- Streamlit chosen per the hackathon's suggested
      demo platforms; self-hosted rather than Streamlit Community Cloud, so
      it can read `state/` directly and isn't subject to Community Cloud's
      idle-sleep behavior).
- [ ] Code pushed to a public GitHub repository.
- [ ] Isolated deployment directory + venv on the target host.
- [ ] systemd services for the loop and the dashboard.
- [ ] Firewall rule opened for the dashboard's port.
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

## Deployment steps

1. SSH into the host.
2. Create an isolated directory, e.g. `~/sentinel-gate`, separate from
   anything else on the host.
3. Clone the public GitHub repo into it (once pushed) and create a
   dedicated venv:
   ```
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt streamlit
   ```
4. Copy `.env` (the real hackathon Alpaca keys + Anthropic key) to the host
   out-of-band (`scp`, not committed to git) and lock it down:
   ```
   chmod 600 ~/sentinel-gate/.env
   ```
5. Create two systemd services:
   - `sentinel-gate-loop.service` -- `ExecStart=.../.venv/bin/python -m agent.loop`, `Restart=on-failure`
   - `sentinel-gate-dashboard.service` -- `ExecStart=.../.venv/bin/streamlit run dashboard/app.py --server.port 8501 --server.address 0.0.0.0`
   Both with `WorkingDirectory=~/sentinel-gate` and an `EnvironmentFile`
   pointing at `.env`.
6. Open a firewall rule allowing inbound TCP on the dashboard's port
   (8501) -- the host currently only allows SSH in.
7. `systemctl enable --now` both services.
8. Verify:
   - `journalctl -u sentinel-gate-loop -f` shows cycles starting/completing
     on schedule during market hours.
   - `http://<HOST_EXTERNAL_IP>:8501` loads the dashboard from a machine
     outside the host's network.

## Operational notes

- **The external IP is ephemeral**, not static -- stable while the VM keeps
  running, but can change on restart. Don't restart the VM once live;
  re-confirm the IP immediately before submitting the Application URL.
  Reserving a static IP removes this risk entirely at a small ongoing cost,
  if preferred.
- **Resource sharing**: this is a small (`e2-micro`-class) shared-vCPU host
  already running another service. Sentinel Gate's loop and dashboard are
  lightweight, but monitor memory if both services are doing meaningful work
  at the same time.
- **Rollback**: `systemctl stop sentinel-gate-loop sentinel-gate-dashboard`
  stops both without touching anything else on the host.
- **Testing vs. the real run**: verify the full cycle (MCP connection,
  Claude reasoning, gated execution) against `.env.test` credentials for a
  separate throwaway paper account first. Only point the deployed services
  at the real hackathon `.env` once that's confirmed working, so the
  judged account's history starts clean from the actual competition.
