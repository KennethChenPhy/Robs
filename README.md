# Robs

HK.MHImain futures trader on Futu OpenD (SIMULATE or REAL).

## Environment

```bash
cd /Users/CK/FTAPI4Python_10.4.6408/Robs
source ../bin/activate
pip install -r requirements.txt
```

Requires OpenD at `127.0.0.1:11111`.

```bash
../bin/python main.py   # connectivity check
```

## MHImain trader

Single-contract futures trader with trend entry, P/L exits, LTD rollover, and multi-leg support.

| Position | Allowed actions |
|----------|-----------------|
| **flat (0)** | HOLD, BUY (open long), SELL (open short) |
| **long (+1)** | HOLD, SELL (close) — no add |
| **short (−1)** | HOLD, BUY (cover) — no add |

**Entry** (when flat, armed, not in cooldown):

| Trend | Rule |
|-------|------|
| **bull** | Open long |
| **bear** | Open short |
| **uncertain** | Price &lt; MA → long; price &gt; MA → short |

```bash
chmod +x run_mhimain.sh

./run_mhimain.sh
./run_mhimain.sh --trend bull
./run_mhimain.sh --trend bear
./run_mhimain.sh --prompt-trend
```

Config: [`config/mhimain.yaml`](config/mhimain.yaml)

Key settings:

- `mhimain.trd_env` — `SIMULATE` or `REAL`
- `mhimain.contract_rollover` — LTD rollover (11:58 HKT roll, 17:15 front flip)
- `cut_loss_pts` / `take_profit_pts` — session P/L exits
- `reentry_*` — post-exit cooldown
- `risk.max_daily_loss_pct` — kill switch

See [`COMMANDS.md`](COMMANDS.md) for Mac + GCP deployment.

## Tests

```bash
../bin/python -m unittest discover -s robs -p 'test_*.py' -v
```
