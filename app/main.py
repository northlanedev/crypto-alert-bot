"""Telegram layer: commands + background loop that checks prices and fires alerts."""
import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from .core import SYMBOLS, AlertDB, ParseError, PriceProvider, due_alerts, fmt_price, parse_alert

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

log = logging.getLogger("crypto-bot")
router = Router()

HELP = (
    "<b>Крипто-алерты</b> 📈\n\n"
    "/price btc — цена (можно несколько: <code>/price btc eth sol</code>)\n"
    "/alert btc &gt; 70000 — сообщу, когда BTC станет дороже 70 000 $\n"
    "/alert eth &lt; 3000 repeat — повторяющийся алерт\n"
    "/alerts — мои алерты · /del 3 — удалить · /clear — удалить все\n"
    "/coins — список монет"
)


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP)


@router.message(Command("coins"))
async def cmd_coins(message: Message) -> None:
    await message.answer("Поддерживаются: " + ", ".join(s.upper() for s in SYMBOLS))


@router.message(Command("price"))
async def cmd_price(message: Message, command: CommandObject, prices: PriceProvider) -> None:
    wanted = {s for s in (command.args or "btc").lower().split() if s in SYMBOLS}
    if not wanted:
        await message.answer("Не знаю таких монет. Список: /coins")
        return
    try:
        got = await asyncio.to_thread(prices.prices, wanted)
    except Exception as e:  # network / API errors
        log.warning("price error: %s", e)
        await message.answer("Сервис цен сейчас недоступен, попробуйте через минуту.")
        return
    await message.answer("\n".join(f"<b>{s.upper()}</b>: {fmt_price(got[s])}" for s in sorted(got)))


@router.message(Command("alert"))
async def cmd_alert(message: Message, command: CommandObject, db: AlertDB) -> None:
    try:
        symbol, op, target, repeat = parse_alert(command.args)
        alert = db.add(message.chat.id, symbol, op, target, repeat)
    except ParseError as e:
        await message.answer(str(e))
        return
    kind = " (повторяющийся)" if repeat else ""
    await message.answer(f"✅ Алерт #{alert.id}{kind}: {symbol.upper()} {op} {fmt_price(target)}")


@router.message(Command("alerts"))
async def cmd_alerts(message: Message, db: AlertDB) -> None:
    alerts = db.for_chat(message.chat.id)
    if not alerts:
        await message.answer("Алертов нет. Добавить: <code>/alert btc &gt; 70000</code>")
        return
    lines = [f"#{a.id} — {a.symbol.upper()} {a.op} {fmt_price(a.target)}{' 🔁' if a.repeat else ''}" for a in alerts]
    await message.answer("Ваши алерты:\n" + "\n".join(lines).replace(">", "&gt;").replace("<", "&lt;"))


@router.message(Command("del"))
async def cmd_del(message: Message, command: CommandObject, db: AlertDB) -> None:
    raw = (command.args or "").strip().lstrip("#")
    if not raw.isdigit():
        await message.answer("Формат: /del 3")
        return
    await message.answer("🗑 Удалено" if db.delete(message.chat.id, int(raw)) else "Нет такого алерта")


@router.message(Command("clear"))
async def cmd_clear(message: Message, db: AlertDB) -> None:
    await message.answer(f"Удалено алертов: {db.clear(message.chat.id)}")


async def alert_loop(bot: Bot, db: AlertDB, prices: PriceProvider, interval: int) -> None:
    while True:
        try:
            alerts = db.all()
            if alerts:
                current = await asyncio.to_thread(prices.prices, {a.symbol for a in alerts})
                for alert, price in due_alerts(alerts, current):
                    arrow = "📈" if alert.op == ">" else "📉"
                    try:
                        await bot.send_message(
                            alert.chat_id,
                            f"{arrow} <b>{alert.symbol.upper()}</b> {'выше' if alert.op == '>' else 'ниже'} "
                            f"{fmt_price(alert.target)} — сейчас {fmt_price(price)}",
                        )
                    except TelegramForbiddenError:
                        db.clear(alert.chat_id)  # user blocked the bot
                        continue
                    except TelegramAPIError as e:
                        log.warning("send failed: %s", e)
                        continue
                    if not alert.repeat:
                        db.delete(alert.chat_id, alert.id)
        except Exception as e:
            log.error("alert loop: %s", e)
        await asyncio.sleep(interval)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.getenv("BOT_TOKEN") or exit("BOT_TOKEN is not set (see .env.example)")
    interval = int(os.getenv("POLL_SECONDS", "60"))
    db, prices = AlertDB(os.getenv("DB_PATH", "alerts.db")), PriceProvider(ttl=max(interval - 5, 10))
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(db=db, prices=prices)
    dp.include_router(router)
    asyncio.create_task(alert_loop(bot, db, prices, interval))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
