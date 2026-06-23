# Robs

Experience-rule trading stack on Futu OpenD, with optional Ising/attractor models for Phase 2 research.

## Environment

```bash
cd /Users/CK/FTAPI4Python_10.4.6408/Robs
source ../bin/activate
pip install -r requirements.txt
```

Requires OpenD at `127.0.0.1:11111`.

## Workflow

### 1. Collect data

```bash
./run.sh robs/cli/collect.py --config default.yaml
```

Stores snapshots and 1m bars under `data/`.

### 2. Debug rules (no orders)

```bash
./run.sh robs/cli/debug_rules.py --iterations 20
```

### 3. Backtest gate

```bash
./run.sh robs/cli/backtest.py --config default.yaml
```

Fails if `min_bars`, `max_drawdown_pct`, or `min_trades` are not met.

Daily profile:

```bash
./run.sh robs/cli/backtest.py --config daily.yaml
```

### 4. Paper / live loop

Paper mode is default (`strategy.paper_trading: true` in config).

```bash
./run.sh robs/cli/run_live.py --config default.yaml
```

Set `strategy.paper_trading: false` only after backtest passes and you accept live risk.

## Configuration

- [`config/default.yaml`](config/default.yaml) — intraday rules and risk limits
- [`config/daily.yaml`](config/daily.yaml) — daily bar profile (extends default)

Rules (tunable without code changes):

- **pair_spread** — rolling % spread between two tickers (from `correlation_bull_bear.py`)
- **point_breakout** — point-threshold breakout (from `monitor_100points_playsound.py`)

### 5. HK.MHImain (dedicated script)

Single-contract futures trader with strict position rules:

| Position | Allowed actions |
|----------|-----------------|
| **flat (0)** | HOLD, BUY (open long), SELL (open short) |
| **long (+1)** | HOLD, SELL (close) — **no BUY** |
| **short (-1)** | HOLD, BUY (cover) — **no SELL** |

**Entry** (when flat, not in cooldown):

| Trend | Rule |
|-------|------|
| **bull** | Open **long** |
| **bear** | Open **short** |
| **uncertain** | Price **< MA5** → long; price **> MA5** → short |

Applies at **launch** (if no position) and again after cooldown clears.

- `ma_period: 5` — daily MA for uncertain mode

| Trend | New entries | Exits |
|-------|-------------|-------|
| **bull** | Long only | Close long (profit or stop loss) |
| **bear** | Short only | Close short (profit or stop loss) |
| **uncertain** | Long or short | Either side |

```bash
chmod +x run_mhimain.sh

# default: uncertain
./run_mhimain.sh

# override trend
./run_mhimain.sh --trend bull
./run_mhimain.sh --trend bear

# interactive prompt (optional)
./run_mhimain.sh --prompt-trend
```

Config: [`config/mhimain.yaml`](config/mhimain.yaml):

- `default_trend` — `bull` | `bear` | `uncertain` (used when `--trend` omitted)
- `trd_env` — **`SIMULATE`** (paper/sim) or **`REAL`** (live); single switch for orders and position sync
- `max_daily_loss_pct` — kill switch if session equity falls this % below day start
- `max_market_slippage_pts: 3` — market order only if last price is within 3pts of bid (sell) or ask (buy)
- `position_refresh_sec: 60` — re-sync broker position and P/L every minute (launch baseline unchanged)
- `cut_loss_pts: 200` — close when P/L is **200 pts below launch** (`launch − 200`)
- `take_profit_pts: 400` — close when P/L is **400 pts above launch** (`launch + 400`)
- `reentry_move_pts: 300` — after cut loss or take profit, no new entries until price moves **300pts** from exit **or 6 trading hours** pass

```yaml
reentry_move_pts: 300
reentry_trading_hours: 6
```

**Panic pause:** if price moves **200pts within 200 seconds**, **cut loss is disabled for 30 minutes** (take profit still allowed). Re-check after wait.

```yaml
panic_move_pts: 200
panic_window_sec: 200
panic_wait_min: 30
```

**Launch baseline:** script reads broker position and sets `baseline_pnl_pts` to current P/L.

| P/L at launch | Take profit at | Cut loss at |
|---------------|----------------|-------------|
| **−1700 pts** | **−1300 pts** (−1700+400) | **−1900 pts** (−1700−200) |
| **+100 pts** | **+500 pts** | **−100 pts** |
| **0 pts** | **+400 pts** | **−200 pts** |


Optional modules in `robs/models/`:

- `spins.py` — binary spin encoding
- `ising.py` — rolling pseudo-likelihood Ising fit
- `attractor.py` — delay embedding + regime clusters

These are not wired into live execution by default; use them for research before replacing rules.

## Checklist: paper → live

1. OpenD connected (`python main.py`)
2. Collect several sessions of bars
3. Backtest passes gate
4. Paper run behaves as expected
5. Set `trd_env: REAL` deliberately
6. Confirm risk limits (`max_daily_loss_pct`, `max_position_shares`)
