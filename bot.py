import os
import re
import sqlite3
import hashlib
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, ConversationHandler,
)

# ==========================
# CONFIG
# ==========================

TOKEN    = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
DB_PATH  = os.environ.get("DB_PATH", "inventory.db")
os.makedirs(os.path.dirname(DB_PATH) if os.path.dirname(DB_PATH) else ".", exist_ok=True)

# ==========================
# STATES  (one flat enum)
# ==========================

(
    # add item
    S_ADD_BARCODE, S_ADD_SHELF, S_ADD_BOX,
    # photo
    S_PHOTO_BARCODE, S_PHOTO_IMG,
    # search
    S_SEARCH,
    # move
    S_MOVE_BARCODE, S_MOVE_SHELF, S_MOVE_BOX,
    # delete
    S_DELETE_BARCODE,
    # row management
    S_ROW_MENU,
    S_ADDROW_NAME, S_ADDROW_ITEMS,
    S_APPENDROW_PICK, S_APPENDROW_ITEMS,
    S_INSERTROW_PICK, S_INSERTROW_POS, S_INSERTROW_BARCODE,
    S_SHOWROW_NAME,
    S_RENAMEROW_PICK, S_RENAMEROW_NEW,
    S_DELETEROW_PICK,
) = range(22)

# ==========================
# DATABASE
# ==========================

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
conn.executescript("""
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS stores (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT NOT NULL UNIQUE,
    join_code TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS users (
    tg_id    INTEGER NOT NULL,
    store_id INTEGER NOT NULL,
    role     TEXT NOT NULL DEFAULT 'worker',
    name     TEXT,
    PRIMARY KEY (tg_id, store_id)
);
CREATE TABLE IF NOT EXISTS inventory (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id INTEGER NOT NULL,
    barcode  TEXT NOT NULL,
    shelf    TEXT NOT NULL,
    box      INTEGER NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    UNIQUE(store_id, barcode, shelf, box)
);
CREATE TABLE IF NOT EXISTS photos (
    store_id INTEGER NOT NULL,
    barcode  TEXT NOT NULL,
    file_id  TEXT NOT NULL,
    PRIMARY KEY (store_id, barcode)
);
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id   INTEGER NOT NULL,
    tg_id      INTEGER NOT NULL,
    action     TEXT NOT NULL,
    detail     TEXT,
    ts         TEXT DEFAULT (datetime('now'))
);
""")
conn.commit()
cur = conn.cursor()

# ==========================
# HELPERS
# ==========================

def make_code(name): return hashlib.sha1(name.encode()).hexdigest()[:8].upper()

def get_user_store(tg_id):
    r = cur.execute("SELECT store_id, role FROM users WHERE tg_id=? LIMIT 1", (tg_id,)).fetchone()
    return (r["store_id"], r["role"]) if r else (None, None)

def get_store(store_id):
    return cur.execute("SELECT * FROM stores WHERE id=?", (store_id,)).fetchone()

def get_store_by_code(code):
    return cur.execute("SELECT * FROM stores WHERE join_code=?", (code.upper().strip(),)).fetchone()

def get_photo(store_id, barcode):
    r = cur.execute("SELECT file_id FROM photos WHERE store_id=? AND barcode=?", (store_id, barcode)).fetchone()
    return r["file_id"] if r else None

def log(store_id, tg_id, action, detail=""):
    cur.execute("INSERT INTO audit_log(store_id,tg_id,action,detail) VALUES(?,?,?,?)",
                (store_id, tg_id, action, detail))
    conn.commit()

def is_admin(tg_id): return tg_id == ADMIN_ID

def menu(role):
    base = [
        [KeyboardButton("🔍 Search"),       KeyboardButton("➕ Add item")],
        [KeyboardButton("🔀 Move"),          KeyboardButton("📷 Add photo")],
        [KeyboardButton("🗑 Delete"),        KeyboardButton("📦 Rows")],
        [KeyboardButton("ℹ️ Help")],
    ]
    if role in ("manager", "admin"):
        base.append([KeyboardButton("📊 Audit"), KeyboardButton("⚠️ Low stock")])
    return ReplyKeyboardMarkup(base, resize_keyboard=True)

async def get_ctx(update, context):
    """Return (store_id, role) and set in user_data. Send error if not in a store."""
    tg_id = update.effective_user.id
    store_id = context.user_data.get("store_id")
    role     = context.user_data.get("role")
    if not store_id:
        store_id, role = get_user_store(tg_id)
        if not store_id:
            await update.effective_message.reply_text(
                "❌ You are not in any store. Use /join CODE first."
            )
            return None, None
        context.user_data["store_id"] = store_id
        context.user_data["role"]     = role
    return store_id, role

# ==========================
# CANCEL / FALLBACK
# ==========================

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = get_user_store(update.effective_user.id)
    context.user_data.clear()
    if store_id:
        context.user_data["store_id"] = store_id
        context.user_data["role"]     = role
    await update.message.reply_text("↩️ Cancelled.", reply_markup=menu(role or "worker"))
    return ConversationHandler.END

# ==========================
# /start  /help
# ==========================

HELP = (
    "👟 *Puma Depot Bot*\n\n"
    "*🔍 Search* — find by barcode (partial OK)\n"
    "*➕ Add item* — add barcode → shelf → box\n"
    "*🔀 Move* — change item location\n"
    "*📷 Add photo* — attach photo to barcode\n"
    "*🗑 Delete* — remove an item\n"
    "*📦 Rows* — manage shelves/rows\n\n"
    "After searching, tap result buttons:\n"
    "➖ Sold  ➕ Restock  🔀 Move  🗑 Delete\n\n"
    "Manager: 📊 Audit · ⚠️ Low stock · /promote · /members\n"
    "Admin: /newstore · /liststores"
)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    store_id, role = get_user_store(tg_id)
    if not store_id:
        if is_admin(tg_id):
            await update.message.reply_text("👋 Admin! Create a store: `/newstore NAME`", parse_mode="Markdown")
        else:
            await update.message.reply_text("👋 Welcome! Ask your manager for a join code, then: `/join CODE`", parse_mode="Markdown")
        return
    store = get_store(store_id)
    await update.message.reply_text(
        f"👋 *{store['name']}* | role: *{role}*\n\nUse the buttons below.",
        parse_mode="Markdown", reply_markup=menu(role)
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _, role = get_user_store(update.effective_user.id)
    await update.message.reply_text(HELP, parse_mode="Markdown", reply_markup=menu(role or "worker"))

# ==========================
# ADMIN COMMANDS
# ==========================

async def cmd_newstore(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: `/newstore NAME`", parse_mode="Markdown")
        return
    name = " ".join(context.args)
    code = make_code(name)
    try:
        cur.execute("INSERT INTO stores(name,join_code) VALUES(?,?)", (name, code))
        conn.commit()
        sid = cur.lastrowid
        cur.execute("INSERT OR IGNORE INTO users(tg_id,store_id,role,name) VALUES(?,?,?,?)",
                    (ADMIN_ID, sid, "admin", "Admin"))
        conn.commit()
        await update.message.reply_text(
            f"✅ Store *{name}* created!\nJoin code: `{code}`\nShare: `/join {code}`",
            parse_mode="Markdown"
        )
    except sqlite3.IntegrityError:
        await update.message.reply_text(f"⚠️ Store *{name}* already exists.", parse_mode="Markdown")

async def cmd_liststores(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ Admin only.")
        return
    rows = cur.execute("SELECT id,name,join_code FROM stores ORDER BY name").fetchall()
    if not rows:
        await update.message.reply_text("No stores yet.")
        return
    text = "🏪 *Stores:*\n\n"
    for r in rows:
        c = cur.execute("SELECT COUNT(*) FROM users WHERE store_id=?", (r["id"],)).fetchone()[0]
        text += f"• *{r['name']}* — `{r['join_code']}` — {c} user(s)\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: `/join CODE`", parse_mode="Markdown")
        return
    store = get_store_by_code(context.args[0])
    if not store:
        await update.message.reply_text("❌ Invalid code.")
        return
    tg_id = update.effective_user.id
    name  = update.effective_user.full_name
    existing = cur.execute("SELECT role FROM users WHERE tg_id=? AND store_id=?",
                           (tg_id, store["id"])).fetchone()
    if existing:
        await update.message.reply_text(f"⚠️ Already in *{store['name']}* as *{existing['role']}*.", parse_mode="Markdown")
        return
    cur.execute("INSERT INTO users(tg_id,store_id,role,name) VALUES(?,?,?,?)",
                (tg_id, store["id"], "worker", name))
    conn.commit()
    log(store["id"], tg_id, "join", name)
    await update.message.reply_text(
        f"✅ Joined *{store['name']}* as worker!\nTap a button to get started.",
        parse_mode="Markdown", reply_markup=menu("worker")
    )
    managers = cur.execute("SELECT tg_id FROM users WHERE store_id=? AND role IN ('manager','admin')",
                           (store["id"],)).fetchall()
    for m in managers:
        try:
            await context.bot.send_message(m["tg_id"],
                f"👤 *{name}* joined *{store['name']}*.\n`/promote {tg_id} manager` to promote.",
                parse_mode="Markdown")
        except Exception: pass

async def cmd_promote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    store_id, role = get_user_store(tg_id)
    if role not in ("manager","admin") and not is_admin(tg_id):
        await update.message.reply_text("❌ Manager/admin only.")
        return
    if len(context.args) != 2:
        await update.message.reply_text("Usage: `/promote USER_ID ROLE`", parse_mode="Markdown")
        return
    try: target = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ USER_ID must be a number.")
        return
    new_role = context.args[1].lower()
    if new_role not in ("worker","manager"):
        await update.message.reply_text("❌ Role must be worker or manager.")
        return
    if not cur.execute("SELECT 1 FROM users WHERE tg_id=? AND store_id=?", (target, store_id)).fetchone():
        await update.message.reply_text("❌ User not in your store.")
        return
    cur.execute("UPDATE users SET role=? WHERE tg_id=? AND store_id=?", (new_role, target, store_id))
    conn.commit()
    await update.message.reply_text(f"✅ User `{target}` is now *{new_role}*.", parse_mode="Markdown")
    try:
        await context.bot.send_message(target, f"🎉 Your role is now *{new_role}*!", parse_mode="Markdown")
    except Exception: pass

async def cmd_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = get_user_store(update.effective_user.id)
    if role not in ("manager","admin"):
        await update.message.reply_text("❌ Manager/admin only.")
        return
    rows = cur.execute("SELECT tg_id,name,role FROM users WHERE store_id=? ORDER BY role,name",
                       (store_id,)).fetchall()
    store = get_store(store_id)
    text = f"👥 *{store['name']} members:*\n\n"
    icons = {"admin":"👑","manager":"🔑","worker":"👷"}
    for r in rows:
        text += f"{icons.get(r['role'],'?')} {r['name'] or '?'} — `{r['tg_id']}` — *{r['role']}*\n"
    await update.message.reply_text(text, parse_mode="Markdown")

# ==========================
# SEARCH
# ==========================

async def search_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = await get_ctx(update, context)
    if not store_id: return ConversationHandler.END
    await update.message.reply_text("🔍 Enter barcode (or partial, e.g. `778`):", parse_mode="Markdown")
    return S_SEARCH

async def search_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = await get_ctx(update, context)
    if not store_id: return ConversationHandler.END
    q = update.message.text.strip()
    rows = cur.execute(
        "SELECT barcode,shelf,box,quantity FROM inventory WHERE store_id=? AND barcode LIKE ? ORDER BY barcode,shelf,box",
        (store_id, f"%{q}%")
    ).fetchall()
    if not rows:
        await update.message.reply_text(f"❌ No results for `{q}`.", parse_mode="Markdown", reply_markup=menu(role))
        return ConversationHandler.END
    # unique barcodes
    seen = {}
    for r in rows:
        seen.setdefault(r["barcode"], []).append(r)
    if len(seen) == 1:
        barcode = list(seen.keys())[0]
        await send_card(update.message, store_id, barcode, seen[barcode])
    else:
        btns = [[InlineKeyboardButton(b, callback_data=f"view:{store_id}:{b}")] for b in seen]
        await update.message.reply_text(
            f"🔍 *{len(seen)}* matches for `{q}` — tap one:",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns)
        )
    return ConversationHandler.END

async def send_card(msg, store_id, barcode, locs):
    loc_text = "\n".join(f"📍 *{r['shelf']}* — box *{r['box']}*  (qty: {r['quantity']})" for r in locs)
    caption  = f"🔍 *{barcode}*\n\n{loc_text}"
    btns = InlineKeyboardMarkup([
        [InlineKeyboardButton("➖ Sold",      callback_data=f"sold:{store_id}:{barcode}"),
         InlineKeyboardButton("➕ Restock",   callback_data=f"restock:{store_id}:{barcode}")],
        [InlineKeyboardButton("🔀 Move",      callback_data=f"move:{store_id}:{barcode}"),
         InlineKeyboardButton("🗑 Delete",    callback_data=f"delitem:{store_id}:{barcode}")],
    ])
    photo = get_photo(store_id, barcode)
    if photo:
        await msg.reply_photo(photo=photo, caption=caption, parse_mode="Markdown", reply_markup=btns)
    else:
        await msg.reply_text(caption + "\n\n_No photo yet — tap 📷 Add photo_",
                             parse_mode="Markdown", reply_markup=btns)

async def cb_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    _, store_id, barcode = q.data.split(":", 2)
    store_id = int(store_id)
    locs = cur.execute(
        "SELECT shelf,box,quantity FROM inventory WHERE store_id=? AND barcode=?",
        (store_id, barcode)
    ).fetchall()
    if not locs:
        await q.message.reply_text("❌ Not found.")
        return
    await send_card(q.message, store_id, barcode, locs)

# ==========================
# INLINE ACTION CALLBACKS
# ==========================

async def cb_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    parts    = q.data.split(":")
    action   = parts[0]
    store_id = int(parts[1])
    barcode  = parts[2]
    tg_id    = q.from_user.id
    _, role  = get_user_store(tg_id)

    if action == "sold":
        cur.execute("UPDATE inventory SET quantity=MAX(0,quantity-1) WHERE store_id=? AND barcode=?",
                    (store_id, barcode))
        conn.commit()
        log(store_id, tg_id, "sold", barcode)
        qty = cur.execute("SELECT MIN(quantity) FROM inventory WHERE store_id=? AND barcode=?",
                          (store_id, barcode)).fetchone()[0]
        await q.message.reply_text(f"✅ Sold 1× `{barcode}`. Remaining: *{qty}*" +
                                   ("\n⚠️ Low stock!" if qty<=2 else ""), parse_mode="Markdown")
        if qty<=2: await notify_low(context, store_id, barcode, qty)

    elif action == "restock":
        cur.execute("UPDATE inventory SET quantity=quantity+1 WHERE store_id=? AND barcode=?",
                    (store_id, barcode))
        conn.commit()
        log(store_id, tg_id, "restock", barcode)
        qty = cur.execute("SELECT MIN(quantity) FROM inventory WHERE store_id=? AND barcode=?",
                          (store_id, barcode)).fetchone()[0]
        await q.message.reply_text(f"✅ Restocked `{barcode}`. Now: *{qty}*", parse_mode="Markdown")

    elif action == "delitem":
        cur.execute("DELETE FROM inventory WHERE store_id=? AND barcode=?", (store_id, barcode))
        cur.execute("DELETE FROM photos WHERE store_id=? AND barcode=?", (store_id, barcode))
        conn.commit()
        log(store_id, tg_id, "delete", barcode)
        await q.message.reply_text(f"🗑 Deleted `{barcode}`.", parse_mode="Markdown",
                                   reply_markup=menu(role or "worker"))

    elif action == "move":
        context.user_data["store_id"]     = store_id
        context.user_data["role"]         = role or "worker"
        context.user_data["move_barcode"] = barcode
        await q.message.reply_text(f"🔀 Moving `{barcode}`.\n\nNew shelf name:", parse_mode="Markdown")
        # We can't return a state from a callback — use user_data flag instead
        context.user_data["awaiting_move_shelf"] = True

async def notify_low(context, store_id, barcode, qty):
    store = get_store(store_id)
    for m in cur.execute("SELECT tg_id FROM users WHERE store_id=? AND role IN ('manager','admin')",
                         (store_id,)).fetchall():
        try:
            await context.bot.send_message(m["tg_id"],
                f"⚠️ *Low stock* — {store['name']}\n`{barcode}` has only *{qty}* left.",
                parse_mode="Markdown")
        except Exception: pass

# ==========================
# ADD ITEM  (3 steps)
# ==========================

async def add_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = await get_ctx(update, context)
    if not store_id: return ConversationHandler.END
    context.user_data.pop("add_barcode", None)
    context.user_data.pop("add_shelf",   None)
    await update.message.reply_text("➕ *Step 1 of 3*\nEnter the barcode:", parse_mode="Markdown")
    return S_ADD_BARCODE

async def add_got_barcode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = await get_ctx(update, context)
    if not store_id: return ConversationHandler.END
    context.user_data["add_barcode"] = update.message.text.strip()
    await update.message.reply_text("📦 *Step 2 of 3*\nShelf / row name:", parse_mode="Markdown")
    return S_ADD_SHELF

async def add_got_shelf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = await get_ctx(update, context)
    if not store_id: return ConversationHandler.END
    context.user_data["add_shelf"] = update.message.text.strip()
    await update.message.reply_text("🔢 *Step 3 of 3*\nBox number:", parse_mode="Markdown")
    return S_ADD_BOX

async def add_got_box(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = await get_ctx(update, context)
    if not store_id: return ConversationHandler.END
    try:
        box = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Box number must be a number. Try again:")
        return S_ADD_BOX
    barcode = context.user_data.get("add_barcode","")
    shelf   = context.user_data.get("add_shelf","")
    if not barcode or not shelf:
        await update.message.reply_text("❌ Something went wrong. Please tap ➕ Add item and start again.")
        return ConversationHandler.END
    try:
        cur.execute("INSERT INTO inventory(store_id,barcode,shelf,box) VALUES(?,?,?,?)",
                    (store_id, barcode, shelf, box))
        conn.commit()
        log(store_id, update.effective_user.id, "add", f"{barcode} @ {shelf} box {box}")
        await update.message.reply_text(
            f"✅ *Saved!*\n\n📦 `{barcode}`\n📍 {shelf}\n🔢 Box {box}",
            parse_mode="Markdown", reply_markup=menu(role)
        )
    except sqlite3.IntegrityError:
        await update.message.reply_text(
            f"⚠️ `{barcode}` already exists at *{shelf}* box {box}.",
            parse_mode="Markdown", reply_markup=menu(role)
        )
    context.user_data.pop("add_barcode", None)
    context.user_data.pop("add_shelf",   None)
    return ConversationHandler.END

# ==========================
# ADD PHOTO  (2 steps)
# ==========================

async def photo_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = await get_ctx(update, context)
    if not store_id: return ConversationHandler.END
    await update.message.reply_text("📷 Enter the barcode to attach a photo to:")
    return S_PHOTO_BARCODE

async def photo_got_barcode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = await get_ctx(update, context)
    if not store_id: return ConversationHandler.END
    barcode = update.message.text.strip()
    if not cur.execute("SELECT 1 FROM inventory WHERE store_id=? AND barcode=?",
                       (store_id, barcode)).fetchone():
        await update.message.reply_text(f"❌ `{barcode}` not in inventory.", parse_mode="Markdown",
                                        reply_markup=menu(role))
        return ConversationHandler.END
    context.user_data["photo_barcode"] = barcode
    existing = get_photo(store_id, barcode)
    await update.message.reply_text(
        f"📷 Now send the photo for `{barcode}`" + (" _(replaces existing)_" if existing else "") + ":",
        parse_mode="Markdown"
    )
    return S_PHOTO_IMG

async def photo_got_img(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = await get_ctx(update, context)
    if not store_id: return ConversationHandler.END
    barcode = context.user_data.get("photo_barcode","")
    file_id = update.message.photo[-1].file_id
    cur.execute("INSERT INTO photos(store_id,barcode,file_id) VALUES(?,?,?) "
                "ON CONFLICT(store_id,barcode) DO UPDATE SET file_id=excluded.file_id",
                (store_id, barcode, file_id))
    conn.commit()
    log(store_id, update.effective_user.id, "photo", barcode)
    await update.message.reply_text(f"✅ Photo saved for `{barcode}`!", parse_mode="Markdown",
                                    reply_markup=menu(role))
    return ConversationHandler.END

# ==========================
# MOVE  (2 steps via conversation)
# ==========================

async def move_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = await get_ctx(update, context)
    if not store_id: return ConversationHandler.END
    await update.message.reply_text("🔀 Enter the barcode to move:")
    return S_MOVE_BARCODE

async def move_got_barcode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = await get_ctx(update, context)
    if not store_id: return ConversationHandler.END
    barcode = update.message.text.strip()
    locs = cur.execute("SELECT shelf,box FROM inventory WHERE store_id=? AND barcode=?",
                       (store_id, barcode)).fetchall()
    if not locs:
        await update.message.reply_text(f"❌ `{barcode}` not found.", parse_mode="Markdown",
                                        reply_markup=menu(role))
        return ConversationHandler.END
    context.user_data["move_barcode"] = barcode
    loc_text = ", ".join(f"{r['shelf']} box {r['box']}" for r in locs)
    await update.message.reply_text(f"📍 Currently: {loc_text}\n\nNew shelf name:")
    return S_MOVE_SHELF

async def move_got_shelf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = await get_ctx(update, context)
    if not store_id: return ConversationHandler.END
    context.user_data["move_shelf"] = update.message.text.strip()
    await update.message.reply_text("🔢 New box number:")
    return S_MOVE_BOX

async def move_got_box(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = await get_ctx(update, context)
    if not store_id: return ConversationHandler.END
    try:
        box = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Must be a number. Try again:")
        return S_MOVE_BOX
    barcode = context.user_data.get("move_barcode","")
    shelf   = context.user_data.get("move_shelf","")
    cur.execute("UPDATE inventory SET shelf=?,box=? WHERE store_id=? AND barcode=?",
                (shelf, box, store_id, barcode))
    conn.commit()
    log(store_id, update.effective_user.id, "move", f"{barcode} -> {shelf} box {box}")
    await update.message.reply_text(f"✅ Moved `{barcode}` to *{shelf}*, box {box}.",
                                    parse_mode="Markdown", reply_markup=menu(role))
    return ConversationHandler.END

# ==========================
# DELETE  (1 step)
# ==========================

async def delete_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = await get_ctx(update, context)
    if not store_id: return ConversationHandler.END
    await update.message.reply_text("🗑 Enter the barcode to delete:")
    return S_DELETE_BARCODE

async def delete_got_barcode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = await get_ctx(update, context)
    if not store_id: return ConversationHandler.END
    barcode = update.message.text.strip()
    locs = cur.execute("SELECT shelf,box FROM inventory WHERE store_id=? AND barcode=?",
                       (store_id, barcode)).fetchall()
    if not locs:
        await update.message.reply_text(f"❌ `{barcode}` not found.", parse_mode="Markdown",
                                        reply_markup=menu(role))
        return ConversationHandler.END
    btns = [
        [InlineKeyboardButton(f"{r['shelf']} box {r['box']}",
                              callback_data=f"delone:{store_id}:{barcode}:{r['shelf']}:{r['box']}")]
        for r in locs
    ]
    btns.append([InlineKeyboardButton("🗑 Delete ALL", callback_data=f"delall:{store_id}:{barcode}")])
    await update.message.reply_text(f"Which entry to delete for `{barcode}`?",
                                    parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))
    return ConversationHandler.END

async def cb_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    parts    = q.data.split(":")
    action   = parts[0]
    store_id = int(parts[1])
    barcode  = parts[2]
    tg_id    = q.from_user.id
    _, role  = get_user_store(tg_id)
    if action == "delone":
        shelf = parts[3]; box = int(parts[4])
        cur.execute("DELETE FROM inventory WHERE store_id=? AND barcode=? AND shelf=? AND box=?",
                    (store_id, barcode, shelf, box))
        conn.commit()
        log(store_id, tg_id, "delone", f"{barcode} @ {shelf} box {box}")
        await q.message.reply_text(f"🗑 Deleted `{barcode}` from *{shelf}* box {box}.",
                                   parse_mode="Markdown", reply_markup=menu(role or "worker"))
    elif action == "delall":
        cur.execute("DELETE FROM inventory WHERE store_id=? AND barcode=?", (store_id, barcode))
        cur.execute("DELETE FROM photos WHERE store_id=? AND barcode=?",    (store_id, barcode))
        conn.commit()
        log(store_id, tg_id, "delall", barcode)
        await q.message.reply_text(f"🗑 Deleted all entries for `{barcode}`.",
                                   parse_mode="Markdown", reply_markup=menu(role or "worker"))

# ==========================
# ROW MANAGEMENT
# ==========================

ROW_MENU_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("📋 Show row",          callback_data="rm:show")],
    [InlineKeyboardButton("➕ Add new row",        callback_data="rm:add")],
    [InlineKeyboardButton("📎 Append to row",     callback_data="rm:append")],
    [InlineKeyboardButton("📌 Insert at position",callback_data="rm:insert")],
    [InlineKeyboardButton("✏️ Rename row",        callback_data="rm:rename")],
    [InlineKeyboardButton("🗑 Delete row",         callback_data="rm:delete")],
])

async def rows_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = await get_ctx(update, context)
    if not store_id: return ConversationHandler.END
    await update.message.reply_text("📦 *Row management:*", parse_mode="Markdown", reply_markup=ROW_MENU_KB)
    return S_ROW_MENU

def shelf_buttons(store_id, cb_prefix):
    shelves = cur.execute("SELECT DISTINCT shelf FROM inventory WHERE store_id=? ORDER BY shelf",
                          (store_id,)).fetchall()
    return InlineKeyboardMarkup([[InlineKeyboardButton(r["shelf"], callback_data=f"{cb_prefix}:{r['shelf']}")] for r in shelves])

async def rm_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    store_id = context.user_data.get("store_id")
    if not store_id:
        store_id, role = get_user_store(q.from_user.id)
        context.user_data["store_id"] = store_id
        context.user_data["role"]     = role
    action = q.data.split(":")[1]
    context.user_data["rm_action"] = action

    if action == "show":
        await q.message.reply_text("📋 Type the shelf/row name to show:")
        return S_SHOWROW_NAME

    elif action == "add":
        await q.message.reply_text(
            "➕ *New row*\nType the shelf name:", parse_mode="Markdown")
        return S_ADDROW_NAME

    elif action == "append":
        kb = shelf_buttons(store_id, "pick_append")
        if not kb.inline_keyboard:
            await q.message.reply_text("❌ No shelves yet. Add items first.")
            return ConversationHandler.END
        await q.message.reply_text("📎 Pick a shelf to append to:", reply_markup=kb)
        return S_APPENDROW_PICK

    elif action == "insert":
        kb = shelf_buttons(store_id, "pick_insert")
        if not kb.inline_keyboard:
            await q.message.reply_text("❌ No shelves yet.")
            return ConversationHandler.END
        await q.message.reply_text("📌 Pick a shelf to insert into:", reply_markup=kb)
        return S_INSERTROW_PICK

    elif action == "rename":
        kb = shelf_buttons(store_id, "pick_rename")
        if not kb.inline_keyboard:
            await q.message.reply_text("❌ No shelves yet.")
            return ConversationHandler.END
        await q.message.reply_text("✏️ Pick a shelf to rename:", reply_markup=kb)
        return S_RENAMEROW_PICK

    elif action == "delete":
        kb = shelf_buttons(store_id, "pick_delete")
        if not kb.inline_keyboard:
            await q.message.reply_text("❌ No shelves yet.")
            return ConversationHandler.END
        await q.message.reply_text("🗑 Pick a shelf to delete:", reply_markup=kb)
        return S_DELETEROW_PICK

    return ConversationHandler.END

# show row
async def rm_show_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = await get_ctx(update, context)
    if not store_id: return ConversationHandler.END
    shelf = update.message.text.strip()
    rows  = cur.execute(
        "SELECT box, GROUP_CONCAT(barcode,', ') as barcodes FROM inventory "
        "WHERE store_id=? AND shelf=? GROUP BY box ORDER BY box",
        (store_id, shelf)
    ).fetchall()
    if not rows:
        await update.message.reply_text(f"❌ Shelf *{shelf}* not found.", parse_mode="Markdown",
                                        reply_markup=menu(role))
        return ConversationHandler.END
    text = f"📋 *{shelf}*\n\n"
    for r in rows:
        text += f"`Box {r['box']}.` {r['barcodes']}\n"
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=menu(role))
    return ConversationHandler.END

# add new row
async def rm_addrow_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["new_shelf"] = update.message.text.strip()
    await update.message.reply_text(
        "Now send all barcodes separated by spaces.\n"
        "Each word = next box. Comma = same box.\n"
        "Example: `111 222,223 333`", parse_mode="Markdown"
    )
    return S_ADDROW_ITEMS

async def rm_addrow_items(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = await get_ctx(update, context)
    if not store_id: return ConversationHandler.END
    shelf  = context.user_data.get("new_shelf","")
    groups = update.message.text.strip().split()
    count  = 0
    for box_num, group in enumerate(groups, start=1):
        for barcode in [b.strip() for b in group.split(",") if b.strip()]:
            try:
                cur.execute("INSERT INTO inventory(store_id,barcode,shelf,box) VALUES(?,?,?,?)",
                            (store_id, barcode, shelf, box_num))
                count += 1
            except sqlite3.IntegrityError: pass
    conn.commit()
    log(store_id, update.effective_user.id, "addrow", f"{shelf} +{count}")
    await update.message.reply_text(f"✅ Added *{count}* barcodes to *{shelf}*.",
                                    parse_mode="Markdown", reply_markup=menu(role))
    return ConversationHandler.END

# append row — pick via inline button
async def rm_append_pick_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    shelf = q.data.split(":",1)[1]
    context.user_data["target_shelf"] = shelf
    max_box = cur.execute(
        "SELECT COALESCE(MAX(box),0) FROM inventory WHERE store_id=? AND shelf=?",
        (context.user_data["store_id"], shelf)
    ).fetchone()[0]
    await q.message.reply_text(
        f"📎 Appending to *{shelf}* (current max box: {max_box}).\n"
        "Send barcodes: each word = next box, comma = same box.\n"
        "Example: `111 222,223`", parse_mode="Markdown"
    )
    return S_APPENDROW_ITEMS

async def rm_append_items(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = await get_ctx(update, context)
    if not store_id: return ConversationHandler.END
    shelf   = context.user_data.get("target_shelf","")
    next_box = cur.execute(
        "SELECT COALESCE(MAX(box),0)+1 FROM inventory WHERE store_id=? AND shelf=?",
        (store_id, shelf)
    ).fetchone()[0]
    groups = update.message.text.strip().split()
    count  = 0
    for group in groups:
        for barcode in [b.strip() for b in group.split(",") if b.strip()]:
            try:
                cur.execute("INSERT INTO inventory(store_id,barcode,shelf,box) VALUES(?,?,?,?)",
                            (store_id, barcode, shelf, next_box))
                count += 1
            except sqlite3.IntegrityError: pass
        next_box += 1
    conn.commit()
    log(store_id, update.effective_user.id, "append", f"{shelf} +{count}")
    await update.message.reply_text(f"✅ Appended *{count}* barcodes to *{shelf}*.",
                                    parse_mode="Markdown", reply_markup=menu(role))
    return ConversationHandler.END

# insert at position
async def rm_insert_pick_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    shelf = q.data.split(":",1)[1]
    context.user_data["target_shelf"] = shelf
    rows = cur.execute(
        "SELECT box, GROUP_CONCAT(barcode,', ') as bc FROM inventory "
        "WHERE store_id=? AND shelf=? GROUP BY box ORDER BY box",
        (context.user_data["store_id"], shelf)
    ).fetchall()
    preview = "\n".join(f"  Box {r['box']}: {r['bc']}" for r in rows) if rows else "  (empty)"
    await q.message.reply_text(
        f"📌 *{shelf}*\n{preview}\n\nAt which box number to insert? (others shift down)",
        parse_mode="Markdown"
    )
    return S_INSERTROW_POS

async def rm_insert_pos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        pos = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Must be a number. Try again:")
        return S_INSERTROW_POS
    context.user_data["insert_pos"] = pos
    await update.message.reply_text(
        f"Enter barcode(s) for box {pos} (comma-separate for multiple):")
    return S_INSERTROW_BARCODE

async def rm_insert_barcode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = await get_ctx(update, context)
    if not store_id: return ConversationHandler.END
    shelf    = context.user_data.get("target_shelf","")
    pos      = context.user_data.get("insert_pos", 1)
    barcodes = [b.strip() for b in update.message.text.split(",") if b.strip()]
    cur.execute("UPDATE inventory SET box=box+1 WHERE store_id=? AND shelf=? AND box>=?",
                (store_id, shelf, pos))
    count = 0
    for barcode in barcodes:
        try:
            cur.execute("INSERT INTO inventory(store_id,barcode,shelf,box) VALUES(?,?,?,?)",
                        (store_id, barcode, shelf, pos))
            count += 1
        except sqlite3.IntegrityError: pass
    conn.commit()
    log(store_id, update.effective_user.id, "insert", f"{shelf} box {pos} +{count}")
    await update.message.reply_text(
        f"✅ Inserted *{count}* barcode(s) at box {pos} in *{shelf}*. Others shifted down.",
        parse_mode="Markdown", reply_markup=menu(role)
    )
    return ConversationHandler.END

# rename row
async def rm_rename_pick_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    shelf = q.data.split(":",1)[1]
    context.user_data["target_shelf"] = shelf
    await q.message.reply_text(f"✏️ New name for *{shelf}*:", parse_mode="Markdown")
    return S_RENAMEROW_NEW

async def rm_rename_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = await get_ctx(update, context)
    if not store_id: return ConversationHandler.END
    old = context.user_data.get("target_shelf","")
    new = update.message.text.strip()
    cur.execute("UPDATE inventory SET shelf=? WHERE store_id=? AND shelf=?", (new, store_id, old))
    conn.commit()
    log(store_id, update.effective_user.id, "rename", f"{old} -> {new}")
    await update.message.reply_text(f"✅ Renamed *{old}* → *{new}*.",
                                    parse_mode="Markdown", reply_markup=menu(role))
    return ConversationHandler.END

# delete row
async def rm_delete_pick_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    shelf    = q.data.split(":",1)[1]
    store_id = context.user_data.get("store_id")
    if not store_id:
        store_id, _ = get_user_store(q.from_user.id)
    count = cur.execute("SELECT COUNT(*) FROM inventory WHERE store_id=? AND shelf=?",
                        (store_id, shelf)).fetchone()[0]
    btns = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, delete", callback_data=f"confirmdelshelf:{store_id}:{shelf}"),
        InlineKeyboardButton("❌ Cancel",      callback_data="canceldelshelf"),
    ]])
    await q.message.reply_text(
        f"⚠️ Delete shelf *{shelf}* and all *{count}* item(s)?",
        parse_mode="Markdown", reply_markup=btns
    )
    return S_DELETEROW_PICK

async def cb_confirmdelshelf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    _, store_id, shelf = q.data.split(":", 2)
    store_id = int(store_id)
    tg_id    = q.from_user.id
    _, role  = get_user_store(tg_id)
    cur.execute("DELETE FROM inventory WHERE store_id=? AND shelf=?", (store_id, shelf))
    conn.commit()
    log(store_id, tg_id, "delshelf", shelf)
    await q.message.reply_text(f"🗑 Shelf *{shelf}* deleted.", parse_mode="Markdown",
                               reply_markup=menu(role or "worker"))
    return ConversationHandler.END

async def cb_canceldelshelf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    _, role = get_user_store(q.from_user.id)
    await q.message.reply_text("↩️ Cancelled.", reply_markup=menu(role or "worker"))
    return ConversationHandler.END

# ==========================
# MANAGER COMMANDS
# ==========================

async def cmd_audit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = get_user_store(update.effective_user.id)
    if role not in ("manager","admin"):
        await update.message.reply_text("❌ Manager only.")
        return
    rows = cur.execute(
        "SELECT ts,action,detail,tg_id FROM audit_log WHERE store_id=? ORDER BY id DESC LIMIT 20",
        (store_id,)
    ).fetchall()
    if not rows:
        await update.message.reply_text("No activity yet.")
        return
    text = "📋 *Last 20 actions:*\n\n"
    for r in rows:
        text += f"`{r['ts'][:16]}` {r['action']} — {r['detail']} (by `{r['tg_id']}`)\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_lowstock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    store_id, role = get_user_store(update.effective_user.id)
    if role not in ("manager","admin"):
        await update.message.reply_text("❌ Manager only.")
        return
    rows = cur.execute(
        "SELECT barcode,shelf,box,quantity FROM inventory WHERE store_id=? AND quantity<=2 ORDER BY quantity,barcode",
        (store_id,)
    ).fetchall()
    if not rows:
        await update.message.reply_text("✅ No low stock items.")
        return
    text = "⚠️ *Low stock:*\n\n"
    for r in rows:
        text += f"• `{r['barcode']}` — {r['shelf']} box {r['box']} — *{r['quantity']}* left\n"
    await update.message.reply_text(text, parse_mode="Markdown")

# ==========================
# ERROR HANDLER
# ==========================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    import traceback
    logger.error("Exception: %s", "".join(traceback.format_exception(
        type(context.error), context.error, context.error.__traceback__)))
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong. Please tap the button again.")
        except Exception: pass

# ==========================
# KEYBOARD BUTTON ROUTER
# (only for buttons that are NOT conversation entry points)
# ==========================

async def keyboard_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📊 Audit":     return await cmd_audit(update, context)
    if text == "⚠️ Low stock": return await cmd_lowstock(update, context)
    if text == "ℹ️ Help":      return await cmd_help(update, context)

# ==========================
# BUILD APP
# ==========================

ENTRY_CANCEL = [CommandHandler("cancel", cancel)]

def make_conv(name, entry_text, entry_fn, states):
    return ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(f"^{re.escape(entry_text)}$"), entry_fn),
        ],
        states=states,
        fallbacks=ENTRY_CANCEL + [
            MessageHandler(filters.Regex(r"^(🔍 Search|➕ Add item|🔀 Move|📷 Add photo|🗑 Delete|📦 Rows|ℹ️ Help|📊 Audit|⚠️ Low stock)$"), cancel)
        ],
        name=name,
        per_user=True,
        per_chat=False,
        allow_reentry=True,
    )

app = Application.builder().token(TOKEN).build()

# Plain commands
app.add_handler(CommandHandler("start",      cmd_start))
app.add_handler(CommandHandler("help",       cmd_help))
app.add_handler(CommandHandler("newstore",   cmd_newstore))
app.add_handler(CommandHandler("liststores", cmd_liststores))
app.add_handler(CommandHandler("join",       cmd_join))
app.add_handler(CommandHandler("promote",    cmd_promote))
app.add_handler(CommandHandler("members",    cmd_members))

# Inline callbacks
app.add_handler(CallbackQueryHandler(cb_view,             pattern=r"^view:"))
app.add_handler(CallbackQueryHandler(cb_action,           pattern=r"^(sold|restock|delitem|move):"))
app.add_handler(CallbackQueryHandler(cb_delete,           pattern=r"^(delone|delall):"))
app.add_handler(CallbackQueryHandler(cb_confirmdelshelf,  pattern=r"^confirmdelshelf:"))
app.add_handler(CallbackQueryHandler(cb_canceldelshelf,   pattern=r"^canceldelshelf$"))

# Conversations — each button is its own clean conversation
app.add_handler(make_conv("search", "🔍 Search", search_entry, {
    S_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, search_run)],
}))
app.add_handler(make_conv("add", "➕ Add item", add_entry, {
    S_ADD_BARCODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_got_barcode)],
    S_ADD_SHELF:   [MessageHandler(filters.TEXT & ~filters.COMMAND, add_got_shelf)],
    S_ADD_BOX:     [MessageHandler(filters.TEXT & ~filters.COMMAND, add_got_box)],
}))
app.add_handler(make_conv("photo", "📷 Add photo", photo_entry, {
    S_PHOTO_BARCODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, photo_got_barcode)],
    S_PHOTO_IMG:     [MessageHandler(filters.PHOTO, photo_got_img)],
}))
app.add_handler(make_conv("move", "🔀 Move", move_entry, {
    S_MOVE_BARCODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, move_got_barcode)],
    S_MOVE_SHELF:   [MessageHandler(filters.TEXT & ~filters.COMMAND, move_got_shelf)],
    S_MOVE_BOX:     [MessageHandler(filters.TEXT & ~filters.COMMAND, move_got_box)],
}))
app.add_handler(make_conv("delete", "🗑 Delete", delete_entry, {
    S_DELETE_BARCODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_got_barcode)],
}))
app.add_handler(make_conv("rows", "📦 Rows", rows_entry, {
    S_ROW_MENU:       [CallbackQueryHandler(rm_menu_cb, pattern=r"^rm:")],
    S_SHOWROW_NAME:   [MessageHandler(filters.TEXT & ~filters.COMMAND, rm_show_name)],
    S_ADDROW_NAME:    [MessageHandler(filters.TEXT & ~filters.COMMAND, rm_addrow_name)],
    S_ADDROW_ITEMS:   [MessageHandler(filters.TEXT & ~filters.COMMAND, rm_addrow_items)],
    S_APPENDROW_PICK: [CallbackQueryHandler(rm_append_pick_cb, pattern=r"^pick_append:")],
    S_APPENDROW_ITEMS:[MessageHandler(filters.TEXT & ~filters.COMMAND, rm_append_items)],
    S_INSERTROW_PICK: [CallbackQueryHandler(rm_insert_pick_cb, pattern=r"^pick_insert:")],
    S_INSERTROW_POS:  [MessageHandler(filters.TEXT & ~filters.COMMAND, rm_insert_pos)],
    S_INSERTROW_BARCODE:[MessageHandler(filters.TEXT & ~filters.COMMAND, rm_insert_barcode)],
    S_RENAMEROW_PICK: [CallbackQueryHandler(rm_rename_pick_cb, pattern=r"^pick_rename:")],
    S_RENAMEROW_NEW:  [MessageHandler(filters.TEXT & ~filters.COMMAND, rm_rename_new)],
    S_DELETEROW_PICK: [
        CallbackQueryHandler(rm_delete_pick_cb,   pattern=r"^pick_delete:"),
        CallbackQueryHandler(cb_confirmdelshelf,  pattern=r"^confirmdelshelf:"),
        CallbackQueryHandler(cb_canceldelshelf,   pattern=r"^canceldelshelf$"),
    ],
}))

# Non-conversation keyboard buttons (audit, low stock, help)
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, keyboard_router))

app.add_error_handler(error_handler)

print("Puma Depot Bot v2 running...")
app.run_polling()
