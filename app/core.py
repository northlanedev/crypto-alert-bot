"""Alerts core: command parsing, SQLite storage, batched price provider with cache.

No Telegram imports: everything here is unit-tested with fakes.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Callable

import requests

# symbol -> CoinGecko id
SYMBOLS: dict[str, str] = {
    "btc": "bitcoin", "eth": "ethereum", "sol": "solana", "ton": "the-open-network",
    "bnb": "binancecoin", "xrp": "ripple", "ada": "cardano", "doge": "dogecoin",
    "trx": "tron", "avax": "avalanche-2", "link": "chainlink", "dot": "polkadot",
    "usdt": "tether", "usdc": "usd-coin",
}
MAX_ALERTS_PER_CHAT = 20


@dataclass(frozen=True)
class Alert:
    id: int
    chat_id: int
    symbol: str
    op: str  # ">" or "<"
    target: Decimal
    repeat: bool = False

    def hit(self, price: Decimal) -> bool:
        return price > self.target if self.op == ">" else price < self.target


class ParseError(ValueError):
    pass


def parse_alert(args: str | None) -> tuple[str, str, Decimal, bool]:
    """'btc > 70000' / 'eth<3000' / 'sol > 250 repeat' -> (symbol, op, target, repeat)."""
    if not args:
        raise ParseError("Формат: /alert btc > 70000")
    text = args.strip().lower().replace(">", " > ").replace("<", " < ")
    parts = text.split()
    repeat = parts[-1] in ("repeat", "всегда", "r")
    if repeat:
        parts = parts[:-1]
    if len(parts) != 3 or parts[1] not in (">", "<"):
        raise ParseError("Формат: /alert btc > 70000")
    symbol, op, raw = parts
    if symbol not in SYMBOLS:
        raise ParseError(f"Монета {symbol.upper()} не поддерживается. Список: /coins")
    try:
        target = Decimal(raw.replace(",", "").replace("_", "").replace("$", ""))
    except InvalidOperation:
        raise ParseError("Цель должна быть числом, например 70000") from None
    if target <= 0:
        raise ParseError("Цель должна быть больше нуля")
    return symbol, op, target, repeat


def fmt_price(p: Decimal) -> str:
    """Readable USD price: 97412 -> $97,412; 0.0001234 -> $0.0001234."""
    if p >= 1:
        q = p.quantize(Decimal("0.01"))
        s = f"{q:,.2f}"
        return "$" + (s[:-3] if s.endswith(".00") else s)
    return "$" + format(p.normalize(), "f")


# ------------------------------------------------------------------ storage
class AlertDB:
    def __init__(self, path: str = "alerts.db") -> None:
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,"
            " symbol TEXT NOT NULL, op TEXT NOT NULL, target TEXT NOT NULL, repeat INTEGER NOT NULL DEFAULT 0)"
        )
        self.db.commit()

    def add(self, chat_id: int, symbol: str, op: str, target: Decimal, repeat: bool = False) -> Alert:
        count = self.db.execute("SELECT COUNT(*) FROM alerts WHERE chat_id=?", (chat_id,)).fetchone()[0]
        if count >= MAX_ALERTS_PER_CHAT:
            raise ParseError(f"Максимум {MAX_ALERTS_PER_CHAT} алертов. Удалите лишние: /alerts")
        cur = self.db.execute(
            "INSERT INTO alerts(chat_id, symbol, op, target, repeat) VALUES (?,?,?,?,?)",
            (chat_id, symbol, op, str(target), int(repeat)),
        )
        self.db.commit()
        return Alert(cur.lastrowid, chat_id, symbol, op, target, repeat)

    def for_chat(self, chat_id: int) -> list[Alert]:
        return [_alert(r) for r in self.db.execute("SELECT * FROM alerts WHERE chat_id=? ORDER BY id", (chat_id,))]

    def all(self) -> list[Alert]:
        return [_alert(r) for r in self.db.execute("SELECT * FROM alerts ORDER BY id")]

    def delete(self, chat_id: int, alert_id: int) -> bool:
        cur = self.db.execute("DELETE FROM alerts WHERE id=? AND chat_id=?", (alert_id, chat_id))
        self.db.commit()
        return cur.rowcount > 0

    def clear(self, chat_id: int) -> int:
        cur = self.db.execute("DELETE FROM alerts WHERE chat_id=?", (chat_id,))
        self.db.commit()
        return cur.rowcount


def _alert(r) -> Alert:
    return Alert(r[0], r[1], r[2], r[3], Decimal(r[4]), bool(r[5]))


# ------------------------------------------------------------------ prices
class PriceProvider:
    """CoinGecko prices for many coins in ONE request, cached for `ttl` seconds."""

    URL = "https://api.coingecko.com/api/v3/simple/price"

    def __init__(self, ttl: float = 30, session: requests.Session | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl, self.session, self.clock = ttl, session or requests.Session(), clock
        self._cache: dict[str, tuple[float, Decimal]] = {}

    def prices(self, symbols: set[str]) -> dict[str, Decimal]:
        now = self.clock()
        fresh = {s: p for s, (t, p) in self._cache.items() if s in symbols and now - t < self.ttl}
        missing = sorted(s for s in symbols if s not in fresh and s in SYMBOLS)
        if missing:
            ids = ",".join(SYMBOLS[s] for s in missing)
            r = self.session.get(self.URL, params={"ids": ids, "vs_currencies": "usd"}, timeout=15)
            r.raise_for_status()
            data = r.json()
            for s in missing:
                usd = data.get(SYMBOLS[s], {}).get("usd")
                if usd is not None:
                    price = Decimal(str(usd))
                    self._cache[s] = (now, price)
                    fresh[s] = price
        return fresh


def due_alerts(alerts: list[Alert], prices: dict[str, Decimal]) -> list[tuple[Alert, Decimal]]:
    """Alerts whose condition is met at the current prices."""
    return [(a, prices[a.symbol]) for a in alerts if a.symbol in prices and a.hit(prices[a.symbol])]
