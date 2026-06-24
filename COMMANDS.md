# Robs command reference

Quick cheat sheet for local Mac development and GCP VM deployment.

## Layout

| Path | What |
|------|------|
| `~/FTAPI4Python_10.4.6408/` | Futu API bundle + shared Python venv (`bin/python`) |
| `~/FTAPI4Python_10.4.6408/Robs/` | MHImain trader, configs, GCP deploy scripts |
| `Robs/config/mhimain.yaml` | Main trader config |
| `Robs/deploy/gcp/` | Mac → VM sync + VM bootstrap scripts |
| `Robs/deploy/systemd/` | `futu-opend` + `robs-mhimain` units |

---

## Mac — environment

```bash
cd ~/FTAPI4Python_10.4.6408/Robs
source ../bin/activate          # optional; scripts use ../bin/python directly
pip install -r requirements.txt # Mac: pyyaml, scipy (futu-api from parent venv)
```

OpenD must be running locally at `127.0.0.1:11111`.

```bash
# Quick OpenD connectivity check
cd ~/FTAPI4Python_10.4.6408/Robs
../bin/python main.py
```

---

## Mac — trader & dev

```bash
cd ~/FTAPI4Python_10.4.6408/Robs

# MHImain (primary)
./run_mhimain.sh
./run_mhimain.sh --trend bull
./run_mhimain.sh --trend bear
./run_mhimain.sh --prompt-trend
./run_mhimain.sh --log-format json    # or text
./run_mhimain.sh --log-level INFO
./run_mhimain.sh --trade-password '…' # REAL / non-interactive

# Other Robs CLIs (via run.sh)
./run.sh robs/cli/collect.py --config default.yaml
./run.sh robs/cli/debug_rules.py --iterations 20
./run.sh robs/cli/backtest.py --config default.yaml
./run.sh robs/cli/run_live.py --config default.yaml

# Tests (52 tests)
cd ~/FTAPI4Python_10.4.6408/Robs
../bin/python -m unittest discover -s robs -p 'test_*.py' -v
```

---

## GCP — one-time setup (from Mac)

```bash
cd ~/FTAPI4Python_10.4.6408/Robs

# 1) Set project and create HK VM (asia-east2-a)
export GCP_PROJECT=your-project-id
./deploy/gcp/provision-hk.sh
# Optional overrides: INSTANCE, ZONE, MACHINE (default e2-small)

# 2) Sync code to VM
./deploy/gcp/sync-to-vm.sh robs-trader asia-east2-a
./deploy/gcp/sync-to-vm.sh --full robs-trader asia-east2-a   # whole repo tree

# 3) SSH in
gcloud compute ssh robs-trader --zone=asia-east2-a
```

**Defaults:** instance `robs-trader`, zone `asia-east2-a` (Hong Kong).

---

## GCP VM — first-time bootstrap (on VM)

```bash
cd ~/Robs

./deploy/gcp/bootstrap-vm.sh          # apt, swap, Python venv at deploy/gcp/.venv

# Upload OpenD tarball to VM first, then:
./deploy/gcp/install-opend.sh ~/Futu_OpenD_*_Ubuntu*.tar.gz
./deploy/gcp/configure-opend-paths.sh # symlinks, /etc/futu-opend.env

sudo nano /etc/futu-opend.env       # FUTU_ACCOUNT, FUTU_PASSWORD
# REAL trading (systemd):
# FUTU_TRADE_PASSWORD=your_trade_password

./deploy/gcp/opend-first-login.sh    # interactive SMS login (use tmux)

./deploy/gcp/install-systemd.sh       # or: install-systemd.sh your_username

sudo systemctl start futu-opend
sudo systemctl start robs-mhimain
```

**Manual test before systemd:**

```bash
cd ~/Robs
./deploy/gcp/run-mhimain.sh --trend uncertain
./deploy/gcp/run-mhimain.sh --log-format text
```

---

## GCP — day-to-day (Mac)

```bash
cd ~/FTAPI4Python_10.4.6408/Robs

# Push code changes after local edits
./deploy/gcp/sync-to-vm.sh robs-trader asia-east2-a

# SSH
gcloud compute ssh robs-trader --zone=asia-east2-a

# SCP a file (example)
gcloud compute scp ./config/mhimain.yaml robs-trader:~/Robs/config/ --zone=asia-east2-a
```

---

## GCP VM — services & logs

```bash
# Status
sudo systemctl status futu-opend
sudo systemctl status robs-mhimain

# Start / stop / restart
sudo systemctl start futu-opend
sudo systemctl start robs-mhimain
sudo systemctl restart robs-mhimain
sudo systemctl stop robs-mhimain

# Follow logs
journalctl -u futu-opend -f
journalctl -u robs-mhimain -f
journalctl -u robs-mhimain --since "1 hour ago"

# After editing systemd units or env
sudo systemctl daemon-reload
sudo systemctl restart futu-opend robs-mhimain
```

**Edit trader trend / flags:** change `ExecStart` in `deploy/systemd/robs-mhimain.service`, re-run `install-systemd.sh`, then restart.

---

## Common `gcloud` commands

```bash
# Project
gcloud config set project YOUR_PROJECT
gcloud config get-value project

# VM
gcloud compute instances list
gcloud compute instances describe robs-trader --zone=asia-east2-a
gcloud compute instances stop robs-trader --zone=asia-east2-a
gcloud compute instances start robs-trader --zone=asia-east2-a

# SSH / SCP
gcloud compute ssh robs-trader --zone=asia-east2-a
gcloud compute scp LOCAL_FILE robs-trader:REMOTE_PATH --zone=asia-east2-a

# Firewall (created by provision-hk.sh)
gcloud compute firewall-rules list
gcloud compute firewall-rules describe allow-ssh-robs
```

OpenD port **11111 is not exposed** to the internet — localhost on VM only.

---

## Environment files (`/etc/futu-opend.env` on VM)

| Variable | Purpose |
|----------|---------|
| `FUTU_ACCOUNT` | Futu login ID |
| `FUTU_PASSWORD` | Futu login password (OpenD) |
| `FUTU_TRADE_PASSWORD` | Trade unlock for **REAL** (systemd / non-TTY) |
| `LISTEN_IP` | Default `127.0.0.1` |
| `LISTEN_PORT` | Default `11111` |
| `OPEND_HOME` | Auto-set by `configure-opend-paths.sh` |
| `OPEND_CFG_FILE` | Auto-set XML path |

---

## Config quick reference (`config/mhimain.yaml`)

| Key | Typical value |
|-----|----------------|
| `mhimain.trd_env` | `SIMULATE` or `REAL` |
| `mhimain.trade_unlock_valid_days` | `21` |
| `data.poll_interval_sec` | `1` |
| `risk.stale_poll_multiplier` | `30` (poll gap + data_time threshold) |
| `risk.max_daily_loss_pct` | `50` |
| `logging.format` | `json` or `text` |
| `logging.level` | `INFO` |

---

## Typical workflows

**Local dev → deploy:**

```bash
# Mac: edit, test, sync
../bin/python -m unittest discover -s robs -p 'test_*.py' -q
./deploy/gcp/sync-to-vm.sh
gcloud compute ssh robs-trader --zone=asia-east2-a --command="sudo systemctl restart robs-mhimain"
```

**OpenD session expired on VM:**

```bash
sudo systemctl stop robs-mhimain
./deploy/gcp/opend-first-login.sh   # SMS in tmux
sudo systemctl start futu-opend robs-mhimain
```

**REAL auth expired (systemd):** restart with `FUTU_TRADE_PASSWORD` set in `/etc/futu-opend.env`, or SSH interactively and re-enter password.
