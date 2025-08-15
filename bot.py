import asyncio
import os
import re
import csv
from dataclasses import dataclass
from typing import List, Optional, Tuple

import aiosqlite
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup,
    KeyboardButton, Update, LabeledPrice, InputFile
)
from telegram.constants import ParseMode, ChatType
from telegram.ext import (
    ApplicationBuilder, Application, CommandHandler, ContextTypes,
    MessageHandler, CallbackQueryHandler, ConversationHandler, filters,
    PreCheckoutQueryHandler, ChannelPostHandler
)

# ================== ENV / CONST ==================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
PAYMENT_PROVIDER_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN")  # optional
MANAGER_USERNAME = os.getenv("MANAGER_USERNAME", "Granku56")   # можно переопределить в .env
BOT_USERNAME = os.getenv("BOT_USERNAME", "")  # без @, для deep-link кнопки

DB_PATH = "store.db"

BTN_CART = "🛒 Корзина"
BTN_WARDROBE = "👗 Гардероб"
BTN_ORDERS = "🧾 Мои покупки"
BTN_FEEDBACK = "✉️ Обратная связь"
BTN_BACK_TO_CATS = "⬅️ Назад в категории"

ASK_FEEDBACK = 1  # состояние диалога обратной связи
IMPORT_WAIT_FILE = 1001  # состояние для импорта CSV

# ========= HELPERS =========
def price_fmt(p: int) -> str:
    rub, kop = p // 100, p % 100
    return f"{rub:,}.{kop:02d} ₽".replace(",", " ")

def parse_price(text: str) -> Optional[int]:
    """
    Возвращает цену в копейках (int) или None.
    Ищет варианты: "4 990 ₽", "4990 руб", "Цена: 5.990", "5 990р"
    """
    if not text:
        return None
    t = text.replace("\u00a0", " ")  # неразрывные пробелы
    patterns = [
        r"(?:цена[:\s]*)?(\d[\d\s\.]{1,12})\s?(?:₽|руб|руб\.|р)\b",
        r"\b(\d[\d\s\.]{1,12})\s?(?:₽|руб|руб\.|р)\b",
    ]
    for pat in patterns:
        m = re.search(pat, t, flags=re.IGNORECASE)
        if m:
            num = re.sub(r"[^\d]", "", m.group(1))
            if num.isdigit():
                return int(num) * 100
    # чистое число на строке
    for line in t.splitlines():
        if re.fullmatch(r"\s*\d[\d\s\.]{1,12}\s*", line.strip()):
            num = re.sub(r"[^\d]", "", line)
            if num.isdigit():
                return int(num) * 100
    return None

def parse_sizes(text: str) -> Optional[str]:
    """
    Ищем "Размеры: S, M, L" / "sizes: 42/44/46" и т.п.
    Возвращаем исходную строку размеров (напр. "S, M, L")
    """
    if not text:
        return None
    t = text.replace("\u00a0", " ")
    m = re.search(r"(?:размеры?|sizes?)\s*[:\-–]\s*([A-Za-zА-Яа-я0-9 ,\/\-]+)", t, flags=re.IGNORECASE)
    if m:
        sizes = m.group(1).strip()
        sizes = re.sub(r"\s+", " ", sizes)
        return sizes[:120]
    # строка только с размерами
    for line in t.splitlines():
        if re.fullmatch(r"\s*(?:[A-Za-zА-Яа-я0-9]{1,3}[\s,\/\-]+){1,10}[A-Za-zА-Яа-я0-9]{1,3}\s*", line.strip()):
            return line.strip()[:120]
    return None

def parse_hashtags(text: str) -> List[str]:
    if not text:
        return []
    tags = re.findall(r"#([\w\d_]+)", text, flags=re.UNICODE)
    return [t.strip() for t in tags if t.strip()]

def first_line(text: str) -> str:
    if not text:
        return "Товар"
    return text.strip().splitlines()[0][:128]

@dataclass
class Product:
    id: int
    title: str
    price: int
    photo_file_id: Optional[str]
    descr: Optional[str]
    category: str
    brand: str
    sizes: Optional[str]
    source_chat_id: Optional[int]
    source_msg_id: Optional[int]

# ========= DB INIT =========
INIT_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS products(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    price INTEGER NOT NULL,
    photo_file_id TEXT,
    descr TEXT,
    category TEXT DEFAULT 'Общее',
    brand TEXT DEFAULT 'NoBrand',
    sizes TEXT,
    source_chat_id INTEGER,
    source_msg_id INTEGER
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_source_msg ON products(source_chat_id, source_msg_id);

CREATE TABLE IF NOT EXISTS cart(
    user_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    qty INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(user_id, product_id)
);

CREATE TABLE IF NOT EXISTS wardrobe(
    user_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    PRIMARY KEY(user_id, product_id)
);

CREATE TABLE IF NOT EXISTS orders(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    total_price INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'NEW',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS order_items(
    order_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    qty INTEGER NOT NULL,
    price INTEGER NOT NULL
);
"""

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(INIT_SQL)
        await db.commit()

# ========= DB QUERIES =========
async def add_product(p: Product) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT OR IGNORE INTO products
            (title, price, photo_file_id, descr, category, brand, sizes, source_chat_id, source_msg_id)
            VALUES(?,?,?,?,?,?,?,?,?)
        """, (p.title, p.price, p.photo_file_id, p.descr, p.category, p.brand, p.sizes, p.source_chat_id, p.source_msg_id))
        await db.commit()
        if cur.lastrowid:
            return cur.lastrowid
        # Если IGNORE сработал (дубликат source), достанем id
        cur2 = await db.execute("""
            SELECT id FROM products WHERE source_chat_id IS ? AND source_msg_id IS ?
        """, (p.source_chat_id, p.source_msg_id))
        row = await cur2.fetchone()
        return row[0] if row else 0

async def get_categories() -> List[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT DISTINCT category FROM products ORDER BY category")
        rows = await cur.fetchall()
    return [r[0] for r in rows if r[0]]

async def get_brands_by_category(category: str) -> List[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT DISTINCT brand FROM products WHERE category=? ORDER BY brand", (category,))
        rows = await cur.fetchall()
    return [r[0] for r in rows if r[0]]

async def list_products(category: Optional[str]=None, brand: Optional[str]=None,
                        price_from: Optional[int]=None, price_to: Optional[int]=None,
                        size_query: Optional[str]=None,
                        offset: int=0, limit: int=6) -> List[Product]:
    q = "SELECT id,title,price,photo_file_id,descr,category,brand,sizes,source_chat_id,source_msg_id FROM products"
    params: Tuple = ()
    where = []
    if category:
        where.append("category=?")
        params += (category,)
    if brand:
        where.append("brand=?")
        params += (brand,)
    if price_from is not None:
        where.append("price>=?")
        params += (price_from,)
    if price_to is not None:
        where.append("price<=?")
        params += (price_to,)
    if size_query:
        where.append("sizes LIKE ?")
        params += (f"%{size_query}%",)
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params += (limit, offset)

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(q, params)
        rows = await cur.fetchall()
    return [Product(*r) for r in rows]

async def get_product_by_id(pid: int) -> Optional[Product]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id,title,price,photo_file_id,descr,category,brand,sizes,source_chat_id,source_msg_id
            FROM products WHERE id=?
        """, (pid,))
        row = await cur.fetchone()
    return Product(*row) if row else None

async def get_cart(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT p.id, p.title, p.price, c.qty
            FROM cart c JOIN products p ON p.id=c.product_id
            WHERE c.user_id=?
        """, (user_id,))
        return await cur.fetchall()

async def add_to_cart(user_id: int, product_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO cart(user_id, product_id, qty)
            VALUES(?,?,1)
            ON CONFLICT(user_id, product_id) DO UPDATE SET qty=qty+1
        """, (user_id, product_id))
        await db.commit()

async def clear_cart(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM cart WHERE user_id=?", (user_id,))
        await db.commit()

async def add_to_wardrobe(user_id: int, product_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO wardrobe(user_id, product_id) VALUES(?,?)", (user_id, product_id))
        await db.commit()

async def get_wardrobe(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT p.id, p.title, p.price
            FROM wardrobe w JOIN products p ON p.id=w.product_id
            WHERE w.user_id=?
        """, (user_id,))
        return await cur.fetchall()

async def create_order(user_id: int):
    items = await get_cart(user_id)
    if not items:
        return None
    total = sum(price * qty for _, _, price, qty in items)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO orders(user_id, total_price, status) VALUES(?,?, 'NEW')",
            (user_id, total)
        )
        order_id = cur.lastrowid
        await db.executemany(
            "INSERT INTO order_items(order_id, product_id, qty, price) VALUES(?,?,?,?)",
            [(order_id, pid, qty, price) for (pid, _, price, qty) in items]
        )
        await db.commit()
    await clear_cart(user_id)
    return order_id, total

async def list_orders(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id, total_price, status, created_at
            FROM orders WHERE user_id=? ORDER BY id DESC
        """, (user_id,))
        return await cur.fetchall()

# ========= KEYBOARDS =========
def build_categories_kb(categories: List[str]) -> ReplyKeyboardMarkup:
    if not categories:
        categories = ["🧥 Куртки", "👕 Одежда", "👖 Джинсы", "👟 Кроссовки"]
    rows = []
    for i in range(0, len(categories), 2):
        row = [KeyboardButton(categories[i])]
        if i + 1 < len(categories):
            row.append(KeyboardButton(categories[i+1]))
        rows.append(row)
    rows += [
        [KeyboardButton(BTN_CART), KeyboardButton(BTN_WARDROBE)],
        [KeyboardButton(BTN_ORDERS), KeyboardButton(BTN_FEEDBACK)]
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def build_brands_kb(brands: List[str]) -> ReplyKeyboardMarkup:
    if not brands:
        brands = ["NoBrand"]
    rows = []
    for i in range(0, len(brands), 2):
        row = [KeyboardButton(brands[i])]
        if i + 1 < len(brands):
            row.append(KeyboardButton(brands[i+1]))
        rows.append(row)
    rows.append([KeyboardButton(BTN_BACK_TO_CATS)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def product_inline_kb(pid: int) -> InlineKeyboardMarkup:
    dl = f"https://t.me/{BOT_USERNAME}?start=prd_{pid}" if BOT_USERNAME else None
    rows = [
        [
            InlineKeyboardButton("🛒 Купить", callback_data=f"buy:{pid}"),
            InlineKeyboardButton("👗 В гардероб", callback_data=f"wardrobe:{pid}")
        ],
        [
            InlineKeyboardButton("👤 Написать менеджеру", url=f"https://t.me/{MANAGER_USERNAME}")
        ],
    ]
    if dl:
        rows.append([InlineKeyboardButton("🔗 Открыть в боте", url=dl)])
    return InlineKeyboardMarkup(rows)

# ========= VIEWS =========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # deep-link: /start prd_123
    args = context.args
    if args and len(args) >= 1 and args[0].startswith("prd_"):
        try:
            pid = int(args[0].split("_")[1])
            pr = await get_product_by_id(pid)
            if pr:
                caption = f"*{pr.title}*\n{price_fmt(pr.price)}\n{(pr.descr or '')[:800]}"
                if pr.photo_file_id:
                    await update.message.reply_photo(pr.photo_file_id, caption=caption, parse_mode=ParseMode.MARKDOWN, reply_markup=product_inline_kb(pr.id))
                else:
                    await update.message.reply_text(caption, parse_mode=ParseMode.MARKDOWN, reply_markup=product_inline_kb(pr.id))
        except Exception:
            pass

    cats = await get_categories()
    context.user_data.clear()
    if update.message:
        await update.message.reply_text(
            "Добро пожаловать 👋\nПерешлите мне пост из канала — я добавлю товар в каталог.\nИли выберите категорию:",
            reply_markup=build_categories_kb(cats)
        )
    elif update.callback_query:
        await update.callback_query.message.reply_text(
            "Выберите категорию:",
            reply_markup=build_categories_kb(cats)
        )
    if not ADMIN_CHAT_ID and update.message:
        await update.message.reply_text("ℹ️ Укажи ADMIN_CHAT_ID в .env, чтобы получать обратную связь.")

async def show_products_by_brand(update: Update, context: ContextTypes.DEFAULT_TYPE, category: str, brand: str,
                                 offset=0, price_from=None, price_to=None, size_query=None):
    items = await list_products(category=category, brand=brand, offset=offset, limit=6,
                                price_from=price_from, price_to=price_to, size_query=size_query)
    if not items and offset == 0:
        await update.message.reply_text("В этом бренде пока пусто.")
        return
    for pr in items:
        caption = f"*{pr.title}*\n{price_fmt(pr.price)}"
        if pr.sizes:
            caption += f"\nРазмеры: {pr.sizes}"
        caption += f"\n\n{(pr.descr or '')[:500]}"
        if pr.photo_file_id:
            await update.message.reply_photo(pr.photo_file_id, caption=caption, parse_mode=ParseMode.MARKDOWN, reply_markup=product_inline_kb(pr.id))
        else:
            await update.message.reply_text(caption, parse_mode=ParseMode.MARKDOWN, reply_markup=product_inline_kb(pr.id))
    # пагинация
    await update.message.reply_text(
        "Показать ещё ▶️",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Ещё", callback_data=f"morebrand:{category}:{brand}:{offset+6}")] ]
        )
    )

async def show_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    rows = await get_cart(update.effective_user.id)
    if not rows:
        await update.message.reply_text("Корзина пуста.")
        return
    total = 0
    lines = []
    for pid, title, price, qty in rows:
        lines.append(f"• {title} × {qty} = {price_fmt(price*qty)}")
        total += price * qty
    buttons = [[InlineKeyboardButton("🗑 Очистить корзину", callback_data="clearcart")]]
    if PAYMENT_PROVIDER_TOKEN:
        buttons.insert(0, [InlineKeyboardButton("✅ Оплатить", callback_data="checkout_pay")])
    else:
        buttons.insert(0, [InlineKeyboardButton("✅ Оформить заказ", callback_data="checkout")])
    kb = InlineKeyboardMarkup(buttons)
    await update.message.reply_text("Корзина:\n" + "\n".join(lines) + f"\n\nИтого: *{price_fmt(total)}*",
                                    parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

async def show_wardrobe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    rows = await get_wardrobe(update.effective_user.id)
    if not rows:
        await update.message.reply_text("Гардероб пуст.")
        return
    text = "Ваш гардероб:\n" + "\n".join([f"• {t} — {price_fmt(p)}" for _, t, p in rows])
    await update.message.reply_text(text)

async def show_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    rows = await list_orders(update.effective_user.id)
    if not rows:
        await update.message.reply_text("Покупок пока нет.")
        return
    parts = []
    for oid, total, status, created in rows:
        parts.append(f"Заказ #{oid} от {created[:16]} — {price_fmt(total)} — {status}")
    await update.message.reply_text("Ваши покупки:\n" + "\n".join(parts))

# ========= CALLBACKS =========
async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data.startswith("buy:"):
        pid = int(data.split(":")[1])
        await add_to_cart(q.from_user.id, pid)
        await q.message.reply_text("✅ Добавлено в корзину")

    elif data.startswith("wardrobe:"):
        pid = int(data.split(":")[1])
        await add_to_wardrobe(q.from_user.id, pid)
        await q.message.reply_text("👗 Сохранено в гардероб")

    elif data.startswith("morebrand:"):
        parts = data.split(":")
        cat, brand, off = parts[1], parts[2], int(parts[3])
        fake_update = Update(update.update_id, message=q.message)
        await show_products_by_brand(fake_update, context, category=cat, brand=brand, offset=off)

    elif data == "checkout":
        res = await create_order(q.from_user.id)
        if not res:
            await q.message.reply_text("Корзина пуста.")
            return
        order_id, total = res
        await q.message.reply_text(f"Заказ #{order_id} оформлен на сумму {price_fmt(total)} ✅\n"
                                   f"Статус: NEW. Мы свяжемся с вами для оплаты/доставки.")
        if ADMIN_CHAT_ID:
            await context.bot.send_message(
                int(ADMIN_CHAT_ID),
                f"Новый заказ #{order_id} от @{q.from_user.username or q.from_user.id} "
                f"на сумму {price_fmt(total)}"
            )

    elif data == "checkout_pay":
        # платеж через Telegram Payments
        rows = await get_cart(q.from_user.id)
        if not rows:
            await q.message.reply_text("Корзина пуста.")
            return
        total = sum(price * qty for _, _, price, qty in rows)
        title = "Оплата заказа"
        description = "Оплата товаров из корзины"
        prices = [LabeledPrice(label="Товары", amount=total)]
        await context.bot.send_invoice(
            chat_id=q.message.chat_id,
            title=title,
            description=description,
            payload=f"pay_{q.from_user.id}",
            provider_token=PAYMENT_PROVIDER_TOKEN,
            currency="RUB",
            prices=prices
        )

    elif data == "clearcart":
        await clear_cart(q.from_user.id)
        await q.message.reply_text("Корзина очищена.")

# ========= TEXT ROUTER =========
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        return  # работаем только в личке

    txt = update.message.text

    # сервисные кнопки
    if txt == BTN_CART:
        return await show_cart(update, context)
    if txt == BTN_WARDROBE:
        return await show_wardrobe(update, context)
    if txt == BTN_ORDERS:
        return await show_orders(update, context)
    if txt == BTN_FEEDBACK:
        await update.message.reply_text("Напишите сообщение. Отправлю админу. Отмена — /cancel")
        return ASK_FEEDBACK

    # навигация «назад»
    if txt == BTN_BACK_TO_CATS:
        context.user_data.pop("selected_category", None)
        context.user_data.pop("selected_brand", None)
        cats = await get_categories()
        return await update.message.reply_text("Выберите категорию:", reply_markup=build_categories_kb(cats))

    # фильтры командами: /filter 1000 5000, /size L, /clear_filters
    if txt.startswith("/filter"):
        parts = txt.split()
        pf = int(parts[1]) * 100 if len(parts) > 1 and parts[1].isdigit() else None
        pt = int(parts[2]) * 100 if len(parts) > 2 and parts[2].isdigit() else None
        context.user_data["price_from"] = pf
        context.user_data["price_to"] = pt
        await update.message.reply_text(f"Фильтр по цене установлен: от {parts[1] if pf else '-'} до {parts[2] if pt else '-'} ₽")
        return
    if txt.startswith("/size"):
        parts = txt.split(maxsplit=1)
        context.user_data["size_query"] = parts[1] if len(parts) > 1 else None
        await update.message.reply_text(f"Фильтр по размеру: {context.user_data.get('size_query') or '—'}")
        return
    if txt.startswith("/clear_filters"):
        for k in ("price_from","price_to","size_query"):
            context.user_data.pop(k, None)
        await update.message.reply_text("Фильтры очищены.")
        return

    # выбор категории
    cats = await get_categories()
    if txt in cats:
        context.user_data["selected_category"] = txt
        brands = await get_brands_by_category(txt)
        if not brands:
            return await update.message.reply_text("В этой категории пока нет брендов.",
                                                   reply_markup=build_categories_kb(cats))
        return await update.message.reply_text(f"Категория {txt}. Выберите бренд:",
                                               reply_markup=build_brands_kb(brands))

    # выбор бренда (когда категория уже выбрана)
    sel_cat = context.user_data.get("selected_category")
    if sel_cat:
        brands = await get_brands_by_category(sel_cat)
        if txt in brands:
            context.user_data["selected_brand"] = txt
            pf = context.user_data.get("price_from")
            pt = context.user_data.get("price_to")
            sz = context.user_data.get("size_query")
            return await show_products_by_brand(update, context, category=sel_cat, brand=txt, offset=0,
                                                price_from=pf, price_to=pt, size_query=sz)

    # дефолт — показать меню
    return await start(update, context)

# ========= FEEDBACK DIALOG =========
async def feedback_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message.text
    await update.message.reply_text("Спасибо! Передал админу.")
    if ADMIN_CHAT_ID:
        u = update.effective_user
        await context.bot.send_message(
            int(ADMIN_CHAT_ID),
            f"Обратная связь от @{u.username or u.id}:\n\n{msg}"
        )
    return ConversationHandler.END

async def feedback_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.")
    return ConversationHandler.END

# ========= IMPORT FROM FORWARDED POSTS =========
async def import_from_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Пересылай боту пост из канала (фото + подпись/текст).
    Возьмём: title (1-я строка), price, category/brand из #хэштегов,
    sizes, описание, фото по file_id. Сохраним как товар.
    """
    if update.effective_chat.type != ChatType.PRIVATE:
        return

    msg = update.message
    caption = msg.caption or msg.text or ""
    tags = parse_hashtags(caption)
    category = tags[0] if len(tags) >= 1 else "Общее"
    brand = tags[1] if len(tags) >= 2 else "NoBrand"

    title = first_line(caption)
    price = parse_price(caption)
    sizes = parse_sizes(caption)

    photo_file_id = None
    if msg.photo:
        photo_file_id = msg.photo[-1].file_id

    source_chat_id = None
    source_msg_id = None
    if msg.forward_from_chat and msg.forward_from_chat.id:
        source_chat_id = msg.forward_from_chat.id
        if msg.forward_from_message_id:
            source_msg_id = msg.forward_from_message_id

    p = Product(
        id=0,
        title=title,
        price=price if price is not None else 0,
        photo_file_id=photo_file_id,
        descr=caption,
        category=category,
        brand=brand,
        sizes=sizes,
        source_chat_id=source_chat_id,
        source_msg_id=source_msg_id
    )
    pid = await add_product(p)

    cats = await get_categories()
    await msg.reply_text(
        f"✅ Товар добавлен (id={pid}).\n"
        f"Категория: {category} • Бренд: {brand}\n"
        f"Цена: {price_fmt(p.price) if p.price else '—'}\n"
        f"Размеры: {sizes or '—'}\n"
        f"Откройте категорию → бренд, чтобы увидеть карточку.",
        reply_markup=build_categories_kb(cats)
    )

# ========= AUTO IMPORT FROM CHANNEL POSTS =========
async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Срабатывает, если бот добавлен админом канала. Импортируем новые посты автоматически."""
    msg = update.channel_post
    if not msg:
        return
    caption = msg.caption or msg.text or ""
    if not caption and not msg.photo:
        return  # нечего парсить

    tags = parse_hashtags(caption)
    category = tags[0] if len(tags) >= 1 else "Общее"
    brand = tags[1] if len(tags) >= 2 else "NoBrand"
    title = first_line(caption)
    price = parse_price(caption) or 0
    sizes = parse_sizes(caption)

    photo_file_id = None
    if msg.photo:
        photo_file_id = msg.photo[-1].file_id

    p = Product(
        id=0,
        title=title,
        price=price,
        photo_file_id=photo_file_id,
        descr=caption,
        category=category,
        brand=brand,
        sizes=sizes,
        source_chat_id=msg.chat_id,
        source_msg_id=msg.message_id
    )
    pid = await add_product(p)
    if ADMIN_CHAT_ID:
        await context.bot.send_message(int(ADMIN_CHAT_ID), f"Импортирован пост из канала как товар id={pid} ({category}/{brand}).")

# ========= CSV EXPORT / IMPORT =========
async def export_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != str(ADMIN_CHAT_ID):
        return
    path = "catalog_export.csv"
    header = ["id","title","price_rub","photo_file_id","descr","category","brand","sizes","source_chat_id","source_msg_id"]
    async with aiosqlite.connect(DB_PATH) as db, open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        async with db.execute("SELECT id,title,price,photo_file_id,descr,category,brand,sizes,source_chat_id,source_msg_id FROM products ORDER BY id DESC") as cur:
            async for row in cur:
                row = list(row)
                row[2] = row[2] / 100  # price в рублях
                writer.writerow(row)
    await update.message.reply_document(InputFile(path), filename="catalog_export.csv", caption="Экспорт каталога")

async def import_csv_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != str(ADMIN_CHAT_ID):
        return
    await update.message.reply_text("Пришлите CSV-файл с колонками: title,price_rub,photo_file_id,descr,category,brand,sizes")
    return IMPORT_WAIT_FILE

async def import_csv_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.document:
        await update.message.reply_text("Это не файл. Пришлите CSV-файл или /cancel")
        return IMPORT_WAIT_FILE
    file = await update.message.document.get_file()
    path = await file.download_to_drive(custom_path="import.csv")
    cnt = 0
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                title = row.get("title") or "Товар"
                price_rub = row.get("price_rub") or "0"
                price = int(float(str(price_rub).replace(",", ".")) * 100)
                photo_file_id = row.get("photo_file_id") or None
                descr = row.get("descr") or ""
                category = row.get("category") or "Общее"
                brand = row.get("brand") or "NoBrand"
                sizes = row.get("sizes") or None
                p = Product(0, title, price, photo_file_id, descr, category, brand, sizes, None, None)
                pid = await add_product(p)
                if pid:
                    cnt += 1
            except Exception:
                continue
    await update.message.reply_text(f"Импорт завершён. Добавлено товаров: {cnt}")
    return ConversationHandler.END

# ========= PAYMENTS HANDLERS =========
async def precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    await query.answer(ok=True)

async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    res = await create_order(update.effective_user.id)
    if not res:
        return
    order_id, total = res
    await update.message.reply_text(f"Оплата прошла успешно! Заказ #{order_id} на {price_fmt(total)} оформлен ✅")
    if ADMIN_CHAT_ID:
        await context.bot.send_message(int(ADMIN_CHAT_ID), f"Оплачен заказ #{order_id} от @{update.effective_user.username or update.effective_user.id} на сумму {price_fmt(total)}")

# ========= MAIN =========
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан. Создай .env с BOT_TOKEN=...")
    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    await init_db()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("export", export_csv))
    import_conv = ConversationHandler(
        entry_points=[CommandHandler("import", import_csv_cmd)],
        states={IMPORT_WAIT_FILE: [MessageHandler(filters.Document.ALL, import_csv_file)]},
        fallbacks=[CommandHandler("cancel", feedback_cancel)],
        allow_reentry=True
    )
    app.add_handler(import_conv)

    # Колбэки
    app.add_handler(CallbackQueryHandler(on_cb))

    # Диалог обратной связи
    feedback_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Text(BTN_FEEDBACK), on_text)],
        states={ASK_FEEDBACK: [MessageHandler(filters.TEXT & ~filters.COMMAND, feedback_save)]},
        fallbacks=[CommandHandler("cancel", feedback_cancel)],
        allow_reentry=True
    )
    app.add_handler(feedback_conv)

    # Импорт из пересланных постов (только личка)
    app.add_handler(MessageHandler(
        (filters.PHOTO | filters.TEXT) & filters.FORWARDED & filters.ChatType.PRIVATE,
        import_from_forward
    ))

    # Навигация (только личка)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_text))

    # Автоимпорт из канала (включится, когда бот будет админом)
    app.add_handler(ChannelPostHandler(on_channel_post, block=False))

    # Платежи
    if PAYMENT_PROVIDER_TOKEN:
        app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
        app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))

    print("Bot started. Press Ctrl+C to stop.")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
