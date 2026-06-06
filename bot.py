import os
import re
import sqlite3
import hashlib
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, ConversationHandler,
)

# ==========================
# CONFIG
# ==========================

TOKEN    = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])   # your Telegram user ID

DB_PATH = os.environ.get("DB_PATH", "inventory.db")
os.makedirs(os.path.dirname(DB_PATH) if os.path.dirname(DB_PATH) else ".", exist_ok=True)

# ConversationHandler states
(
    AWAIT_STORE_NAME,
    AWAIT_JOIN_CODE,
    AWAIT_SEARCH,
    AWAIT_ADD_BARCODE, AWAIT_ADD_ROW, AWAIT_ADD_POSITION,
    AWAIT_PHOTO_BARCODE, AWAIT_PHOTO_IMG,
    AWAIT_MOVE_BARCODE, AWAIT_MOVE_ROW, AWAIT_MOVE_POS,
    AWAIT_DELETE_BARCODE,
    AWAIT_APPROVE_TARGET,
    # Row management
    AWAIT_ROW_ACTION,
    AWAIT_ADDROW_NAME, AWAIT_ADDROW_ITEMS,
    AWAIT_APPENDROW_NAME, AWAIT_APPENDROW_ITEMS,
    AWAIT_SHOWROW_NAME,
    AWAIT_DELETEROW_NAME,
    AWAIT_RENAMEROW_OLD, AWAIT_RENAMEROW_NEW,
    AWAIT_INSERTROW_NAME, AWAIT_INSERTROW_POS, AWAIT_INSERTROW_BARCODE,
) = range(25)

# ==========================
# DATABASE
# ==========================

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

cur.executescript("""
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS stores (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL UNIQUE,
    join_code  TEXT    NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS users (
    tg_id    INTEGER NOT NULL,
    store_id INTEGER NOT NULL,
    role     TEXT    NOT NULL DEFAULT 'worker',
    name     TEXT,
    PRIMARY KEY (tg_id, store_id),
    FOREIGN KEY (store_id) REFERENCES stores(id)
);

CREATE TABLE IF NOT EXISTS inventory (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id INTEGER NOT NULL,
    barcode  TEXT    NOT NULL,
    row_name TEXT    NOT NULL,
    position INTEGER NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    UNIQUE(store_id, barcode, row_name, position),
    FOREIGN KEY (store_id) REFERENCES stores(id)
);

CREATE TABLE IF NOT EXISTS photos (
    store_id INTEGER NOT NULL,
    barcode  TEXT    NOT NULL,
    file_id  TEXT    NOT NULL,
    PRIMARY KEY (store_id, barcode),
    FOREIGN KEY (store_id) REFERENCES stores(id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id   INTEGER NOT NULL,
    tg_id      INTEGER NOT NULL,
    action     TEXT    NOT NULL,
    detail     TEXT,
    created_at TEXT    DEFAULT (datetime('now'))
);
""")
conn.commit()

# ==========================
# HELPERS
# ==========================

def make_join_code(name: str) -> str:
    return hashlib.sha1(name.encode()).hexdigest()[:8].upper()

def get_user(tg_id: int, store_id: int):
    return cur.execute(
        "SELECT * FROM users WHERE tg_id=? AND store_id=?", (tg_id, store_id)
    ).fetchone()

def get_store_by_code(code: str):
    return cur.execute(
        "SELECT * FROM stores WHERE join_code=?", (code.upper().strip(),)
    ).fetchone()

def get_store_by_id(store_id: int):
    return cur.execute("SELECT * FROM stores WHERE id=?", (store_id,)).fetchone()

def user_store(tg_id: int):
    """Return (store_id, role) for the user's active store, or (None, None)."""
    row = cur.execute(
        "SELECT store_id, role FROM users WHERE tg_id=? LIMIT 1", (tg_id,)
    ).fetchone()
    return (row["store_id"], row["role"]) if row else (None, None)

def is_admin(tg_id: int) -> bool:
    return tg_id == ADMIN_ID

def log(store_id, tg_id, action, detail=""):
    cur.execute(
        "INSERT INTO audit_log (store_id, tg_id, action, detail) VALUES (?,?,?,?)",
        (store_id, tg_id, action, detail)
    )
    conn.commit()

def get_photo(store_id, barcode):
    row = cur.execute(
        "SELECT file_id FROM photos WHERE store_id=? AND barcode=?", (store_id, barcode)
    ).fetchone()
    return row["file_id"] if row else None

def main_menu(role: str) -> ReplyKeyboardMarkup:
    """Persistent bottom keyboard based on role."""
    worker_keys = [
        [KeyboardButton("🔍 Search"), KeyboardButton("➕ Add item")],
        [KeyboardButton("🔀 Move product"), KeyboardButton("📷 Add photo")],
        [KeyboardButton("🗑 Delete item"), KeyboardButton("📦 Manage rows")],
        [KeyboardButton("ℹ️ Help")],
    ]
    manager_keys = worker_keys + [
        [KeyboardButton("📊 Audit log"), KeyboardButton("⚠️ Low stock")],
    ]
    keys = manager_keys if role in ("manager", "admin") else worker_keys
    return ReplyKeyboardMarkup(keys, resize_keyboard=True)

async def require_store(update: Update) -> tuple:
    """Returns (store_id, role) or sends error and returns (None, None)."""
    tg_id = update.effective_user.id
    if is_admin(tg_id):
        store_id, role = user_store(tg_id)
        if store_id:
            return store_id, "admin"
    store_id, role = user_store(tg_id)
    if not store_id:
        await update.message.reply_text(
            "❌ You are not part of any store yet.\n"
            "Use /join CODE to join a store, or ask your manager."
        )
        return None, None
    return store_id, role

# ==========================
# /START
# ==========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    store_id, role = user_store(tg_id)

    if is_admin(tg_id) and not store_id:
        await update.message.reply_text(
            "👋 Welcome, Admin!\n\n"
            "Create your first store with:\n`/newstore StoreName`",
            parse_mode="Markdown"
        )
        return

    if not store_id:
        await update.message.reply_text(
            "👋 Welcome to *Puma Depot Bot*!\n\n"
            "To get started, ask your manager for a join code, then send:\n"
            "`/join CODE`",
            parse_mode="Markdown"
        )
        return

    store = get_store_by_id(store_id)
    await update.message.reply_text(
        f"👋 Welcome back!\n🏪 Store: *{store['name']}*\n🔑 Role: *{role}*\n\n"
        "Use the buttons below to get started.",
        parse_mode="Markdown",
        reply_markup=main_menu(role)
    )

# ==========================
# ADMIN — /newstore
# ==========================

async def newstore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return

    if not context.args:
        await update.message.reply_text("Usage: `/newstore StoreName`", parse_mode="Markdown")
        return

    name = " ".join(context.args)
    code = make_join_code(name)

    try:
        cur.execute("INSERT INTO stores (name, join_code) VALUES (?,?)", (name, code))
        conn.commit()
        store_id = cur.lastrowid
        cur.execute(
            "INSERT OR IGNORE INTO users (tg_id, store_id, role, name) VALUES (?,?,?,?)",
            (ADMIN_ID, store_id, "admin", "Admin")
        )
        conn.commit()
        await update.message.reply_text(
            f"✅ Store *{name}* created!\n\n"
            f"🔑 Join code: `{code}`\n\n"
            f"Share this code with managers/workers so they can use `/join {code}`",
            parse_mode="Markdown"
        )
    except sqlite3.IntegrityError:
        await update.message.reply_text(f"⚠️ A store named *{name}* already exists.", parse_mode="Markdown")

# ==========================
# ADMIN — /liststores
# ==========================

async def liststores(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return

    rows = cur.execute("SELECT id, name, join_code FROM stores ORDER BY name").fetchall()
    if not rows:
        await update.message.reply_text("No stores yet. Use `/newstore Name`.", parse_mode="Markdown")
        return

    text = "🏪 *All stores:*\n\n"
    for r in rows:
        count = cur.execute(
            "SELECT COUNT(*) as c FROM users WHERE store_id=?", (r["id"],)
        ).fetchone()["c"]
        text += f"• *{r['name']}* — code: `{r['join_code']}` — {count} user(s)\n"

    await update.message.reply_text(text, parse_mode="Markdown")

# ==========================
# /join
# ==========================

async def join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/join CODE`", parse_mode="Markdown")
        return

    code = context.args[0]
    store = get_store_by_code(code)
    if not store:
        await update.message.reply_text("❌ Invalid join code.")
        return

    tg_id   = update.effective_user.id
    name    = update.effective_user.full_name
    existing = get_user(tg_id, store["id"])

    if existing:
        await update.message.reply_text(
            f"⚠️ You're already in *{store['name']}* as *{existing['role']}*.",
            parse_mode="Markdown"
        )
        return

    cur.execute(
        "INSERT INTO users (tg_id, store_id, role, name) VALUES (?,?,?,?)",
        (tg_id, store["id"], "worker", name)
    )
    conn.commit()
    log(store["id"], tg_id, "join", f"{name} joined as worker")

    await update.message.reply_text(
        f"✅ Joined *{store['name']}* as *worker*!\n\n"
        f"A manager will confirm your access shortly.\n"
        f"Use the menu below to get started.",
        parse_mode="Markdown",
        reply_markup=main_menu("worker")
    )

    # Notify all managers of this store
    managers = cur.execute(
        "SELECT tg_id FROM users WHERE store_id=? AND role IN ('manager','admin')",
        (store["id"],)
    ).fetchall()
    for m in managers:
        try:
            await context.bot.send_message(
                m["tg_id"],
                f"👤 New worker joined *{store['name']}*:\n"
                f"Name: {name}\nID: `{tg_id}`\n\n"
                f"To promote: `/promote {tg_id} manager`",
                parse_mode="Markdown"
            )
        except Exception:
            pass

# ==========================
# /promote  (manager or admin only)
# ==========================

async def promote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    store_id, role = user_store(tg_id)

    if role not in ("manager", "admin") and not is_admin(tg_id):
        await update.message.reply_text("❌ Manager or admin only.")
        return

    if len(context.args) != 2:
        await update.message.reply_text(
            "Usage: `/promote USER_ID ROLE`\nRoles: `worker` `manager`",
            parse_mode="Markdown"
        )
        return

    try:
        target_id   = int(context.args[0])
        target_role = context.args[1].lower()
    except ValueError:
        await update.message.reply_text("❌ USER_ID must be a number.")
        return

    if target_role not in ("worker", "manager"):
        await update.message.reply_text("❌ Role must be `worker` or `manager`.", parse_mode="Markdown")
        return

    target = get_user(target_id, store_id)
    if not target:
        await update.message.reply_text("❌ That user is not in your store.")
        return

    cur.execute(
        "UPDATE users SET role=? WHERE tg_id=? AND store_id=?",
        (target_role, target_id, store_id)
    )
    conn.commit()
    log(store_id, tg_id, "promote", f"{target_id} → {target_role}")

    await update.message.reply_text(
        f"✅ User `{target_id}` is now *{target_role}*.",
        parse_mode="Markdown"
    )
    try:
        await context.bot.send_message(
            target_id,
            f"🎉 Your role in the store has been updated to *{target_role}*!",
            parse_mode="Markdown"
        )
    except Exception:
        pass

# ==========================
# /members
# ==========================

async def members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = await require_store(update)
    if not store_id:
        return
    if role not in ("manager", "admin"):
        await update.message.reply_text("❌ Manager or admin only.")
        return

    rows = cur.execute(
        "SELECT tg_id, name, role FROM users WHERE store_id=? ORDER BY role, name",
        (store_id,)
    ).fetchall()

    store = get_store_by_id(store_id)
    text  = f"👥 *Members of {store['name']}:*\n\n"
    for r in rows:
        icon = "👑" if r["role"] == "admin" else ("🔑" if r["role"] == "manager" else "👷")
        text += f"{icon} {r['name'] or 'Unknown'} — `{r['tg_id']}` — *{r['role']}*\n"

    await update.message.reply_text(text, parse_mode="Markdown")

# ==========================
# SEARCH
# ==========================

async def search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = await require_store(update)
    if not store_id:
        return ConversationHandler.END
    context.user_data["store_id"] = store_id
    context.user_data["role"]     = role
    await update.message.reply_text("🔍 Enter barcode or partial code (e.g. `778`):", parse_mode="Markdown")
    return AWAIT_SEARCH

async def search_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query    = update.message.text.strip()
    store_id = context.user_data.get("store_id")
    if not store_id:
        store_id, role = user_store(update.effective_user.id)
        if not store_id:
            await update.message.reply_text("❌ Session lost. Please tap 🔍 Search again.")
            return ConversationHandler.END
        context.user_data["store_id"] = store_id
        context.user_data["role"] = role

    rows = cur.execute(
        """
        SELECT barcode, row_name, position, quantity
        FROM inventory
        WHERE store_id=? AND barcode LIKE ?
        ORDER BY barcode, row_name, position
        """,
        (store_id, f"%{query}%")
    ).fetchall()

    if not rows:
        await update.message.reply_text(
            f"❌ No results for `{query}`.",
            parse_mode="Markdown",
            reply_markup=main_menu(context.user_data.get("role", "worker"))
        )
        return ConversationHandler.END

    if len(rows) == 1:
        await send_product_card(update, context, store_id, rows[0])
        return ConversationHandler.END

    # Multiple matches — show tappable list
    buttons = []
    seen    = set()
    for r in rows:
        if r["barcode"] not in seen:
            seen.add(r["barcode"])
            buttons.append([InlineKeyboardButton(
                r["barcode"], callback_data=f"view:{store_id}:{r['barcode']}"
            )])

    await update.message.reply_text(
        f"🔍 Found *{len(seen)}* match(es) for `{query}` — tap to view:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return ConversationHandler.END

async def send_product_card(update_or_query, context, store_id, row):
    """Send a full product card with photo (if any) and inline action buttons."""
    barcode  = row["barcode"]
    location = f"📍 *{row['row_name']}* → box {row['position']}"
    qty      = row["quantity"]
    qty_text = f"📦 Qty: *{qty}*" + (" ⚠️ _Low stock!_" if qty <= 2 else "")
    caption  = f"🔍 *{barcode}*\n\n{location}\n{qty_text}"

    buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➖ Sold",      callback_data=f"sold:{store_id}:{barcode}"),
            InlineKeyboardButton("➕ Restocked", callback_data=f"restock:{store_id}:{barcode}"),
        ],
        [
            InlineKeyboardButton("🔀 Move",   callback_data=f"move:{store_id}:{barcode}"),
            InlineKeyboardButton("🗑 Delete", callback_data=f"del:{store_id}:{barcode}:{row['row_name']}:{row['position']}"),
        ],
    ])

    msg    = update_or_query.message if hasattr(update_or_query, "message") else update_or_query
    photo  = get_photo(store_id, barcode)

    if photo:
        await msg.reply_photo(photo=photo, caption=caption, parse_mode="Markdown", reply_markup=buttons)
    else:
        await msg.reply_text(
            caption + "\n\n_No photo — use 📷 Add photo to attach one._",
            parse_mode="Markdown",
            reply_markup=buttons
        )

async def view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button tap from search result list."""
    query = update.callback_query
    await query.answer()
    _, store_id, barcode = query.data.split(":", 2)
    store_id = int(store_id)

    rows = cur.execute(
        "SELECT barcode, row_name, position, quantity FROM inventory WHERE store_id=? AND barcode=?",
        (store_id, barcode)
    ).fetchall()

    if not rows:
        await query.message.reply_text("❌ Not found.")
        return

    # If multiple locations, show each as a card
    for r in rows:
        await send_product_card(query, context, store_id, r)

# ==========================
# INLINE ACTIONS — sold / restock / move / delete
# ==========================

async def action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    action = parts[0]

    store_id = int(parts[1])
    barcode  = parts[2]
    tg_id    = query.from_user.id

    if action == "sold":
        cur.execute(
            "UPDATE inventory SET quantity = MAX(0, quantity-1) WHERE store_id=? AND barcode=?",
            (store_id, barcode)
        )
        conn.commit()
        log(store_id, tg_id, "sold", barcode)
        new_qty = cur.execute(
            "SELECT MIN(quantity) as q FROM inventory WHERE store_id=? AND barcode=?",
            (store_id, barcode)
        ).fetchone()["q"]
        await query.message.reply_text(
            f"✅ Marked 1 sold for `{barcode}`.\n📦 Remaining: *{new_qty}*" +
            ("\n\n⚠️ *Low stock!* Manager has been notified." if new_qty <= 2 else ""),
            parse_mode="Markdown"
        )
        if new_qty <= 2:
            await notify_managers_low_stock(context, store_id, barcode, new_qty)

    elif action == "restock":
        cur.execute(
            "UPDATE inventory SET quantity = quantity+1 WHERE store_id=? AND barcode=?",
            (store_id, barcode)
        )
        conn.commit()
        log(store_id, tg_id, "restock", barcode)
        new_qty = cur.execute(
            "SELECT MIN(quantity) as q FROM inventory WHERE store_id=? AND barcode=?",
            (store_id, barcode)
        ).fetchone()["q"]
        await query.message.reply_text(
            f"✅ Restocked `{barcode}`.\n📦 Now: *{new_qty}*",
            parse_mode="Markdown"
        )

    elif action == "del":
        row_name = parts[3]
        position = int(parts[4])
        cur.execute(
            "DELETE FROM inventory WHERE store_id=? AND barcode=? AND row_name=? AND position=?",
            (store_id, barcode, row_name, position)
        )
        conn.commit()
        log(store_id, tg_id, "delete", f"{barcode} from {row_name} pos {position}")
        await query.message.reply_text(
            f"🗑 Deleted `{barcode}` from *{row_name}*, position {position}.",
            parse_mode="Markdown"
        )

async def notify_managers_low_stock(context, store_id, barcode, qty):
    store    = get_store_by_id(store_id)
    managers = cur.execute(
        "SELECT tg_id FROM users WHERE store_id=? AND role IN ('manager','admin')",
        (store_id,)
    ).fetchall()
    for m in managers:
        try:
            await context.bot.send_message(
                m["tg_id"],
                f"⚠️ *Low stock alert* — {store['name']}\n\n"
                f"Barcode `{barcode}` has only *{qty}* left.",
                parse_mode="Markdown"
            )
        except Exception:
            pass

# ==========================
# ADD ITEM (conversation)
# ==========================

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = await require_store(update)
    if not store_id:
        return ConversationHandler.END
    context.user_data["store_id"] = store_id
    context.user_data["role"]     = role
    await update.message.reply_text("➕ Barcode of the new item:")
    return AWAIT_ADD_BARCODE

async def add_barcode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Recover store_id if user_data was wiped (e.g. bot restart)
    if "store_id" not in context.user_data:
        store_id, role = user_store(update.effective_user.id)
        if not store_id:
            await update.message.reply_text("❌ Session lost. Please tap ➕ Add item again.")
            return ConversationHandler.END
        context.user_data["store_id"] = store_id
        context.user_data["role"] = role

    context.user_data["new_barcode"] = update.message.text.strip()
    rows = cur.execute(
        "SELECT DISTINCT row_name FROM inventory WHERE store_id=? ORDER BY row_name",
        (context.user_data["store_id"],)
    ).fetchall()
    if rows:
        buttons = [[InlineKeyboardButton(r["row_name"], callback_data=f"row:{r['row_name']}")] for r in rows]
        buttons.append([InlineKeyboardButton("✏️ Type new row name", callback_data="row:__new__")])
        await update.message.reply_text(
            "📋 Select a row or type a new one:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    else:
        await update.message.reply_text("📋 Row name (e.g. `Новая_коллекция_1ряд`):", parse_mode="Markdown")
    return AWAIT_ADD_ROW

async def add_row_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    # Recover store_id if lost
    if "store_id" not in context.user_data:
        store_id, role = user_store(query.from_user.id)
        if not store_id:
            await query.message.reply_text("❌ Session lost. Please tap ➕ Add item again.")
            return ConversationHandler.END
        context.user_data["store_id"] = store_id
        context.user_data["role"] = role
    if query.data == "row:__new__":
        await query.message.reply_text("✏️ Type the new row name:")
        return AWAIT_ADD_ROW
    row_name = query.data.split(":", 1)[1]
    context.user_data["new_row"] = row_name
    await query.message.reply_text(f"📦 Box / position number in *{row_name}*:", parse_mode="Markdown")
    return AWAIT_ADD_POSITION

async def add_row_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_row"] = update.message.text.strip()
    await update.message.reply_text(f"📦 Box / position number:")
    return AWAIT_ADD_POSITION

async def add_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        position = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Position must be a number. Try again:")
        return AWAIT_ADD_POSITION

    store_id = context.user_data["store_id"]
    barcode  = context.user_data["new_barcode"]
    row_name = context.user_data["new_row"]
    tg_id    = update.effective_user.id

    try:
        cur.execute(
            "INSERT INTO inventory (store_id, barcode, row_name, position) VALUES (?,?,?,?)",
            (store_id, barcode, row_name, position)
        )
        conn.commit()
        log(store_id, tg_id, "add", f"{barcode} → {row_name} pos {position}")
        await update.message.reply_text(
            f"✅ Added `{barcode}` → *{row_name}*, box {position}\n\n"
            f"💡 Tap *📷 Add photo* to attach a product photo.",
            parse_mode="Markdown",
            reply_markup=main_menu(context.user_data.get("role", "worker"))
        )
    except sqlite3.IntegrityError:
        await update.message.reply_text(
            f"⚠️ `{barcode}` already exists at *{row_name}*, box {position}.",
            parse_mode="Markdown",
            reply_markup=main_menu(context.user_data.get("role", "worker"))
        )
    return ConversationHandler.END

# ==========================
# ADD PHOTO (conversation)
# ==========================

async def photo_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = await require_store(update)
    if not store_id:
        return ConversationHandler.END
    context.user_data["store_id"] = store_id
    context.user_data["role"]     = role
    await update.message.reply_text("📷 Enter the barcode to attach a photo to:")
    return AWAIT_PHOTO_BARCODE

async def photo_barcode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    barcode  = update.message.text.strip()
    if "store_id" not in context.user_data:
        store_id, role = user_store(update.effective_user.id)
        if not store_id:
            await update.message.reply_text("❌ Session lost. Please tap 📷 Add photo again.")
            return ConversationHandler.END
        context.user_data["store_id"] = store_id
        context.user_data["role"] = role
    store_id = context.user_data["store_id"]

    exists = cur.execute(
        "SELECT COUNT(*) as c FROM inventory WHERE store_id=? AND barcode=?",
        (store_id, barcode)
    ).fetchone()["c"]

    if not exists:
        await update.message.reply_text(
            f"❌ `{barcode}` not found in inventory. Add it first with ➕ Add item.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

    context.user_data["photo_barcode"] = barcode
    existing = get_photo(store_id, barcode)
    note = " _(replaces existing photo)_" if existing else ""
    await update.message.reply_text(
        f"📷 Now send the photo for `{barcode}`{note}.",
        parse_mode="Markdown"
    )
    return AWAIT_PHOTO_IMG

async def photo_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id = context.user_data["store_id"]
    barcode  = context.user_data["photo_barcode"]
    file_id  = update.message.photo[-1].file_id
    tg_id    = update.effective_user.id

    cur.execute(
        """
        INSERT INTO photos (store_id, barcode, file_id) VALUES (?,?,?)
        ON CONFLICT(store_id, barcode) DO UPDATE SET file_id=excluded.file_id
        """,
        (store_id, barcode, file_id)
    )
    conn.commit()
    log(store_id, tg_id, "add_photo", barcode)

    await update.message.reply_text(
        f"✅ Photo saved for `{barcode}`!",
        parse_mode="Markdown",
        reply_markup=main_menu(context.user_data.get("role", "worker"))
    )
    return ConversationHandler.END

# ==========================
# MOVE PRODUCT (conversation)
# ==========================

async def move_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = await require_store(update)
    if not store_id:
        return ConversationHandler.END
    context.user_data["store_id"] = store_id
    context.user_data["role"]     = role
    await update.message.reply_text("🔀 Barcode of the product to move:")
    return AWAIT_MOVE_BARCODE

async def move_barcode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    barcode  = update.message.text.strip()
    if "store_id" not in context.user_data:
        store_id, role = user_store(update.effective_user.id)
        if not store_id:
            await update.message.reply_text("❌ Session lost. Please tap 🔀 Move product again.")
            return ConversationHandler.END
        context.user_data["store_id"] = store_id
        context.user_data["role"] = role
    store_id = context.user_data["store_id"]

    rows = cur.execute(
        "SELECT row_name, position FROM inventory WHERE store_id=? AND barcode=?",
        (store_id, barcode)
    ).fetchall()

    if not rows:
        await update.message.reply_text(f"❌ `{barcode}` not found.", parse_mode="Markdown")
        return ConversationHandler.END

    context.user_data["move_barcode"] = barcode
    locs = ", ".join(f"{r['row_name']} box {r['position']}" for r in rows)
    await update.message.reply_text(
        f"📍 Currently at: {locs}\n\nNew row name:"
    )
    return AWAIT_MOVE_ROW

async def move_row(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["move_row"] = update.message.text.strip()
    await update.message.reply_text("📦 New box / position number:")
    return AWAIT_MOVE_POS

async def move_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        new_pos = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Must be a number. Try again:")
        return AWAIT_MOVE_POS

    store_id = context.user_data["store_id"]
    barcode  = context.user_data["move_barcode"]
    new_row  = context.user_data["move_row"]
    tg_id    = update.effective_user.id

    cur.execute(
        "UPDATE inventory SET row_name=?, position=? WHERE store_id=? AND barcode=?",
        (new_row, new_pos, store_id, barcode)
    )
    conn.commit()
    log(store_id, tg_id, "move", f"{barcode} → {new_row} pos {new_pos}")

    await update.message.reply_text(
        f"✅ Moved `{barcode}` to *{new_row}*, box {new_pos}.",
        parse_mode="Markdown",
        reply_markup=main_menu(context.user_data.get("role", "worker"))
    )
    return ConversationHandler.END

# ==========================
# DELETE ITEM (conversation)
# ==========================

async def delete_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = await require_store(update)
    if not store_id:
        return ConversationHandler.END
    context.user_data["store_id"] = store_id
    context.user_data["role"]     = role
    await update.message.reply_text("🗑 Enter the barcode to delete:")
    return AWAIT_DELETE_BARCODE

async def delete_barcode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    barcode  = update.message.text.strip()
    if "store_id" not in context.user_data:
        store_id, role = user_store(update.effective_user.id)
        if not store_id:
            await update.message.reply_text("❌ Session lost. Please tap 🗑 Delete item again.")
            return ConversationHandler.END
        context.user_data["store_id"] = store_id
        context.user_data["role"] = role
    store_id = context.user_data["store_id"]

    rows = cur.execute(
        "SELECT id, row_name, position FROM inventory WHERE store_id=? AND barcode=?",
        (store_id, barcode)
    ).fetchall()

    if not rows:
        await update.message.reply_text(f"❌ `{barcode}` not found.", parse_mode="Markdown",
            reply_markup=main_menu(context.user_data.get("role","worker")))
        return ConversationHandler.END

    buttons = [
        [InlineKeyboardButton(
            f"{r['row_name']} — box {r['position']}",
            callback_data=f"delrow:{store_id}:{barcode}:{r['row_name']}:{r['position']}"
        )]
        for r in rows
    ]
    buttons.append([InlineKeyboardButton("🗑 Delete ALL locations", callback_data=f"delall:{store_id}:{barcode}")])

    await update.message.reply_text(
        f"Which entry to delete for `{barcode}`?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    return ConversationHandler.END

async def delete_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts    = query.data.split(":")
    action   = parts[0]
    store_id = int(parts[1])
    barcode  = parts[2]
    tg_id    = query.from_user.id
    store_id_ud, role = user_store(tg_id)

    if action == "delrow":
        row_name = parts[3]
        position = int(parts[4])
        cur.execute(
            "DELETE FROM inventory WHERE store_id=? AND barcode=? AND row_name=? AND position=?",
            (store_id, barcode, row_name, position)
        )
        conn.commit()
        log(store_id, tg_id, "delete", f"{barcode} @ {row_name} pos {position}")
        await query.message.reply_text(
            f"🗑 Deleted `{barcode}` from *{row_name}*, box {position}.",
            parse_mode="Markdown",
            reply_markup=main_menu(role or "worker")
        )

    elif action == "delall":
        cur.execute("DELETE FROM inventory WHERE store_id=? AND barcode=?", (store_id, barcode))
        cur.execute("DELETE FROM photos WHERE store_id=? AND barcode=?", (store_id, barcode))
        conn.commit()
        log(store_id, tg_id, "delete_all", barcode)
        await query.message.reply_text(
            f"🗑 Deleted all entries for `{barcode}` (including photo).",
            parse_mode="Markdown",
            reply_markup=main_menu(role or "worker")
        )

# ==========================
# SHOW ROWS
# ==========================

async def show_rows(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = await require_store(update)
    if not store_id:
        return

    rows = cur.execute(
        "SELECT row_name, COUNT(*) as total FROM inventory WHERE store_id=? GROUP BY row_name ORDER BY row_name",
        (store_id,)
    ).fetchall()

    if not rows:
        await update.message.reply_text("📭 No rows yet.", reply_markup=main_menu(role))
        return

    store = get_store_by_id(store_id)
    text  = f"📋 *{store['name']} — all rows:*\n\n"
    for r in rows:
        text += f"• *{r['row_name']}* — {r['total']} item(s)\n"

    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=main_menu(role))

# ==========================
# AUDIT LOG (manager+)
# ==========================

async def audit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = await require_store(update)
    if not store_id:
        return
    if role not in ("manager", "admin"):
        await update.message.reply_text("❌ Manager or admin only.")
        return

    rows = cur.execute(
        """
        SELECT action, detail, tg_id, created_at
        FROM audit_log
        WHERE store_id=?
        ORDER BY id DESC LIMIT 20
        """,
        (store_id,)
    ).fetchall()

    if not rows:
        await update.message.reply_text("📋 No activity yet.")
        return

    store = get_store_by_id(store_id)
    text  = f"📋 *{store['name']} — last 20 actions:*\n\n"
    for r in rows:
        text += f"`{r['created_at'][:16]}` — {r['action']} — {r['detail']} (by `{r['tg_id']}`)\n"

    await update.message.reply_text(text, parse_mode="Markdown")

# ==========================
# LOW STOCK (manager+)
# ==========================

async def low_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = await require_store(update)
    if not store_id:
        return
    if role not in ("manager", "admin"):
        await update.message.reply_text("❌ Manager or admin only.")
        return

    rows = cur.execute(
        """
        SELECT barcode, row_name, position, quantity
        FROM inventory
        WHERE store_id=? AND quantity <= 2
        ORDER BY quantity, barcode
        """,
        (store_id,)
    ).fetchall()

    if not rows:
        await update.message.reply_text("✅ No low stock items.")
        return

    store = get_store_by_id(store_id)
    text  = f"⚠️ *{store['name']} — low stock:*\n\n"
    for r in rows:
        text += f"• `{r['barcode']}` — {r['row_name']} box {r['position']} — *{r['quantity']}* left\n"

    await update.message.reply_text(text, parse_mode="Markdown")


# ==========================
# ROW MANAGEMENT
# ==========================

ROW_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("📋 Show row",        callback_data="rowmgr:show")],
    [InlineKeyboardButton("➕ Add new row",      callback_data="rowmgr:addrow")],
    [InlineKeyboardButton("📎 Append to row",   callback_data="rowmgr:append")],
    [InlineKeyboardButton("📌 Insert at position", callback_data="rowmgr:insert")],
    [InlineKeyboardButton("✏️ Rename row",      callback_data="rowmgr:rename")],
    [InlineKeyboardButton("🗑 Delete row",       callback_data="rowmgr:delete")],
])

async def rowmgr_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = await require_store(update)
    if not store_id:
        return ConversationHandler.END
    context.user_data["store_id"] = store_id
    context.user_data["role"]     = role
    await update.message.reply_text("📦 *Row management* — choose an action:", parse_mode="Markdown", reply_markup=ROW_MENU)
    return AWAIT_ROW_ACTION

async def rowmgr_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Standalone entry for show row (called from inline button outside conversation)."""
    store_id, role = await require_store(update)
    if not store_id:
        return ConversationHandler.END
    context.user_data["store_id"] = store_id
    context.user_data["role"] = role
    await update.message.reply_text("📋 Which row to show? Enter row name:")
    return AWAIT_SHOWROW_NAME

async def rowmgr_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split(":")[1]
    # Ensure store_id is set — may be missing if bot restarted
    if "store_id" not in context.user_data:
        store_id, role = user_store(query.from_user.id)
        if not store_id:
            await query.message.reply_text("❌ Session expired. Please tap the button again.")
            return ConversationHandler.END
        context.user_data["store_id"] = store_id
        context.user_data["role"] = role

    if action == "show":
        await query.message.reply_text("📋 Which row to show? Enter row name:")
        context.user_data["rowmgr_action"] = "show"
        return AWAIT_SHOWROW_NAME

    elif action == "addrow":
        await query.message.reply_text(
            "➕ *Add new row*\n\nEnter the row name:",
            parse_mode="Markdown"
        )
        context.user_data["rowmgr_action"] = "addrow"
        return AWAIT_ADDROW_NAME

    elif action == "append":
        rows = cur.execute(
            "SELECT DISTINCT row_name FROM inventory WHERE store_id=? ORDER BY row_name",
            (context.user_data["store_id"],)
        ).fetchall()
        if not rows:
            await query.message.reply_text("❌ No rows yet. Create one first with ➕ Add new row.")
            return ConversationHandler.END
        buttons = [[InlineKeyboardButton(r["row_name"], callback_data=f"pickrow:{r['row_name']}")] for r in rows]
        await query.message.reply_text("📎 *Append to row* — pick a row:", parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons))
        context.user_data["rowmgr_action"] = "append"
        return AWAIT_APPENDROW_NAME

    elif action == "insert":
        rows = cur.execute(
            "SELECT DISTINCT row_name FROM inventory WHERE store_id=? ORDER BY row_name",
            (context.user_data["store_id"],)
        ).fetchall()
        if not rows:
            await query.message.reply_text("❌ No rows yet.")
            return ConversationHandler.END
        buttons = [[InlineKeyboardButton(r["row_name"], callback_data=f"pickrow:{r['row_name']}")] for r in rows]
        await query.message.reply_text("📌 *Insert at position* — pick a row:", parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons))
        context.user_data["rowmgr_action"] = "insert"
        return AWAIT_INSERTROW_NAME

    elif action == "rename":
        rows = cur.execute(
            "SELECT DISTINCT row_name FROM inventory WHERE store_id=? ORDER BY row_name",
            (context.user_data["store_id"],)
        ).fetchall()
        if not rows:
            await query.message.reply_text("❌ No rows yet.")
            return ConversationHandler.END
        buttons = [[InlineKeyboardButton(r["row_name"], callback_data=f"pickrow:{r['row_name']}")] for r in rows]
        await query.message.reply_text("✏️ *Rename row* — pick a row:", parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons))
        context.user_data["rowmgr_action"] = "rename"
        return AWAIT_RENAMEROW_OLD

    elif action == "delete":
        rows = cur.execute(
            "SELECT DISTINCT row_name FROM inventory WHERE store_id=? ORDER BY row_name",
            (context.user_data["store_id"],)
        ).fetchall()
        if not rows:
            await query.message.reply_text("❌ No rows yet.")
            return ConversationHandler.END
        buttons = [[InlineKeyboardButton(r["row_name"], callback_data=f"pickrow:{r['row_name']}")] for r in rows]
        await query.message.reply_text("🗑 *Delete row* — pick a row:", parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(buttons))
        context.user_data["rowmgr_action"] = "delete"
        return AWAIT_DELETEROW_NAME

# ── pickrow callback (shared across sub-actions) ──

async def pickrow_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    row_name = query.data.split(":", 1)[1]
    action   = context.user_data.get("rowmgr_action")
    store_id = context.user_data["store_id"]

    if action == "append":
        context.user_data["target_row"] = row_name
        max_pos = cur.execute(
            "SELECT COALESCE(MAX(position),0) FROM inventory WHERE store_id=? AND row_name=?",
            (store_id, row_name)
        ).fetchone()[0]
        await query.message.reply_text(
            f"📎 Appending to *{row_name}* (currently {max_pos} positions).\n\nSend barcodes separated by spaces. Each word = next position.\nComma-separate multiple barcodes in the same position.\nExample: `111 222,223 333`",
            parse_mode="Markdown"
        )
        return AWAIT_APPENDROW_ITEMS

    elif action == "insert":
        context.user_data["target_row"] = row_name
        # Show current row so user knows positions
        items = cur.execute(
            "SELECT position, GROUP_CONCAT(barcode, ', ') as barcodes FROM inventory "
            "WHERE store_id=? AND row_name=? GROUP BY position ORDER BY position",
            (store_id, row_name)
        ).fetchall()
        preview = "\n".join(f"  {r['position']}. {r['barcodes']}" for r in items) if items else "  (empty)"
        await query.message.reply_text(
            f"📌 *{row_name}* current positions:\n{preview}\n\nAt which position to insert? (existing items shift down)",
            parse_mode="Markdown"
        )
        return AWAIT_INSERTROW_POS

    elif action == "rename":
        context.user_data["target_row"] = row_name
        await query.message.reply_text(f"✏️ New name for *{row_name}*:", parse_mode="Markdown")
        return AWAIT_RENAMEROW_NEW

    elif action == "delete":
        count = cur.execute(
            "SELECT COUNT(*) FROM inventory WHERE store_id=? AND row_name=?",
            (store_id, row_name)
        ).fetchone()[0]
        buttons = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes, delete", callback_data=f"confirmdelrow:{row_name}"),
            InlineKeyboardButton("❌ Cancel",      callback_data="canceldelrow"),
        ]])
        await query.message.reply_text(
            f"⚠️ Delete row *{row_name}* and all *{count}* item(s) in it?",
            parse_mode="Markdown",
            reply_markup=buttons
        )
        return AWAIT_DELETEROW_NAME

# ── SHOW ROW ──

async def showrow_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    row_name = update.message.text.strip()
    store_id = context.user_data["store_id"]
    role     = context.user_data["role"]
    return await _send_row(update.message, store_id, row_name, role)

async def _send_row(msg, store_id, row_name, role):
    items = cur.execute(
        "SELECT position, GROUP_CONCAT(barcode, ', ') as barcodes FROM inventory "
        "WHERE store_id=? AND row_name=? GROUP BY position ORDER BY position",
        (store_id, row_name)
    ).fetchall()
    if not items:
        await msg.reply_text(f"❌ Row *{row_name}* not found.", parse_mode="Markdown",
            reply_markup=main_menu(role))
        return ConversationHandler.END
    text = f"📋 *{row_name}*\n\n"
    for r in items:
        has_photo = any(
            get_photo(store_id, b.strip())
            for b in r["barcodes"].split(",")
        )
        photo_mark = " 📷" if has_photo else ""
        text += f"`{r['position']}.` {r['barcodes']}{photo_mark}\n"
    text += "\n_📷 = has photo_"
    await msg.reply_text(text, parse_mode="Markdown", reply_markup=main_menu(role))
    return ConversationHandler.END

# ── ADD NEW ROW ──

async def addrow_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["target_row"] = update.message.text.strip()
    await update.message.reply_text(
        "Send barcodes separated by spaces. Each word = next box position.\n"
        "Comma-separate for multiple barcodes at the same position.\n\n"
        "Example: `111 222,223 333`",
        parse_mode="Markdown"
    )
    return AWAIT_ADDROW_ITEMS

async def addrow_items(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id = context.user_data["store_id"]
    row_name = context.user_data["target_row"]
    role     = context.user_data["role"]
    tg_id    = update.effective_user.id
    groups   = update.message.text.strip().split()

    count = 0
    skipped = 0
    for position, group in enumerate(groups, start=1):
        for barcode in [b.strip() for b in group.split(",") if b.strip()]:
            try:
                cur.execute(
                    "INSERT INTO inventory (store_id, barcode, row_name, position) VALUES (?,?,?,?)",
                    (store_id, barcode, row_name, position)
                )
                count += 1
            except sqlite3.IntegrityError:
                skipped += 1
    conn.commit()
    log(store_id, tg_id, "addrow", f"{row_name} +{count} items")

    msg = f"✅ Added *{count}* barcode(s) to new row *{row_name}*."
    if skipped:
        msg += f" Skipped {skipped} duplicate(s)."
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_menu(role))
    return ConversationHandler.END

# ── APPEND TO ROW ──

async def appendrow_items(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id = context.user_data["store_id"]
    row_name = context.user_data["target_row"]
    role     = context.user_data["role"]
    tg_id    = update.effective_user.id
    groups   = update.message.text.strip().split()

    next_pos = cur.execute(
        "SELECT COALESCE(MAX(position),0)+1 FROM inventory WHERE store_id=? AND row_name=?",
        (store_id, row_name)
    ).fetchone()[0]

    count = 0
    skipped = 0
    for group in groups:
        for barcode in [b.strip() for b in group.split(",") if b.strip()]:
            try:
                cur.execute(
                    "INSERT INTO inventory (store_id, barcode, row_name, position) VALUES (?,?,?,?)",
                    (store_id, barcode, row_name, next_pos)
                )
                count += 1
            except sqlite3.IntegrityError:
                skipped += 1
        next_pos += 1
    conn.commit()
    log(store_id, tg_id, "appendrow", f"{row_name} +{count} items")

    msg = f"✅ Appended *{count}* barcode(s) to *{row_name}*."
    if skipped:
        msg += f" Skipped {skipped} duplicate(s)."
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_menu(role))
    return ConversationHandler.END

# ── INSERT AT POSITION ──

async def insertrow_pos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        pos = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Must be a number. Try again:")
        return AWAIT_INSERTROW_POS
    context.user_data["insert_pos"] = pos
    await update.message.reply_text(
        f"📌 Inserting at position *{pos}* (items below will shift down).\n\n"
        "Enter the barcode(s) to insert at this position (comma-separate for multiple):",
        parse_mode="Markdown"
    )
    return AWAIT_INSERTROW_BARCODE

async def insertrow_barcode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id = context.user_data["store_id"]
    row_name = context.user_data["target_row"]
    pos      = context.user_data["insert_pos"]
    role     = context.user_data["role"]
    tg_id    = update.effective_user.id
    barcodes = [b.strip() for b in update.message.text.split(",") if b.strip()]

    # Shift all existing items at >= pos down by 1
    cur.execute(
        "UPDATE inventory SET position = position + 1 WHERE store_id=? AND row_name=? AND position >= ?",
        (store_id, row_name, pos)
    )

    count = 0
    for barcode in barcodes:
        try:
            cur.execute(
                "INSERT INTO inventory (store_id, barcode, row_name, position) VALUES (?,?,?,?)",
                (store_id, barcode, row_name, pos)
            )
            count += 1
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    log(store_id, tg_id, "insert", f"{', '.join(barcodes)} → {row_name} pos {pos}")

    await update.message.reply_text(
        f"✅ Inserted *{count}* barcode(s) at position {pos} in *{row_name}*.\n"
        f"All items from position {pos} downward were shifted.",
        parse_mode="Markdown",
        reply_markup=main_menu(role)
    )
    return ConversationHandler.END

# ── RENAME ROW ──

async def renamerow_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id = context.user_data["store_id"]
    old_name = context.user_data["target_row"]
    new_name = update.message.text.strip()
    role     = context.user_data["role"]
    tg_id    = update.effective_user.id

    cur.execute(
        "UPDATE inventory SET row_name=? WHERE store_id=? AND row_name=?",
        (new_name, store_id, old_name)
    )
    conn.commit()
    log(store_id, tg_id, "rename_row", f"{old_name} -> {new_name}")

    await update.message.reply_text(
        f"✅ Renamed *{old_name}* → *{new_name}*.",
        parse_mode="Markdown",
        reply_markup=main_menu(role)
    )
    return ConversationHandler.END

# ── DELETE ROW confirm/cancel callbacks ──

async def confirmdelrow_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query    = update.callback_query
    await query.answer()
    row_name = query.data.split(":", 1)[1]
    tg_id    = query.from_user.id
    store_id, role = user_store(tg_id)

    cur.execute("DELETE FROM inventory WHERE store_id=? AND row_name=?", (store_id, row_name))
    conn.commit()
    log(store_id, tg_id, "delete_row", row_name)

    await query.message.reply_text(
        f"🗑 Row *{row_name}* deleted.",
        parse_mode="Markdown",
        reply_markup=main_menu(role or "worker")
    )
    return ConversationHandler.END

async def canceldelrow_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tg_id    = query.from_user.id
    _, role  = user_store(tg_id)
    await query.message.reply_text("↩️ Cancelled.", reply_markup=main_menu(role or "worker"))
    return ConversationHandler.END

# ==========================
# HELP
# ==========================

HELP_TEXT = """
👟 *Puma Depot Bot — Help*

*🔍 Search* — Find by barcode (partial OK, e.g. 778)
*➕ Add item* — Add a single barcode to a row/box
*🔀 Move product* — Change a product's location
*📷 Add photo* — Attach a photo to a barcode
*🗑 Delete item* — Remove a barcode entry

*📦 Manage rows:*
• 📋 Show row — see all barcodes in a row
• ➕ Add new row — create row with all items at once
• 📎 Append to row — add more items to end of row
• 📌 Insert at position — insert item, shifts others down
• ✏️ Rename row — rename a row
• 🗑 Delete row — delete entire row

After searching, tap on the result to:
• ➖ Sold — reduce quantity by 1
• ➕ Restocked — increase quantity by 1
• 🔀 Move — change location
• 🗑 Delete — remove entry

*Manager only:*
• 📊 Audit log — last 20 actions
• ⚠️ Low stock — items with ≤ 2 left
• `/promote USER_ID ROLE` — change a user's role
• `/members` — list store members

*Admin only:*
• `/newstore NAME` — create a new store
• `/liststores` — list all stores
"""

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _, role = await require_store(update)
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown",
        reply_markup=main_menu(role or "worker"))

# ==========================
# KEYBOARD BUTTON ROUTER
# ==========================

async def keyboard_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route persistent keyboard button taps to the right conversation."""
    text = update.message.text
    if text == "🔍 Search":
        return await search_start(update, context)
    if text == "➕ Add item":
        return await add_start(update, context)
    if text == "🔀 Move product":
        return await move_start(update, context)
    if text == "📷 Add photo":
        return await photo_start(update, context)
    if text == "🗑 Delete item":
        return await delete_start(update, context)
    if text == "📦 Manage rows":
        return await rowmgr_start(update, context)
    if text == "📊 Audit log":
        return await audit(update, context)
    if text == "⚠️ Low stock":
        return await low_stock(update, context)
    if text == "ℹ️ Help":
        return await help_cmd(update, context)

# ==========================
# CANCEL
# ==========================

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _, role = user_store(update.effective_user.id)
    await update.message.reply_text(
        "↩️ Cancelled.", reply_markup=main_menu(role or "worker")
    )
    return ConversationHandler.END

# ==========================
# BOT SETUP
# ==========================

BUTTON_TEXTS = [
    "🔍 Search", "➕ Add item", "🔀 Move product", "📷 Add photo",
    "🗑 Delete item", "📦 Manage rows", "📊 Audit log", "⚠️ Low stock", "ℹ️ Help"
]

async def button_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """If user taps any main menu button mid-conversation, cancel and handle it."""
    context.user_data.clear()
    await keyboard_router(update, context)
    return ConversationHandler.END

def build_conv(entry_points, states, name):
    # Add every main button as a fallback so tapping them always works
    button_fallbacks = [
        MessageHandler(filters.Regex(f"^{re.escape(t)}$"), button_fallback)
        for t in BUTTON_TEXTS
    ]
    return ConversationHandler(
        entry_points=entry_points,
        states=states,
        fallbacks=[CommandHandler("cancel", cancel)] + button_fallbacks,
        name=name,
        per_user=True,
        per_chat=True,
        allow_reentry=True,
    )

app = Application.builder().token(TOKEN).build()

# Commands
app.add_handler(CommandHandler("start",      start))
app.add_handler(CommandHandler("newstore",   newstore))
app.add_handler(CommandHandler("liststores", liststores))
app.add_handler(CommandHandler("join",       join))
app.add_handler(CommandHandler("promote",    promote))
app.add_handler(CommandHandler("members",    members))
app.add_handler(CommandHandler("help",       help_cmd))

# Callback queries
app.add_handler(CallbackQueryHandler(view_callback,           pattern=r"^view:"))
app.add_handler(CallbackQueryHandler(action_callback,         pattern=r"^(sold|restock):"))
app.add_handler(CallbackQueryHandler(delete_confirm_callback, pattern=r"^(delrow|delall):"))
app.add_handler(CallbackQueryHandler(confirmdelrow_callback,  pattern=r"^confirmdelrow:"))
app.add_handler(CallbackQueryHandler(canceldelrow_callback,   pattern=r"^canceldelrow$"))

# Conversations
app.add_handler(build_conv(
    entry_points=[CommandHandler("search", search_start)],
    states={AWAIT_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, search_execute)]},
    name="search"
))
app.add_handler(build_conv(
    entry_points=[CommandHandler("add", add_start)],
    states={
        AWAIT_ADD_BARCODE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, add_barcode)],
        AWAIT_ADD_ROW:      [
            CallbackQueryHandler(add_row_callback, pattern=r"^row:"),
            MessageHandler(filters.TEXT & ~filters.COMMAND, add_row_text),
        ],
        AWAIT_ADD_POSITION: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_position)],
    },
    name="add"
))
app.add_handler(build_conv(
    entry_points=[CommandHandler("addphoto", photo_start)],
    states={
        AWAIT_PHOTO_BARCODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, photo_barcode)],
        AWAIT_PHOTO_IMG:     [MessageHandler(filters.PHOTO, photo_receive)],
    },
    name="photo"
))
app.add_handler(build_conv(
    entry_points=[CommandHandler("move", move_start)],
    states={
        AWAIT_MOVE_BARCODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, move_barcode)],
        AWAIT_MOVE_ROW:     [MessageHandler(filters.TEXT & ~filters.COMMAND, move_row)],
        AWAIT_MOVE_POS:     [MessageHandler(filters.TEXT & ~filters.COMMAND, move_position)],
    },
    name="move"
))
app.add_handler(build_conv(
    entry_points=[CommandHandler("delete", delete_start)],
    states={
        AWAIT_DELETE_BARCODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_barcode)],
    },
    name="delete"
))

# Row management conversation
app.add_handler(build_conv(
    entry_points=[CommandHandler("rows", rowmgr_start)],
    states={
        AWAIT_ROW_ACTION:      [CallbackQueryHandler(rowmgr_action,    pattern=r"^rowmgr:")],
        AWAIT_SHOWROW_NAME:    [MessageHandler(filters.TEXT & ~filters.COMMAND, showrow_name)],
        AWAIT_ADDROW_NAME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, addrow_name)],
        AWAIT_ADDROW_ITEMS:    [MessageHandler(filters.TEXT & ~filters.COMMAND, addrow_items)],
        AWAIT_APPENDROW_NAME:  [CallbackQueryHandler(pickrow_callback, pattern=r"^pickrow:")],
        AWAIT_APPENDROW_ITEMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, appendrow_items)],
        AWAIT_INSERTROW_NAME:  [CallbackQueryHandler(pickrow_callback, pattern=r"^pickrow:")],
        AWAIT_INSERTROW_POS:   [MessageHandler(filters.TEXT & ~filters.COMMAND, insertrow_pos)],
        AWAIT_INSERTROW_BARCODE:[MessageHandler(filters.TEXT & ~filters.COMMAND, insertrow_barcode)],
        AWAIT_RENAMEROW_OLD:   [CallbackQueryHandler(pickrow_callback, pattern=r"^pickrow:")],
        AWAIT_RENAMEROW_NEW:   [MessageHandler(filters.TEXT & ~filters.COMMAND, renamerow_new)],
        AWAIT_DELETEROW_NAME:  [
            CallbackQueryHandler(pickrow_callback,      pattern=r"^pickrow:"),
            CallbackQueryHandler(confirmdelrow_callback, pattern=r"^confirmdelrow:"),
            CallbackQueryHandler(canceldelrow_callback,  pattern=r"^canceldelrow$"),
        ],
    },
    name="rowmgr"
))

# Keyboard button router (must come LAST, catches all remaining TEXT)
app.add_handler(MessageHandler(
    filters.TEXT & ~filters.COMMAND,
    keyboard_router
))

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    import traceback
    err = ''.join(traceback.format_exception(type(context.error), context.error, context.error.__traceback__))
    print(f"ERROR: {err}")
    # Notify user so they don't sit confused
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong. Please tap the button again to retry."
            )
        except Exception:
            pass

app.add_error_handler(error_handler)

print("Puma Depot Bot running...")
app.run_polling()
