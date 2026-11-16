from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.core import MAX_ALERTS_PER_CHAT, AlertDB, ParseError, PriceProvider, due_alerts, fmt_price, parse_alert


@pytest.mark.parametrize(
    "args, expected",
    [
        ("btc > 70000", ("btc", ">", Decimal("70000"), False)),
        ("ETH<3000", ("eth", "<", Decimal("3000"), False)),
        ("sol > 250.5 repeat", ("sol", ">", Decimal("250.5"), True)),
        ("ton > $7", ("ton", ">", Decimal("7"), False)),
        ("btc > 100,000", ("btc", ">", Decimal("100000"), False)),
    ],
)
def test_parse_alert_ok(args, expected):
    assert parse_alert(args) == expected


@pytest.mark.parametrize("args", [None, "", "btc 70000", "btc >", "shib > 1", "btc > abc", "btc > -5", "btc = 5"])
def test_parse_alert_errors(args):
    with pytest.raises(ParseError):
        parse_alert(args)


@pytest.mark.parametrize(
    "price, text",
    [(Decimal("97412"), "$97,412"), (Decimal("3150.456"), "$3,150.46"), (Decimal("0.0001234"), "$0.0001234")],
)
def test_fmt_price(price, text):
    assert fmt_price(price) == text


def test_db_crud_and_limits():
    db = AlertDB(":memory:")
    a = db.add(1, "btc", ">", Decimal("70000"))
    db.add(2, "eth", "<", Decimal("3000"), repeat=True)
    assert [x.id for x in db.for_chat(1)] == [a.id]
    assert db.for_chat(2)[0].repeat is True
    assert db.delete(2, a.id) is False  # someone else's alert
    assert db.delete(1, a.id) is True
    for _ in range(MAX_ALERTS_PER_CHAT):
        db.add(3, "sol", ">", Decimal("1"))
    with pytest.raises(ParseError):
        db.add(3, "sol", ">", Decimal("1"))
    assert db.clear(3) == MAX_ALERTS_PER_CHAT


def test_due_alerts():
    db = AlertDB(":memory:")
    up = db.add(1, "btc", ">", Decimal("70000"))
    down = db.add(1, "eth", "<", Decimal("3000"))
    db.add(1, "sol", ">", Decimal("500"))
    due = due_alerts(db.all(), {"btc": Decimal("71000"), "eth": Decimal("3100"), "sol": Decimal("250")})
    assert [(a.id, p) for a, p in due] == [(up.id, Decimal("71000"))]
    assert down.hit(Decimal("2999.99"))


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


def test_price_provider_batches_and_caches():
    session = MagicMock()
    session.get.return_value.json.return_value = {"bitcoin": {"usd": 97412}, "solana": {"usd": 251.8}}
    clock = Clock()
    p = PriceProvider(ttl=30, session=session, clock=clock)

    assert p.prices({"btc", "sol"}) == {"btc": Decimal("97412"), "sol": Decimal("251.8")}
    assert session.get.call_count == 1  # one request for all coins
    assert session.get.call_args.kwargs["params"]["ids"] == "bitcoin,solana"

    p.prices({"btc"})
    assert session.get.call_count == 1  # served from cache
    clock.t = 31
    p.prices({"btc"})
    assert session.get.call_count == 2  # cache expired
