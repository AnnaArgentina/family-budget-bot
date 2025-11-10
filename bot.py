import os
import sys
import sqlite3
from datetime import datetime, timedelta
from contextlib import closing
from dotenv import load_dotenv

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes
)

# ====== ENV ======
load_dotenv()
def _get_token() -> str:
    token = os.getenv("BOT_TOKEN", "").strip().strip('"').strip("'")
    if not token:
        print(
            "❌ Не найден BOT_TOKEN. Задайте переменную окружения или строку в .env:\n"
            "BOT_TOKEN=123456:ABC-DEF...\n",
            file=sys.stderr
        )
        sys.exit(1)
    return token

BOT_TOKEN = _get_token()
BASE_CCY = os.getenv("BASE_CURRENCY", "USD").upper()
DEFAULT_INPUT_CCY = os.getenv("DEFAULT_INPUT_CURRENCY", "ARS").upper()

DB_PATH = "budget.db"

# ====== CONSTANTS ======
CATEGORIES = ["еда", "аренда", "развлечения", "прочее"]

ACCOUNTS = [
    "ARS (нал)",
    "RUB (нал)",
    "RUB (карта)",
    "USD (нал)",
    "USDT (биржа А)",
    "USDT (биржа G)",
    "BTC (биржа А)",
    "BTC (биржа G)",
    "ETH (биржа А)",
    "EUR (карта)",
]

ACCOUNT_CCY = {
    "ARS (нал)": "ARS",
    "RUB (нал)": "RUB",
    "RUB (карта)": "RUB",
    "USD (нал)": "USD",
    "USDT (биржа А)": "USDT",
    "USDT (биржа G)": "USDT",
    "BTC (биржа А)": "BTC",
    "BTC (биржа G)": "BTC",
    "ETH (биржа А)": "ETH",
    "EUR (карта)": "EUR",
}

CCY_LIST = ["ARS","RUB","USD","USDT","BTC","ETH","EUR"]

# Conversation states
EXP_CAT, EXP_AMOUNT, EXP_CCY, EXP_ACC = range(4)
INC_AMOUNT, INC_CCY, INC_ACC = range(3)
EX_FROM_ACC, EX_TO_ACC, EX_AMOUNT, EX_RATE = range(4)
REP_PERIOD, REP_CUSTOM_FROM, REP_CUSTOM_TO = range(3)
REC_ACC, REC_AMOUNT = range(2)

# ====== DB INIT ======
def init_db():
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        c = conn.cursor()
        c.execute("""
        CREATE TABLE IF NOT EXISTS transactions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            user_id INTEGER,
            username TEXT,
            type TEXT CHECK(type IN ('expense','income','exchange_in','exchange_out','reconcile')) NOT NULL,
            category TEXT,
            account TEXT NOT NULL,
            amount REAL NOT NULL,
            currency TEXT NOT NULL,
            note TEXT
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS fx_rates(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            currency TEXT NOT NULL,
            to_usd REAL NOT NULL
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """)
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('base_ccy',?)", (BASE_CCY,))
        c.execute(
            "INSERT INTO fx_rates(ts,currency,to_usd) "
            "SELECT ?,?,? WHERE NOT EXISTS (SELECT 1 FROM fx_rates WHERE currency='USD')",
            (datetime.utcnow().isoformat(), "USD", 1.0)
        )

def get_latest_rate(currency:str)->float:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("SELECT to_usd FROM fx_rates WHERE currency=? ORDER BY ts DESC LIMIT 1", (currency.upper(),))
        row = c.fetchone()
        if row:
            return row[0]
        raise ValueError(f"Нет курса для {currency}. Задайте /setrate {currency} <число> (сколько {BASE_CCY} за 1 {currency}).")

def set_rate(currency:str, to_usd:float):
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        c = conn.cursor()
        c.execute("INSERT INTO fx_rates(ts,currency,to_usd) VALUES(?,?,?)",
                  (datetime.utcnow().isoformat(), currency.upper(), float(to_usd)))

def add_txn(ts, user, ttype, category, account, amount, currency, note=None):
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        c = conn.cursor()
        c.execute("""
        INSERT INTO transactions(ts,user_id,username,type,category,account,amount,currency,note)
        VALUES(?,?,?,?,?,?,?,?,?)
        """, (
            ts,
            getattr(user, "id", None) if user else None,
            getattr(user, "username", None) if user else None,
            ttype, category, account, amount, currency.upper(), note
        ))

def sum_balances_by_account():
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        res = {acc: 0.0 for acc in ACCOUNTS}
        for acc in ACCOUNTS:
            c.execute("""
                SELECT SUM(
                    CASE
                        WHEN type IN ('income','exchange_in','reconcile') THEN amount
                        WHEN type IN ('expense','exchange_out') THEN -amount
                        ELSE 0
                    END
                )
                FROM transactions WHERE account=?
            """, (acc,))
            total = c.fetchone()[0]
            res[acc] = total or 0.0
        return res

def sum_balances_in_usd():
    acc_native = sum_balances_by_account()
    total_usd = 0.0
    details = []
    for acc, amt in acc_native.items():
        ccy = ACCOUNT_CCY[acc]
        rate = get_latest_rate(ccy)
        usd = amt * rate if ccy != "USD" else amt
        details.append((acc, amt, ccy, usd))
        total_usd += usd
    return total_usd, details

def parse_period(kind, frm=None, to=None):
    now = datetime.now()
    if kind == "Сегодня":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = now
    elif kind == "Неделя":
        start = (now - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = now
    elif kind == "Месяц" or kind == "С начала месяца":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = now
    elif kind == "Произвольный" and frm and to:
        start = datetime.fromisoformat(frm)
        end = datetime.fromisoformat(to)
    else:
        start = now - timedelta(days=30)
        end = now
    return start, end

# ====== UI HELPERS ======
def cat_keyboard():
    buttons = [[InlineKeyboardButton(c.capitalize(), callback_data=f"cat:{c}")] for c in CATEGORIES]
    return InlineKeyboardMarkup(buttons)

def ccy_keyboard(default_first=True):
    lst = CCY_LIST.copy()
    if default_first and DEFAULT_INPUT_CCY in lst:
        lst.remove(DEFAULT_INPUT_CCY)
        lst.insert(0, DEFAULT_INPUT_CCY)
    rows, row = [], []
    for i, c in enumerate(lst, start=1):
        row.append(InlineKeyboardButton(c, callback_data=f"ccy:{c}"))
        if i % 3 == 0:
            rows.append(row); row=[]
    if row: rows.append(row)
    return InlineKeyboardMarkup(rows)

def accounts_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton(acc, callback_data=f"acc:{acc}")] for acc in ACCOUNTS])

def period_keyboard():
    opts = ["Сегодня","Неделя","Месяц","С начала месяца","Произвольный"]
    return InlineKeyboardMarkup([[InlineKeyboardButton(o, callback_data=f"period:{o}")] for o in opts])

# ====== COMMANDS ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    init_db()
    text = (
        f"Привет! Я семейный бюджет-бот.\n\n"
        f"База: {BASE_CCY}. Валюта ввода по умолчанию: {DEFAULT_INPUT_CCY}.\n\n"
        f"Что умею:\n"
        f"• /expense – записать расход\n"
        f"• /income – записать доход\n"
        f"• /exchange – обмен валют (фиксируем курс)\n"
        f"• /setrate <CCY> <курс_к_{BASE_CCY}> – задать/обновить курс\n"
        f"• /balance – остатки по кошелькам и в {BASE_CCY}\n"
        f"• /report – отчёт по периодам\n"
        f"• /reconcile – сверка (ввести конечный остаток по кошельку)\n"
        f"• /help – подсказка"
    )
    if update.message:
        await update.message.reply_text(text)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text("Команды: /expense /income /exchange /setrate /balance /report /reconcile")

# ====== EXPENSE FLOW ======
async def expense_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Выберите категорию расхода:", reply_markup=cat_keyboard())
    return EXP_CAT

async def expense_pick_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    cat = query.data.split(":",1)[1]
    context.user_data["exp_cat"] = cat
    await query.edit_message_text(f"Категория: {cat}. Введите сумму (только число, {DEFAULT_INPUT_CCY} по умолчанию).")
    return EXP_AMOUNT

async def expense_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amount_str = update.message.text.replace(",", ".").strip()
    try:
        amount = float(amount_str)
    except:
        await update.message.reply_text("Не понял сумму. Введите число, например 123.45")
        return EXP_AMOUNT
    context.user_data["exp_amount"] = amount
    await update.message.reply_text(
        f"Выберите валюту (по умолчанию {DEFAULT_INPUT_CCY}) или укажите счёт:",
        reply_markup=ccy_keyboard(default_first=True)
    )
    return EXP_CCY

async def expense_pick_ccy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    ccy = query.data.split(":",1)[1]
    context.user_data["exp_ccy"] = ccy
    await query.edit_message_text(f"Валюта: {ccy}. Теперь выберите счёт/кошелёк:", reply_markup=accounts_keyboard())
    return EXP_ACC

async def expense_pick_acc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    acc = query.data.split(":",1)[1]
    data = context.user_data
    add_txn(datetime.utcnow().isoformat(), query.from_user, "expense",
            data.get("exp_cat"), acc, data.get("exp_amount"), data.get("exp_ccy", DEFAULT_INPUT_CCY))
    await query.edit_message_text(f"✅ Расход записан: {data.get('exp_amount')} {data.get('exp_ccy', DEFAULT_INPUT_CCY)} • {data.get('exp_cat')} • {acc}")
    context.user_data.clear()
    return ConversationHandler.END

# ====== INCOME FLOW ======
async def income_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введите сумму дохода (число).")
    return INC_AMOUNT

async def income_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amount_str = update.message.text.replace(",", ".").strip()
    try:
        amount = float(amount_str)
    except:
        await update.message.reply_text("Не понял сумму. Введите число, например 500")
        return INC_AMOUNT
    context.user_data["inc_amount"] = amount
    await update.message.reply_text("Выберите валюту дохода:", reply_markup=ccy_keyboard(default_first=False))
    return INC_CCY

async def income_pick_ccy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ccy = q.data.split(":",1)[1]
    context.user_data["inc_ccy"] = ccy
    await q.edit_message_text("Выберите счёт/кошелёк для зачисления:", reply_markup=accounts_keyboard())
    return INC_ACC

async def income_pick_acc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    acc = q.data.split(":",1)[1]
    data = context.user_data
    add_txn(datetime.utcnow().isoformat(), q.from_user, "income",
            None, acc, data.get("inc_amount"), data.get("inc_ccy"))
    await q.edit_message_text(f"✅ Доход записан: {data.get('inc_amount')} {data.get('inc_ccy')} • {acc}")
    context.user_data.clear()
    return ConversationHandler.END

# ====== EXCHANGE FLOW ======
async def exchange_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Выберите счёт, ОТКУДА списываем:", reply_markup=accounts_keyboard())
    return EX_FROM_ACC

async def ex_pick_from(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    context.user_data["ex_from"] = q.data.split(":",1)[1]
    await q.edit_message_text("Теперь выберите счёт, КУДА зачисляем:", reply_markup=accounts_keyboard())
    return EX_TO_ACC

async def ex_pick_to(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    context.user_data["ex_to"] = q.data.split(":",1)[1]
    await q.edit_message_text("Введите сумму исходной валюты (число).")
    return EX_AMOUNT

async def ex_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amt = float(update.message.text.replace(",", ".").strip())
    except:
        await update.message.reply_text("Нужно число, попробуйте ещё раз.")
        return EX_AMOUNT
    context.user_data["ex_amt"] = amt
    await update.message.reply_text("Введите курс сделки: сколько USD за 1 единицу исходной валюты.\nНапр.: ARS→USD: 0.0012; USD→EUR: 1.07 (но это USD за 1 исходной валюты).")
    return EX_RATE

async def ex_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        rate_to_usd = float(update.message.text.replace(",", ".").strip())
    except:
        await update.message.reply_text("Нужно число (курс к USD).")
        return EX_RATE
    data = context.user_data
    from_acc = data["ex_from"]; to_acc = data["ex_to"]; amt = data["ex_amt"]

    from_ccy = ACCOUNT_CCY[from_acc]
    to_ccy = ACCOUNT_CCY[to_acc]

    set_rate(from_ccy, rate_to_usd)
    if to_ccy != "USD":
        try:
            to_usd_to_ccy = get_latest_rate(to_ccy)
        except:
            await update.message.reply_text(f"Нет курса для {to_ccy}. Задайте /setrate {to_ccy} <курс_к_{BASE_CCY}> и повторите обмен.")
            context.user_data.clear()
            return ConversationHandler.END
    else:
        to_usd_to_ccy = 1.0

    usd_value = amt * rate_to_usd
    target_rate = to_usd_to_ccy
    target_amount = usd_value / target_rate

    add_txn(datetime.utcnow().isoformat(), update.message.from_user if update.message else None,
            "exchange_out", None, from_acc, amt, from_ccy, note=f"-> {to_acc}")
    add_txn(datetime.utcnow().isoformat(), update.message.from_user if update.message else None,
            "exchange_in", None, to_acc, target_amount, to_ccy, note=f"from {from_acc}")

    await update.message.reply_text(f"✅ Обмен: {amt} {from_ccy} → {round(target_amount,8)} {to_ccy} (курс {from_ccy}→USD={rate_to_usd}).")
    context.user_data.clear()
    return ConversationHandler.END

# ====== SET RATE ======
async def setrate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.split()
    if len(parts) != 3:
        await update.message.reply_text(f"Формат: /setrate CCY КУРС_К_{BASE_CCY}\nНапр.: /setrate ARS 0.0012")
        return
    _, ccy, rate = parts
    try:
        set_rate(ccy.upper(), float(rate))
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")
        return
    await update.message.reply_text(f"✅ Курс сохранён: 1 {ccy.upper()} = {rate} {BASE_CCY}")

# ====== BALANCE ======
async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        total_usd, details = sum_balances_in_usd()
    except Exception as e:
        await update.message.reply_text(f"Нужно задать курсы всех валют, которые есть в кошельках.\nОшибка: {e}")
        return
    lines = ["Остатки по кошелькам:"]
    for acc, amt, ccy, usd in details:
        lines.append(f"• {acc}: {round(amt,8)} {ccy}  (~{round(usd,2)} USD)")
    lines.append(f"\nИтого в USD: {round(total_usd,2)}")
    await update.message.reply_text("\n".join(lines))

# ====== REPORT ======
async def report_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Выберите период отчёта:", reply_markup=period_keyboard())
    return REP_PERIOD

async def report_period(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    kind = q.data.split(":",1)[1]
    if kind == "Произвольный":
        await q.edit_message_text("Введите даты в формате YYYY-MM-DD YYYY-MM-DD (от и до).")
        return REP_CUSTOM_FROM
    start, end = parse_period(kind)
    await q.edit_message_text(make_report_text(start, end))
    return ConversationHandler.END

async def report_custom(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        frm, to = update.message.text.strip().split()
        start, end = parse_period("Произвольный", frm + "T00:00:00", to + "T23:59:59")
    except Exception:
        await update.message.reply_text("Формат: 2025-11-01 2025-11-10")
        return REP_CUSTOM_FROM
    await update.message.reply_text(make_report_text(start, end))
    return ConversationHandler.END

def make_report_text(start: datetime, end: datetime)->str:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("""
        SELECT COALESCE(category,'прочее'), currency, SUM(amount) 
        FROM transactions 
        WHERE type='expense' AND ts BETWEEN ? AND ?
        GROUP BY category, currency
        """, (start.isoformat(), end.isoformat()))
        rows = c.fetchall()

    cat_totals_usd = {}
    for cat, ccy, amt in rows:
        try:
            rate = get_latest_rate(ccy)
        except:
            rate = 0.0
        usd = amt * (rate if ccy!="USD" else 1.0)
        cat_totals_usd[cat] = cat_totals_usd.get(cat, 0.0) + usd

    lines = [f"Отчёт {start.strftime('%Y-%m-%d')} → {end.strftime('%Y-%m-%d')}"]
    if not cat_totals_usd:
        lines.append("Нет расходов за период.")
    else:
        lines.append("Расходы по категориям (в USD):")
        for cat, val in cat_totals_usd.items():
            lines.append(f"• {cat}: {round(val,2)}")
    return "\n".join(lines)

# ====== RECONCILE ======
async def reconcile_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Выберите кошелёк для сверки:", reply_markup=accounts_keyboard())
    return REC_ACC

async def reconcile_pick_acc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    acc = q.data.split(":",1)[1]
    context.user_data["rec_acc"] = acc
    await q.edit_message_text(f"Введите конечный остаток в нативной валюте кошелька ({ACCOUNT_CCY[acc]}).")
    return REC_AMOUNT

async def reconcile_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amt = float(update.message.text.replace(",", ".").strip())
    except:
        await update.message.reply_text("Нужно число.")
        return REC_AMOUNT

    acc = context.user_data["rec_acc"]
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute("""
        SELECT COALESCE(SUM(CASE WHEN type IN ('income','exchange_in','reconcile') THEN amount 
                                 WHEN type IN ('expense','exchange_out') THEN -amount ELSE 0 END),0)
        FROM transactions WHERE account=?
        """, (acc,))
        current = c.fetchone()[0] or 0.0

    diff = amt - current
    if abs(diff) < 1e-9:
        add_txn(datetime.utcnow().isoformat(), update.message.from_user, "reconcile",
                None, acc, 0.0, ACCOUNT_CCY[acc], note="confirm ok")
        await update.message.reply_text("✅ Сальдо подтверждено, корректировка не требуется.")
    elif diff > 0:
        add_txn(datetime.utcnow().isoformat(), update.message.from_user, "reconcile",
                None, acc, diff, ACCOUNT_CCY[acc], note="reconcile up")
        await update.message.reply_text(f"✅ Сверка: добавлено {round(diff,8)} {ACCOUNT_CCY[acc]}")
    else:
        add_txn(datetime.utcnow().isoformat(), update.message.from_user, "reconcile",
                None, acc, -abs(diff), ACCOUNT_CCY[acc], note="reconcile down (as expense)")
        await update.message.reply_text(f"✅ Сверка: списано {round(abs(diff),8)} {ACCOUNT_CCY[acc]}")
    context.user_data.clear()
    return ConversationHandler.END

# ====== HANDLERS & MAIN ======
def make_app():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("setrate", setrate))
    app.add_handler(CommandHandler("balance", balance))

    exp_conv = ConversationHandler(
        entry_points=[CommandHandler("expense", expense_start)],
        states={
            EXP_CAT: [CallbackQueryHandler(expense_pick_cat, pattern=r"^cat:")],
            EXP_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, expense_amount)],
            EXP_CCY: [CallbackQueryHandler(expense_pick_ccy, pattern=r"^ccy:")],
            EXP_ACC: [CallbackQueryHandler(expense_pick_acc, pattern=r"^acc:")],
        },
        fallbacks=[],
        per_message=False,   # важное отличие — гасит предупреждения PTB
    )
    app.add_handler(exp_conv)

    inc_conv = ConversationHandler(
        entry_points=[CommandHandler("income", income_start)],
        states={
            INC_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, income_amount)],
            INC_CCY: [CallbackQueryHandler(income_pick_ccy, pattern=r"^ccy:")],
            INC_ACC: [CallbackQueryHandler(income_pick_acc, pattern=r"^acc:")],
        },
        fallbacks=[],
        per_message=False,
    )
    app.add_handler(inc_conv)

    ex_conv = ConversationHandler(
        entry_points=[CommandHandler("exchange", exchange_start)],
        states={
            EX_FROM_ACC: [CallbackQueryHandler(ex_pick_from, pattern=r"^acc:")],
            EX_TO_ACC: [CallbackQueryHandler(ex_pick_to, pattern=r"^acc:")],
            EX_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ex_amount)],
            EX_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ex_rate)],
        },
        fallbacks=[],
        per_message=False,
    )
    app.add_handler(ex_conv)

    rep_conv = ConversationHandler(
        entry_points=[CommandHandler("report", report_start)],
        states={
            REP_PERIOD: [CallbackQueryHandler(report_period, pattern=r"^period:")],
            REP_CUSTOM_FROM: [MessageHandler(filters.TEXT & ~filters.COMMAND, report_custom)],
        },
        fallbacks=[],
        per_message=False,
    )
    app.add_handler(rep_conv)

    rec_conv = ConversationHandler(
        entry_points=[CommandHandler("reconcile", reconcile_start)],
        states={
            REC_ACC: [CallbackQueryHandler(reconcile_pick_acc, pattern=r"^acc:")],
            REC_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, reconcile_amount)],
        },
        fallbacks=[],
        per_message=False,
    )
    app.add_handler(rec_conv)

    return app

from flask import Flask
flask_app = Flask(__name__)

from telegram.ext import ApplicationBuilder

async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.run_polling()

@flask_app.route("/")
def index():
    return "Bot is running!", 200

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())

