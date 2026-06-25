"""Multi-month HK.MHI portfolio: front-month bot entries; next-month legs exit-only."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from robs.execution.contract_rollover import (
    ContractRolloverManager,
    contract_month_key,
    is_continuous_mhi,
    is_held_ahead_of_front,
    is_hk_mhi_product_code,
    is_named_mhi_contract,
    quote_symbols_for_portfolio,
    resolve_entry_order_code,
)
from robs.execution.position import UnitPositionBook
from robs.execution.position_sync import (
    BrokerPosition,
    apply_broker_position,
    refresh_broker_position,
)
from robs.strategy.mhimain import MHImainStrategy
from robs.strategy.trend import TrendMode


@dataclass(frozen=True)
class BrokerPositionChange:
    code: str
    book: str
    contracts: int


@dataclass
class MHILeg:
    """Manual next-month position (ahead of front): bot may flat, never open."""

    code: str
    position: UnitPositionBook = field(default_factory=UnitPositionBook)
    strategy: MHImainStrategy = field(default_factory=MHImainStrategy)
    rollover: ContractRolloverManager | None = None


@dataclass
class MHIPortfolio:
    """Bot manages front month (MHImain); ahead-month legs are manual open, flat-only."""

    quote_symbol: str
    cfg: dict[str, Any]
    entry_strategy: MHImainStrategy
    entry_position: UnitPositionBook = field(default_factory=UnitPositionBook)
    entry_book_code: str | None = None
    entry_rollover: ContractRolloverManager | None = None
    legs: dict[str, MHILeg] = field(default_factory=dict)
    front_contract: str | None = None
    front_last_trade_time: str | None = None
    _subscribed: list[str] = field(default_factory=list)
    _last_broker_contracts: dict[str, int] = field(default_factory=dict, repr=False)

    @classmethod
    def create(cls, cfg: dict[str, Any], *, trend: TrendMode, quote_symbol: str) -> MHIPortfolio:
        entry_rollover = None
        if is_continuous_mhi(quote_symbol):
            entry_rollover = ContractRolloverManager(quote_symbol, cfg)
        return cls(
            quote_symbol=quote_symbol,
            cfg=cfg,
            entry_strategy=MHImainStrategy.from_config(cfg, trend=trend),
            entry_rollover=entry_rollover,
        )

    def is_flat(self) -> bool:
        if self.entry_position.contracts != 0:
            return False
        return not any(leg.position.contracts != 0 for leg in self.legs.values())

    def is_front_month_code(self, code: str) -> bool:
        front = self.front_contract
        if not front or not is_named_mhi_contract(code):
            return False
        return code.upper() == front.upper()

    def is_next_month_code(self, code: str) -> bool:
        """True when code is a named month ahead of the broker front (manual leg)."""
        front = self.front_contract
        if not front or not is_named_mhi_contract(code):
            return False
        return is_held_ahead_of_front(code, front)

    def ahead_of_front_legs(self) -> list[MHILeg]:
        return [leg for leg in self.active_legs() if self.is_next_month_code(leg.code)]

    def active_legs(self) -> list[MHILeg]:
        return [leg for leg in self.legs.values() if leg.position.contracts != 0]

    def active_leg_codes(self) -> list[str]:
        return sorted(
            (leg.code for leg in self.active_legs()),
            key=lambda c: contract_month_key(c) or (0, 0),
        )

    def total_signed_contracts(self) -> int:
        return self.entry_position.contracts + sum(
            leg.position.contracts for leg in self.legs.values()
        )

    def update_front_context(
        self,
        front_contract: str | None,
        front_last_trade_time: str | None,
    ) -> None:
        self.front_contract = front_contract
        self.front_last_trade_time = front_last_trade_time

    def _roll_on_last_trade_day(self) -> bool:
        return bool(self.cfg.get("mhimain", {}).get("rollover_on_last_trade_day", True))

    def entry_watch_code(self) -> str | None:
        """Named month we'd quote for flat entries (e.g. next month on LTD roll day)."""
        if self.active_leg_codes() or self.entry_position.contracts != 0:
            return None
        front = self.front_contract
        if not front or not is_named_mhi_contract(front):
            return None
        resolved = self.resolve_entry_order_code(front)
        if (
            resolved
            and is_named_mhi_contract(resolved)
            and resolved.upper() != front.upper()
        ):
            return resolved
        return None

    def _quoted_month_codes(self) -> list[str]:
        """Named months that need their own quote (held, watch, rollover targets)."""
        codes: set[str] = set(self.active_leg_codes())
        if self.entry_position.contracts != 0:
            entry_code = self.managed_entry_code()
            if is_named_mhi_contract(entry_code):
                codes.add(entry_code)
        watch = self.entry_watch_code()
        if watch:
            codes.add(watch)
        rollover = self.entry_rollover
        if rollover is not None and rollover.active():
            st = rollover.state
            if st.target_contract:
                codes.add(st.target_contract)
            if st.held_contract:
                codes.add(st.held_contract)
        for leg in self.active_legs():
            if leg.rollover is None or not leg.rollover.active():
                continue
            st = leg.rollover.state
            if st.target_contract:
                codes.add(st.target_contract)
            if st.held_contract:
                codes.add(st.held_contract)
        front = self.front_contract
        if front and is_named_mhi_contract(front):
            codes.add(front)
        return sorted(codes, key=lambda c: contract_month_key(c) or (0, 0))

    def quote_symbols(self) -> list[str]:
        return quote_symbols_for_portfolio(self.quote_symbol, self._quoted_month_codes())

    def resolve_entry_order_code(self, front_contract: str | None) -> str:
        return resolve_entry_order_code(
            self.quote_symbol,
            front_contract or self.front_contract,
            front_last_trade_time=self.front_last_trade_time,
            roll_on_last_trade_day=self._roll_on_last_trade_day(),
        )

    def managed_entry_code(self) -> str:
        """Broker code for the bot entry book (held month, roll-day target, or front)."""
        if self.entry_position.contracts != 0 and self.entry_book_code:
            return self.entry_book_code
        return self.resolve_entry_order_code(self.front_contract)

    def _note_entry_book_code(self, code: str | None) -> None:
        if code and is_hk_mhi_product_code(code):
            self.entry_book_code = code
        elif self.entry_position.contracts == 0:
            self.entry_book_code = None

    def entry_held_code(self) -> str | None:
        """Broker month code for a non-flat entry book, if known."""
        if self.entry_position.contracts == 0:
            return None
        if self.entry_book_code and is_hk_mhi_product_code(self.entry_book_code):
            return self.entry_book_code
        if self.front_contract and is_named_mhi_contract(self.front_contract):
            return self.front_contract
        return None

    def is_roll_day_entry(self) -> bool:
        front = self.front_contract
        if not front or not is_named_mhi_contract(front):
            return False
        return self.managed_entry_code().upper() != front.upper()

    def tracks_on_entry_book(self, code: str) -> bool:
        """True if this broker row belongs on entry_position (not a manual month leg)."""
        if not is_hk_mhi_product_code(code):
            return False
        if self.entry_position.contracts != 0 and self.entry_book_code:
            return code.upper() == self.entry_book_code.upper()
        if code.upper() == self.managed_entry_code().upper():
            return True
        if self.is_front_month_code(code) and self.entry_position.contracts != 0:
            return True
        if self.is_roll_day_entry():
            return False
        return self.is_front_month_code(code)

    def entry_book_code_active(self) -> str | None:
        if self.entry_position.contracts != 0 and self.entry_book_code:
            return self.entry_book_code
        return None

    def tracks_as_next_month_leg(self, code: str) -> bool:
        """Manual next-month row: flat-only leg, never bot roll-day entry book."""
        return self.is_next_month_code(code) and not self.tracks_on_entry_book(code)

    def _drop_leg_if_any(self, code: str) -> None:
        if code in self.legs:
            del self.legs[code]

    def flat_entry_target(
        self,
        front_contract: str | None,
    ) -> tuple[UnitPositionBook, MHImainStrategy, str]:
        """Front month entries; next month on front last trading day (roll). Manual ahead legs are flat-only."""
        code = self.resolve_entry_order_code(front_contract)
        return self.entry_position, self.entry_strategy, code

    def leg_for_code(self, code: str) -> MHILeg | None:
        return self.legs.get(code)

    def ensure_leg(self, code: str) -> MHILeg:
        if not is_hk_mhi_product_code(code):
            raise ValueError(f"not an HK.MHI product: {code}")
        if code not in self.legs:
            strat = MHImainStrategy.from_config(self.cfg, trend=self.entry_strategy.trend)
            rollover = None
            if is_continuous_mhi(self.quote_symbol):
                rollover = ContractRolloverManager(self.quote_symbol, self.cfg)
            self.legs[code] = MHILeg(code=code, strategy=strat, rollover=rollover)
        return self.legs[code]

    def remove_if_flat(self, code: str) -> None:
        leg = self.legs.get(code)
        if leg is not None and leg.position.contracts == 0:
            del self.legs[code]

    def ahead_leg_for_code(self, code: str) -> MHILeg | None:
        """Non-flat manual next-month leg on ``code``, if any."""
        if not self.is_next_month_code(code):
            return None
        leg = self.legs.get(code)
        if leg is None or leg.position.contracts == 0:
            return None
        return leg

    def absorb_leg_into_entry(self, code: str) -> None:
        """Move an ahead-month leg onto the entry book without a broker open."""
        leg = self.legs.pop(code, None)
        if leg is None:
            self._note_entry_book_code(code)
            return
        self.entry_position.contracts = leg.position.contracts
        self.entry_position.default_order_qty = leg.position.default_order_qty
        if leg.strategy.entry_price is not None:
            self.entry_strategy.entry_price = leg.strategy.entry_price
        self.entry_strategy.position_opened_at = leg.strategy.position_opened_at
        self.entry_strategy.on_broker_position_opened()
        self._note_entry_book_code(code)
        self.sync_entry_armed()

    def sync_entry_armed(self) -> None:
        self.entry_strategy.sync_existing_position(not self.is_flat())

    def _sync_risk_shares(self, risk: Any) -> None:
        risk.position_shares = self.total_signed_contracts()

    def _note_broker_contracts(self, code: str, broker_contracts: int) -> None:
        self._last_broker_contracts[str(code).upper()] = broker_contracts

    def _broker_contracts_reportable(self, code: str, broker_contracts: int) -> bool:
        """True when broker-reported signed contracts changed since last refresh."""
        key = str(code).upper()
        prev = self._last_broker_contracts.get(key)
        self._note_broker_contracts(code, broker_contracts)
        return prev is None or prev != broker_contracts

    def _broker_row_missing(self, code: str, by_code: dict[str, BrokerPosition]) -> bool:
        """True when broker no longer reports an open position on this contract."""
        if not by_code:
            return True
        row = by_code.get(code)
        if row is None:
            for key, broker in by_code.items():
                if key.upper() == str(code).upper():
                    row = broker
                    break
        return row is None or row.contracts == 0

    def _resolve_entry_matched(self, by_code: dict[str, BrokerPosition]) -> str | None:
        """Broker month row for the entry book only (never a manual next-month leg)."""
        rollover = self.entry_rollover
        if rollover is not None and rollover.busy():
            st = rollover.state
            if st.phase == "open" and st.target_contract and st.target_contract in by_code:
                return st.target_contract
            if st.phase == "close" and st.held_contract and st.held_contract in by_code:
                return st.held_contract
        front = self.front_contract
        if front and front in by_code and is_named_mhi_contract(front):
            resolved = self.resolve_entry_order_code(front)
            if (
                resolved
                and resolved.upper() != front.upper()
                and (
                    self.entry_position.contracts == 0
                    or (
                        self.entry_book_code
                        and self.entry_book_code.upper() == front.upper()
                    )
                )
            ):
                return front
        for code in self._entry_broker_sync_codes():
            if code in by_code and self.tracks_on_entry_book(code):
                return code
        if len(by_code) == 1:
            only = next(iter(by_code))
            if self.tracks_on_entry_book(only):
                return only
        return None

    def bootstrap_from_broker(
        self,
        broker_legs: list[BrokerPosition],
        risk: Any,
        *,
        ma5: float | None,
        front_contract: str | None = None,
    ) -> None:
        self.entry_strategy.set_ma5(ma5)
        if front_contract:
            self.front_contract = front_contract
        by_code = {
            b.code: b
            for b in broker_legs
            if b.contracts != 0 and is_hk_mhi_product_code(b.code)
        }
        matched = self._resolve_entry_matched(by_code)
        if matched is not None:
            broker = by_code[matched]
            self._drop_leg_if_any(matched)
            apply_broker_position(
                broker, self.entry_position, self.entry_strategy, bootstrap=True
            )
            self._note_entry_book_code(matched)
            self.entry_strategy.on_broker_position_opened()
        for code, broker in by_code.items():
            if matched is not None and code.upper() == matched.upper():
                continue
            if not is_named_mhi_contract(code):
                continue
            leg = self.ensure_leg(code)
            apply_broker_position(broker, leg.position, leg.strategy, bootstrap=True)
            leg.strategy.on_broker_position_opened()
        self.sync_entry_armed()
        self._sync_risk_shares(risk)
        for code, broker in by_code.items():
            self._last_broker_contracts[code.upper()] = broker.contracts

    def _entry_broker_sync_codes(self) -> list[str]:
        """Priority order for which broker month row maps to the entry book."""
        codes: list[str] = []
        rollover = self.entry_rollover
        if rollover is not None and rollover.busy():
            st = rollover.state
            if st.phase == "open" and st.target_contract:
                codes.append(st.target_contract)
            if st.phase == "close" and st.held_contract:
                codes.append(st.held_contract)
        if self.entry_position.contracts != 0 and self.entry_book_code:
            codes.append(self.entry_book_code)
        resolved = self.resolve_entry_order_code(self.front_contract)
        if resolved:
            codes.append(resolved)
        if self.front_contract and is_named_mhi_contract(self.front_contract):
            codes.append(self.front_contract)
        seen: set[str] = set()
        out: list[str] = []
        for code in codes:
            key = code.upper()
            if key not in seen:
                seen.add(key)
                out.append(code)
        return out

    def _refresh_front_entry(
        self,
        by_code: dict[str, BrokerPosition],
        risk: Any,
    ) -> list[BrokerPositionChange]:
        changed: list[BrokerPositionChange] = []
        matched = self._resolve_entry_matched(by_code)
        if matched is not None:
            self._drop_leg_if_any(matched)
            broker = by_code[matched]
            if refresh_broker_position(
                broker,
                self.entry_position,
                self.entry_strategy,
                risk,
                cfg=self.cfg,
                update_risk=False,
            ) and self._broker_contracts_reportable(matched, broker.contracts):
                changed.append(
                    BrokerPositionChange(
                        matched, "entry", self.entry_position.contracts,
                    )
                )
            self._note_entry_book_code(
                matched if self.entry_position.contracts != 0 else None
            )
            return changed
        entry_code = self.entry_book_code or self.entry_held_code()
        if not entry_code:
            sync_codes = self._entry_broker_sync_codes()
            entry_code = sync_codes[0] if sync_codes else self.managed_entry_code()
        if not entry_code:
            return changed
        if (
            self.entry_position.contracts != 0
            and self._broker_row_missing(entry_code, by_code)
        ):
            flat = BrokerPosition(
                code=entry_code,
                contracts=0,
                qty=0,
                entry_price=None,
                current_price=None,
                pnl_points=0.0,
                pnl_val=None,
            )
            if refresh_broker_position(
                flat,
                self.entry_position,
                self.entry_strategy,
                risk,
                cfg=self.cfg,
                update_risk=False,
            ):
                self._note_broker_contracts(entry_code, 0)
                changed.append(
                    BrokerPositionChange(entry_code, "entry", 0),
                )
            self._note_entry_book_code(None)
        return changed

    def _sync_leg_flat_if_broker_gone(
        self,
        code: str,
        by_code: dict[str, BrokerPosition],
        risk: Any,
    ) -> BrokerPositionChange | None:
        leg = self.legs.get(code)
        if leg is None or leg.position.contracts == 0:
            return None
        if not self._broker_row_missing(code, by_code):
            return None
        flat = BrokerPosition(
            code=code,
            contracts=0,
            qty=0,
            entry_price=None,
            current_price=None,
            pnl_points=0.0,
            pnl_val=None,
        )
        if not refresh_broker_position(
            flat, leg.position, leg.strategy, risk, cfg=self.cfg, update_risk=False
        ):
            return None
        self._note_broker_contracts(code, 0)
        return BrokerPositionChange(code, "leg", 0)

    def refresh_from_broker(
        self,
        broker_legs: list[BrokerPosition],
        risk: Any,
    ) -> list[BrokerPositionChange]:
        """Sync entry book and manual month legs from broker positions."""
        by_code = {
            b.code: b
            for b in broker_legs
            if b.contracts != 0 and is_hk_mhi_product_code(b.code)
        }
        changed = self._refresh_front_entry(by_code, risk)
        entry_code = self.entry_book_code_active()

        for code, broker in by_code.items():
            if entry_code and code.upper() == entry_code.upper():
                continue
            if not is_named_mhi_contract(code):
                continue
            leg = self.ensure_leg(code)
            if refresh_broker_position(
                broker, leg.position, leg.strategy, risk, cfg=self.cfg, update_risk=False
            ) and self._broker_contracts_reportable(code, broker.contracts):
                changed.append(
                    BrokerPositionChange(code, "leg", leg.position.contracts),
                )

        for code in list(self.legs.keys()):
            if code in by_code:
                continue
            leg_change = self._sync_leg_flat_if_broker_gone(code, by_code, risk)
            if leg_change is not None:
                changed.append(leg_change)

        self.sync_entry_armed()
        self._sync_risk_shares(risk)
        return changed

    def detect_new_subscriptions(self, symbols: list[str]) -> list[str]:
        new = [s for s in symbols if s not in self._subscribed]
        self._subscribed = list(symbols)
        return new


def parse_quote_batch(
    data: Any,
    symbols: list[str],
) -> tuple[dict[str, float], dict[str, Any], dict[str, str]]:
    """Map symbol -> last_price, row, data_time from a quote dataframe."""
    prices: dict[str, float] = {}
    rows: dict[str, Any] = {}
    data_times: dict[str, str] = {}
    if data is None or len(data) == 0:
        return prices, rows, data_times
    codes = data["code"].astype(str) if "code" in data.columns else None
    for sym in symbols:
        row = None
        if codes is not None:
            exact = data[codes == sym]
            if len(exact):
                row = exact.iloc[0]
        if row is None and len(data) == 1 and len(symbols) == 1:
            row = data.iloc[0]
        if row is None:
            continue
        prices[sym] = float(row["last_price"])
        rows[sym] = row
        data_times[sym] = str(row.get("data_time", ""))
    return prices, rows, data_times
