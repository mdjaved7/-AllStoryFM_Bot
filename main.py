import os
import time
import asyncio
import uuid
import re
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    MessageHandler, 
    CommandHandler, 
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ContextTypes, 
    filters
)
from pymongo import MongoClient

# Environment Credentials
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
MONGO_URI = os.getenv("MONGO_URI", "") 

# --- UPI DETAILS ---
UPI_ID = os.getenv("UPI_ID", "md-javed01@ptyes")
PAYEE_NAME = os.getenv("PAYEE_NAME", "All Story FM")

# 4 Hour Delete Alert Image Link
DELETE_ALERT_IMAGE_URL = os.getenv("DELETE_ALERT_IMAGE_URL", "https://www.image2url.com/r2/default/images/1790398034766-ad28a105-060e-4772-9b9a-ba3999feb513.jpg")

# Multiple Admin IDs Setup
ADMIN_IDS_RAW = os.getenv("ADMIN_ID", "0")
ADMIN_IDS = [int(aid.strip()) for aid in ADMIN_IDS_RAW.split(",") if aid.strip().isdigit()]
ADMIN_ID = ADMIN_IDS[0] if ADMIN_IDS else 0

CHANNEL_INVITE_LINK = os.getenv("CHANNEL_INVITE_LINK", "") 
PRIVATE_STORE_ID = int(os.getenv("PRIVATE_STORE_ID", "0"))  

# ==========================================
# हर प्लान के लिए अलग QR कोड / इमेज URL
# ==========================================
PLANS = {
    "plan_1": {
        "name": "1 दिन", 
        "price": 15, 
        "days": 1,
        "qr_image": "https://www.imghippo.com/i/goTw5524mDE.jpg"
    },
    "plan_2": {
        "name": "3 दिन", 
        "price": 35, 
        "days": 3,
        "qr_image": "https://www.imghippo.com/i/gELs1418DvI.png"
    },
    "plan_3": {
        "name": "7 दिन", 
        "price": 55, 
        "days": 7,
        "qr_image": "https://www.imghippo.com/i/CbGe8938nFw.png"
    },
    "plan_4": {
        "name": "20 दिन", 
        "price": 200, 
        "days": 20,
        "qr_image": "https://www.imghippo.com/i/JFj3187QiY.png"
    },
    "plan_5": {
        "name": "6 महीने", 
        "price": 1199, 
        "days": 180,
        "qr_image": "https://www.imghippo.com/i/WtId7867Rfo.png"
    }
}

# MongoDB Setup
client = MongoClient(MONGO_URI)
primary_db = client['bot_primary_db']
user_col = primary_db['users']
delete_col = primary_db['delete_queue'] 
history_col = primary_db['user_history']  
registry_col = primary_db['batch_registry']
config_col = primary_db['bot_config']
fsub_col = primary_db['force_sub_channels']
join_req_col = primary_db['join_requests_data'] 
transactions_col = primary_db['transactions']
global_files_col = primary_db['global_searchable_files'] 

user_queues = {}
backup_queues = {}
cancel_status = {}
processing_tasks = {}

def get_active_file_db():
    config = config_col.find_one({"_id": "file_db_config"})
    idx = config.get("index", 0) if config else 0
    db_name = f"bot_file_db_{idx}"
    current_db = client[db_name]
    try:
        stats_data = current_db.command("dbStats")
        storage_size_mb = stats_data.get("storageSize", 0) / (1024 * 1024)
        if storage_size_mb >= 450.0:
            idx += 1
            config_col.update_one({"_id": "file_db_config"}, {"$set": {"index": idx}}, upsert=True)
            db_name = f"bot_file_db_{idx}"
            current_db = client[db_name]
    except Exception: pass
    return current_db, db_name

def get_readable_size(size_in_bytes):
    if not size_in_bytes: return "Unknown Size"
    for unit in ['Bytes', 'KB', 'MB', 'GB', 'TB']:
        if size_in_bytes < 1024.0: return f"{size_in_bytes:.2f} {unit}"
        size_in_bytes /= 1024.0
    return f"{size_in_bytes:.2f} PB"

def has_active_pass(user_id):
    if user_id in ADMIN_IDS: return True
    user = user_col.find_one({"user_id": user_id})
    if not user: return False
    validity = user.get("pass_validity", 0)
    return time.time() < validity

def check_and_update_free_access(user_id):
    user = user_col.find_one({"user_id": user_id})
    if not user: return False, "12 घंटे"
    now = time.time()
    cooldown_start = user.get("cooldown_start", 0)
    free_count = user.get("free_count", 0)

    if now - cooldown_start >= 43200:
        free_count = 0
        cooldown_start = now

    if free_count < 3:
        if free_count == 0: cooldown_start = now 
        user_col.update_one({"user_id": user_id}, {"$set": {"free_count": free_count + 1, "cooldown_start": cooldown_start}})
        return True, None
    else:
        remaining_seconds = (cooldown_start + 43200) - now
        hours = int(remaining_seconds // 3600)
        minutes = int((remaining_seconds % 3600) // 60)
        time_str = f"{hours} घंटे {minutes} मिनट" if hours > 0 else f"{minutes} मिनट"
        return False, time_str

def get_cooldown_message(remaining_time, user_first_name):
    msg = (f"हेलो {user_first_name}! 🙈\n\nटाइमर अभी भी {remaining_time} दिखा रहा है! क्या आप वाकई इतना लंबा इंतज़ार करेंगे? 😭\n\n"
           f"सिर्फ ₹15 में अनलिमिटेड पास लेकर अभी अपनी सारी पसंदीदा फाइल्स तुरंत डाउनलोड करें! 🙈")
    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔓 अनलिमिटेड एक्सेस अनलॉक करें", callback_data="buy_plan_list")]])
    return msg, markup

def get_plan_menu_message():
    msg = ("👑 **पास सब्सक्रिप्शन प्लान्स V2** 👑\n\n⚡️ पास के मुख्य फायदे:\n"
           "• ⓧ कोई डोनेशन मैसेज नहीं: बिना किसी डोनेशन मैसेज के 100% क्लीन एक्सपीरियंस।\n"
           "• ♾ कोई एक्सेस लिमिट नहीं: बिना किसी कूलडाउन के सभी ऑडियो/फाइल्स लगातार सुनें।\n\n👇 नीचे अपना पसंदीदा पास प्लान चुनें:")
    buttons = []
    for p_id, plan in PLANS.items():
        hot_emoji = "🔥" if p_id == "plan_3" else "🔹"
        buttons.append([InlineKeyboardButton(f"{hot_emoji} {plan['name']} - ₹{plan['price']}", callback_data=f"buy_{p_id}")])
    buttons.append([InlineKeyboardButton("🕘 मेरे ट्रांसक्शन्स", callback_data="my_transactions")])
    return msg, InlineKeyboardMarkup(buttons)

async def get_fsub_buttons(context, user_id, start_param):
    channels = list(fsub_col.find())
    if not channels: return True, [] 
    unjoined_buttons = []
    has_unjoined = False

    for ch in channels:
        ch_id = ch["channel_id"]
        ch_link = ch["invite_link"]
        ch_title = ch.get("title", "Join Channel")
        has_requested = join_req_col.find_one({"user_id": user_id, "channel_id": ch_id})
        if has_requested: continue

        try:
            member = await context.bot.get_chat_member(chat_id=ch_id, user_id=user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                has_unjoined = True
                unjoined_buttons.append([InlineKeyboardButton(f"📢 Request {ch_title}", url=ch_link)])
        except Exception:
            has_unjoined = True
            unjoined_buttons.append([InlineKeyboardButton(f"📢 Request {ch_title}", url=ch_link)])

    if has_unjoined:
        bot_info = await context.bot.get_me()
        unjoined_buttons.append([InlineKeyboardButton("🔄 Try Again", url=f"https://t.me/{bot_info.username}?start={start_param}")])
        return False, unjoined_buttons
    return True, []

async def auto_delete_monitor(app):
    while True:
        try:
            current_time = time.time()
            all_pending = delete_col.find({"delete_at": {"$lte": current_time}})
            for task in all_pending:
                chat_id = task['chat_id']
                for msg_id in task['message_ids']:
                    try: await app.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                    except: pass
                    await asyncio.sleep(0.1) 
                delete_col.delete_one({"_id": task['_id']})
        except Exception: pass
        await asyncio.sleep(15)

async def run_post_init(application):
    asyncio.create_task(auto_delete_monitor(application))

async def send_files_logic(update, context, batch_key):
    user = update.effective_user
    cancel_status[user.id] = False 
    
    if not has_active_pass(user.id):
        is_allowed, remaining_time = check_and_update_free_access(user.id)
        if not is_allowed:
            msg, markup = get_cooldown_message(remaining_time, user.first_name)
            await update.message.reply_text(msg, reply_markup=markup)
            return

    reg_record = registry_col.find_one({"batch_key": batch_key})
    batch = None
    if reg_record:
        target_db_name = reg_record["db_name"]
        batch = client[target_db_name]['file_batches'].find_one({"batch_key": batch_key})
    else:
        batch = client['bot_database']['file_batches'].find_one({"batch_key": batch_key})
    
    if not batch:
        await update.message.reply_text("❌ Yeh link amanya (invalid) hai या एक्सपायर हो चुका है।")
        return

    try:
        history_col.insert_one({"user_id": user.id, "first_name": user.first_name, "username": user.username, "action": "requested_files", "batch_key": batch_key, "time": datetime.now(ZoneInfo("Asia/Kolkata")).strftime('%Y-%m-%d %H:%M:%S')})
    except: pass
    
    info_msg = await update.message.reply_text("⏳ Sending files...", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("• Cancel", callback_data="cancel_action")], [InlineKeyboardButton("📟 UPDATE CHANNEL", url=CHANNEL_INVITE_LINK)]]))
    sent_message_ids = [info_msg.message_id]
    is_cancelled = False
    
    for file in batch["files"]:
        if cancel_status.get(user.id): 
            is_cancelled = True
            break 
        try:
            sent_msg = None
            file_bytes = file.get('file_size', 0)
            readable_size = get_readable_size(file_bytes)
            file_type = file.get('file_type')
            original_caption = file.get('caption', '')
            
            if file_type == 'photo':
                custom_caption = original_caption if original_caption else ">> JOIN > @AllstoryFM2 🔥"
            elif file_type == 'video' and original_caption:
                custom_caption = f"{original_caption}\n\n👉 FILE SIZE :- {readable_size} 👑\n>> JOIN > @AllstoryFM2 🔥"
            else:
                custom_caption = f">> JOIN > @AllstoryFM2 🔥\n✅✨\n\n👉 FILE SIZE :- {readable_size} 👑\n🔥"

            if file['file_type'] == 'document': sent_msg = await context.bot.send_document(update.message.chat_id, file['file_id'], protect_content=True, caption=custom_caption)
            elif file['file_type'] == 'video': sent_msg = await context.bot.send_video(update.message.chat_id, file['file_id'], protect_content=True, caption=custom_caption)
            elif file['file_type'] == 'photo': sent_msg = await context.bot.send_photo(update.message.chat_id, file['file_id'], protect_content=True, caption=custom_caption)
            elif file['file_type'] == 'audio': sent_msg = await context.bot.send_audio(update.message.chat_id, file['file_id'], protect_content=True, caption=custom_caption)

            if sent_msg: sent_message_ids.append(sent_msg.message_id)
            await asyncio.sleep(0.5) 
        except Exception: break

    if len(sent_message_ids) > 0:
        try: delete_col.insert_one({"chat_id": update.message.chat_id, "message_ids": sent_message_ids, "delete_at": time.time() + 14400})
        except: pass

    try: await context.bot.delete_message(chat_id=update.message.chat_id, message_id=info_msg.message_id)
    except: pass

    alert_text = "𝙷𝙸𝙽𝙳𝙸 𝚂𝚃𝙾𝚁𝚈\n❤️ 𝙷𝙴𝚈 𝙱𝚁𝙾 🇮🇳 \n\n📂 𝙵𝙸𝙻𝙴𝚂 𝚆𝙸𝙻𝙻 𝙱𝙴 𝙳𝙴𝙻𝙴𝚃𝙴𝙳 \n𝙰𝙵𝚃𝙴𝚁 [ 4 𝙷𝙾𝚄𝚁𝚂 ] 𝙿𝙻𝙴𝙰𝚂𝙴 \n𝚂𝙰𝚅𝙴 𝚃𝙷𝙴𝙼 𝚂𝙾𝙼𝙴𝚆𝙷𝙴𝚁𝙴 𝚂𝙰𝙵𝙴."
    if is_cancelled: alert_text += "\n\n⚠️ *Process was cancelled by user.*"

    try:
        final_msg = await context.bot.send_photo(
            chat_id=update.message.chat_id,
            photo=DELETE_ALERT_IMAGE_URL,
            caption=alert_text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📟 UPDATE CHANNEL", url=CHANNEL_INVITE_LINK)]])
        )
        delete_col.insert_one({"chat_id": update.message.chat_id, "message_ids": [final_msg.message_id], "delete_at": time.time() + 14400})
    except: pass

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user_col.find_one({"user_id": user.id}):
        user_col.insert_one({"user_id": user.id, "username": user.username, "first_name": user.first_name, "pass_validity": 0, "free_count": 0, "cooldown_start": 0})
        
    args = context.args
    if args:
        start_param = args[0]
        has_joined_all, fsub_buttons = await get_fsub_buttons(context, user.id, start_param)
        if not has_joined_all:
            await update.message.reply_text("⚠️ <b>Access Restricted!</b>\n\nFiles receive karne ke liye niche diye gaye remaining channels ko join karein:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(fsub_buttons))
            return
        asyncio.create_task(send_files_logic(update, context, start_param))
        return
        
    await update.message.reply_text("🗄️ Welcome! Type a file name to search, or send /plan to buy subscription! 🎯🔥")

async def show_plans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, markup = get_plan_menu_message()
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=markup)

async def handle_callback_queries(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    
    if data == "cancel_action":
        cancel_status[query.from_user.id] = True 
        try: await query.message.delete()
        except: pass
        await query.answer("❌ Files bhejna rok diya gaya hai.")
        return

    if data == "buy_plan_list":
        msg, markup = get_plan_menu_message()
        await query.message.reply_text(msg, parse_mode="Markdown", reply_markup=markup)
        await query.answer()
        return

    if data == "my_transactions":
        await query.answer("Profile fetching...")
        await user_profile(update, context, direct_query=query)
        return

    if data.startswith("buy_plan_"):
        plan_id = data.replace("buy_", "")
        if plan_id not in PLANS: return
        plan = PLANS[plan_id]
        order_receipt = f"PASS-{query.from_user.id}-{plan['days']}D-{uuid.uuid4().hex[:4].upper()}"

        transactions_col.insert_one({
            "order_id": order_receipt,
            "user_id": query.from_user.id,
            "plan_id": plan_id,
            "amount": plan['price'],
            "status": "pending_screenshot",
            "date": datetime.now(ZoneInfo("Asia/Kolkata"))
        })

        caption_msg = (
            f"👑 **प्लान:** {plan['name']} (₹{plan['price']})\n"
            f"🆔 **ऑर्डर ID:** `{order_receipt}`\n"
            f"🔗 **UPI ID:** `{UPI_ID}`\n\n"
            f"📌 **पेमेंट कैसे करें:**\n"
            f"1. ऊपर दिए गए QR कोड को Google Pay, PhonePe, Paytm से स्कैन करें।\n"
            f"2. पूरे **₹{plan['price']}** का भुगतान करें।\n"
            f"3. पेमेंट के बाद, **यहीं पर स्क्रीनशॉट (फोटो) भेजें**।\n\n"
            f"⚠️ **स्क्रीनशॉट भेजते ही आपका पास चेक करके एक्टिवेट कर दिया जाएगा।**"
        )

        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 वापस प्लान्स पर जाएं", callback_data="buy_plan_list")]
        ])

        try:
            await query.message.reply_photo(photo=plan['qr_image'], caption=caption_msg, parse_mode="Markdown", reply_markup=markup)
            await query.answer()
        except Exception:
            await query.message.reply_text(caption_msg, parse_mode="Markdown", reply_markup=markup)
            await query.answer()

    if data.startswith("admin_approve_"):
        if query.from_user.id not in ADMIN_IDS:
            await query.answer("❌ केवल एडमिन ही अप्रूव कर सकते हैं!", show_alert=True)
            return

        order_id = data.replace("admin_approve_", "")
        tx = transactions_col.find_one({"order_id": order_id, "status": "pending_screenshot"})

        if not tx:
            await query.answer("⚠️ यह लेनदेन पहले ही प्रोसेस हो चुका है!", show_alert=True)
            return

        plan = PLANS[tx['plan_id']]
        user_id = tx['user_id']
        current_time = time.time()
        user_data = user_col.find_one({"user_id": user_id})
        current_validity = user_data.get("pass_validity", 0) if user_data else 0
        if current_validity < current_time: current_validity = current_time 

        new_validity = current_validity + (plan['days'] * 86400) 
        exact_expiry_date = datetime.fromtimestamp(new_validity, ZoneInfo("Asia/Kolkata")).strftime('%d/%m/%Y | %I:%M:%S %p')

        user_col.update_one({"user_id": user_id}, {"$set": {"pass_validity": new_validity}})
        transactions_col.update_one({"_id": tx["_id"]}, {"$set": {"status": "success", "completed_at": datetime.now(ZoneInfo("Asia/Kolkata")).strftime('%d/%m/%Y | %I:%M %p')}})

        await query.message.edit_caption(caption=query.message.caption + "\n\n✅ **APPROVED BY ADMIN**", parse_mode="Markdown")
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"✅ **पेमेंट सफल रहा!**\nआपका {plan['name']} का पास एक्टिवेट कर दिया गया है।\n⏰ **समाप्ति समय:** `{exact_expiry_date}`\n\nअब आप सभी फाइल्स एक्सेस कर सकते हैं।",
                parse_mode="Markdown"
            )
        except Exception: pass
        await query.answer("✅ पास एक्टिवेट कर दिया गया!")

    if data.startswith("admin_reject_"):
        if query.from_user.id not in ADMIN_IDS:
            await query.answer("❌ केवल एडमिन रिजेक्ट कर सकते हैं!", show_alert=True)
            return

        order_id = data.replace("admin_reject_", "")
        tx = transactions_col.find_one({"order_id": order_id})
        if tx:
            transactions_col.update_one({"_id": tx["_id"]}, {"$set": {"status": "rejected"}})
            try:
                await context.bot.send_message(
                    chat_id=tx['user_id'],
                    text="❌ **पेमेंट वेरिफिकेशन असफल!**\nआपका भेजा गया स्क्रीनशॉट अमान्य पाया गया। कृपया सही पेमेंट प्रूफ भेजें।"
                )
            except Exception: pass
        await query.message.edit_caption(caption=query.message.caption + "\n\n❌ **REJECTED BY ADMIN**", parse_mode="Markdown")
        await query.answer("पेमेंट रिजेक्ट कर दिया गया।")

async def handle_user_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    pending_tx = transactions_col.find_one({"user_id": user_id, "status": "pending_screenshot"}, sort=[("date", -1)])
    
    if not pending_tx:
        return

    plan = PLANS.get(pending_tx['plan_id'], {})
    photo_file_id = update.message.photo[-1].file_id

    await update.message.reply_text("⏳ **स्क्रीनशॉट प्राप्त हुआ!**\nएडमिन वेरिफिकेशन की जाँच कर रहा है, पुष्टि होते ही आपका पास एक्टिवेट हो जाएगा।")

    admin_markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"admin_approve_{pending_tx['order_id']}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"admin_reject_{pending_tx['order_id']}")
        ]
    ])

    admin_caption = (
        f"📩 **नया पेमेंट वेरिफिकेशन अनुरोध!**\n\n"
        f"👤 यूजर: {update.effective_user.first_name} (@{update.effective_user.username})\n"
        f"🆔 User ID: `{user_id}`\n"
        f"🔖 Order ID: `{pending_tx['order_id']}`\n"
        f"👑 प्लान: {plan.get('name')} (₹{plan.get('price')})\n\n"
        f"कृपया अपने बैंक/UPI में जांचें और नीचे से अप्रूव या रिजेक्ट करें:"
    )

    for admin in ADMIN_IDS:
        try:
            await context.bot.send_photo(chat_id=admin, photo=photo_file_id, caption=admin_caption, parse_mode="Markdown", reply_markup=admin_markup)
        except Exception: pass

async def user_profile(update: Update, context: ContextTypes.DEFAULT_TYPE, direct_query=None):
    user_id = update.effective_user.id
    user_data = user_col.find_one({"user_id": user_id})
    validity = user_data.get("pass_validity", 0) if user_data else 0
    status_text = f"🟢 सक्रिय (वैधता: {datetime.fromtimestamp(validity, ZoneInfo('Asia/Kolkata')).strftime('%d-%m-%Y %I:%M:%S %p')})" if time.time() < validity else "🔴 निष्क्रिय (कृपया पास खरीदें)"

    msg = f"📜 मेरे ट्रांसक्शन्स और पास स्थिति\n──────────────────────\nयूजर: {update.effective_user.first_name} ({user_id})\nपास स्थिति: {status_text}\n──────────────────────\nहाल के लेनदेन:\n\n"
    txs = list(transactions_col.find({"user_id": user_id, "status": "success"}).sort("date", -1).limit(3))
    
    if not txs: msg += "कोई हालिया लेनदेन नहीं मिला।"
    else:
        for i, tx in enumerate(txs, 1):
            plan_name = PLANS.get(tx.get('plan_id'), {}).get('name', 'Unknown')
            date_str = tx.get('completed_at', tx['date'].strftime('%d/%m/%Y | %I:%M %p'))
            msg += f"┄┄┄┄┄┄┄┄┄┄┄ {i} ┄┄┄┄┄┄┄┄┄┄\n\n🆔 ऑर्डर:- {tx['order_id']}\n👑 प्लान:- {plan_name} (₹{tx['amount']})\n💳 पेमेंट मोड:- Direct UPI\n📊 स्थिति:- ✅ ( पेड )\n📅 तारीख:- {date_str}\n\n"

    if direct_query: await direct_query.message.reply_text(msg)
    else: await update.message.reply_text(msg)

async def handle_text_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    user_id = update.effective_user.id
    if len(text) < 3:
        return 
        
    if not has_active_pass(user_id):
        is_allowed, remaining_time = check_and_update_free_access(user_id)
        if not is_allowed:
            msg, markup = get_cooldown_message(remaining_time, update.effective_user.first_name)
            await update.message.reply_text(msg, reply_markup=markup)
            return

    search_msg = await update.message.reply_text("🔍 फाइल खोजी जा रही है...")
    results = list(global_files_col.find({"caption": {"$regex": text, "$options": "i"}}).limit(5))
    
    if not results:
        await search_msg.edit_text(f"❌ '{text}' से जुड़ी कोई फाइल नहीं मिली।")
        return
        
    await search_msg.edit_text(f"✅ '{text}' से जुड़ी फाइल्स भेजी जा रही हैं...")
    sent_msg_ids = [search_msg.message_id]
    
    for file in results:
        try:
            readable_size = get_readable_size(file.get('file_size', 0))
            original_caption = file.get('caption', '')
            if file['file_type'] == 'photo':
                custom_caption = original_caption if original_caption else ">> JOIN > @AllstoryFM2 🔥"
            elif file['file_type'] == 'video' and original_caption:
                custom_caption = f"{original_caption}\n\n👉 FILE SIZE :- {readable_size} 👑\n>> JOIN > @AllstoryFM2 🔥"
            else:
                custom_caption = f">> JOIN > @AllstoryFM2 🔥\n✅✨\n\n👉 FILE SIZE :- {readable_size} 👑\n🔥"
            
            sent_msg = None
            if file['file_type'] == 'document': sent_msg = await context.bot.send_document(update.message.chat_id, file['file_id'], protect_content=True, caption=custom_caption)
            elif file['file_type'] == 'video': sent_msg = await context.bot.send_video(update.message.chat_id, file['file_id'], protect_content=True, caption=custom_caption)
            elif file['file_type'] == 'photo': sent_msg = await context.bot.send_photo(update.message.chat_id, file['file_id'], protect_content=True, caption=custom_caption)
            elif file['file_type'] == 'audio': sent_msg = await context.bot.send_audio(update.message.chat_id, file['file_id'], protect_content=True, caption=custom_caption)
            if sent_msg: sent_msg_ids.append(sent_msg.message_id)
            await asyncio.sleep(0.5) 
        except Exception: pass

    if sent_msg_ids:
        try: delete_col.insert_one({"chat_id": update.message.chat_id, "message_ids": sent_msg_ids, "delete_at": time.time() + 14400})
        except: pass

    alert_text = "𝙷𝙸𝙽𝙳𝙸 𝚂𝚃𝙾𝚁𝚈\n❤️ 𝙷𝙴𝚈 𝙱𝚁𝙾 🇮🇳 \n\n📂 𝙵𝙸𝙻𝙴𝚂 𝚆𝙸𝙻𝙻 𝙱𝙴 𝙳𝙴𝙻𝙴𝚃𝙴𝙳 \n𝙰𝙵𝚃𝙴𝚁 [ 4 𝙷𝙾𝚄𝚁𝚂 ] 𝙿𝙻𝙴𝙰𝚂𝙴 \n𝚂𝙰𝚅𝙴 𝚃𝙷𝙴𝙼 𝚂𝙾𝙼𝙴𝚆𝙷𝙴𝚁𝙴 𝚂𝙰𝙵𝙴."
    try:
        final_msg = await context.bot.send_photo(
            chat_id=update.message.chat_id, photo=DELETE_ALERT_IMAGE_URL, caption=alert_text, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📟 UPDATE CHANNEL", url=CHANNEL_INVITE_LINK)]])
        )
        delete_col.insert_one({"chat_id": update.message.chat_id, "message_ids": [final_msg.message_id], "delete_at": time.time() + 14400})
    except: pass

async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    if len(context.args) < 3: return await update.message.reply_text("❌ Format: `/addchannel <ID> <Link> <Title>`", parse_mode="Markdown")
    try:
        fsub_col.update_one({"channel_id": int(context.args[0])}, {"$set": {"invite_link": context.args[1], "title": " ".join(context.args[2:])}}, upsert=True)
        await update.message.reply_text("✅ Channel Added!")
    except Exception as e: await update.message.reply_text(f"❌ Error: {e}")

async def del_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    if not context.args: return
    try:
        fsub_col.delete_one({"channel_id": int(context.args[0])})
        await update.message.reply_text("✅ Channel removed.")
    except Exception as e: await update.message.reply_text(f"❌ Error: {e}")

async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    channels = list(fsub_col.find())
    msg = "📢 <b>Active Force Join Channels:</b>\n\n"
    for idx, ch in enumerate(channels, 1): msg += f"{idx}. <b>{ch.get('title')}</b>\n🆔 <code>{ch.get('channel_id')}</code>\n🔗 {ch.get('invite_link')}\n\n"
    await update.message.reply_text(msg if channels else "📁 Empty.", parse_mode="HTML")

async def check_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    try:
        logs = list(history_col.find().sort("_id", -1).limit(15))
        await update.message.reply_text("📊 Recent Logs:\n\n" + "".join([f"👤 {e.get('first_name')}\n📥 {e.get('batch_key')}\n⏰ {e.get('time')}\n\n" for e in logs]))
    except: pass

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    active_db, active_name = get_active_file_db()
    try:
        stats_cmd = active_db.command("dbStats")
        storage_mb = stats_cmd.get("storageSize", stats_cmd.get("dataSize", 0)) / (1024 * 1024)
        storage_text = f"{storage_mb:.2f} MB ({active_name})"
    except: storage_text = "Unavailable"
    await update.message.reply_text(f"👥 Total Users: {user_col.count_documents({})}\n📥 Requests: {history_col.count_documents({})}\n🗄️ Storage: {storage_text}")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS: return
    if not context.args and not update.message.reply_to_message: return
    await update.message.reply_text("📢 Broadcast shuru ho raha hai...")
    success = failed = 0
    for user in user_col.find():
        try:
            if update.message.reply_to_message: await context.bot.copy_message(user['user_id'], update.message.chat_id, update.message.reply_to_message.message_id)
            else: await context.bot.send_message(user['user_id'], " ".join(context.args))
            success += 1
            await asyncio.sleep(0.05)
        except: failed += 1
    await update.message.reply_text(f"✅ Complete!\n🟢 Success: {success}\n🔴 Failed: {failed}")

async def process_batch_queue(user_id, context, message):
    await asyncio.sleep(15)
    if user_id not in user_queues: return
    raw_files = user_queues.pop(user_id)
    saved_files = []
    
    for msg in raw_files:
        if not msg: continue
        file_obj = msg.document or msg.video or (msg.photo[-1] if msg.photo else None) or msg.audio
        if file_obj:
            if PRIVATE_STORE_ID != 0:
                try: await context.bot.forward_message(PRIVATE_STORE_ID, msg.chat_id, msg.message_id)
                except: pass
            
            file_name = getattr(file_obj, 'file_name', '')
            caption = msg.caption or ""
            file_type = 'document' if msg.document else 'video' if msg.video else 'audio' if msg.audio else 'photo'
            
            file_data = {
                "file_id": file_obj.file_id, 
                "file_size": getattr(file_obj, 'file_size', 0), 
                "file_type": file_type, 
                "caption": caption,
                "file_name": file_name
            }
            saved_files.append(file_data)
            global_files_col.insert_one(file_data)
            
    backup_queues[user_id] = saved_files
    await message.reply_text("✅ Batch & Search Index updated! Now send /getlink command.")

async def handle_incoming_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    user_id = update.effective_user.id
    if user_id in ADMIN_IDS:
        if user_id not in user_queues: user_queues[user_id] = []
        user_queues[user_id].append(update.message)
        if user_id in processing_tasks: processing_tasks[user_id].cancel()
        processing_tasks[user_id] = asyncio.create_task(process_batch_queue(user_id, context, update.message))
    else:
        if update.message and update.message.photo:
            await handle_user_screenshot(update, context)

def extract_ep_number(file_item):
    text_to_check = f"{file_item.get('caption', '')} {file_item.get('file_name', '')}"
    match = re.search(r'(?:ep|episode)?\s*[_.-]?\s*(\d+)', text_to_check, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None

# =========================================================================
# GETLINK: 5 ऑडियो + 1 फोटो को एक साथ बैच में बांधने का ऑटोमैटिक लॉजिक
# =========================================================================
async def get_link_manually(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS: return
    if user_id not in backup_queues or not backup_queues[user_id]: 
        await update.message.reply_text("❌ कोई फाइल्स कतार में नहीं मिलीं!")
        return
    
    all_items = backup_queues[user_id]
    
    # फाइलों में से ऑडियो और फोटो को क्रम से अलग करें
    audio_files = [f for f in all_items if f['file_type'] in ['audio', 'document']]
    photo_files = [f for f in all_items if f['file_type'] == 'photo']
    
    if not audio_files:
        await update.message.reply_text("❌ कतार में कोई ऑडियो फाइल्स नहीं मिलीं!")
        return

    CHUNK_SIZE = 5
    audio_chunks = [audio_files[i:i + CHUNK_SIZE] for i in range(0, len(audio_files), CHUNK_SIZE)]
    
    active_db, active_name = get_active_file_db()
    bot_info = await context.bot.get_me()
    
    response_lines = []
    base_counter = 1

    for idx, chunk in enumerate(audio_chunks):
        # 5 ऑडियो के साथ उसी क्रम की एक फोटो जोड़ें
        batch_files = []
        if idx < len(photo_files):
            batch_files.append(photo_files[idx])
        batch_files.extend(chunk)

        # केवल ऑडियो फाइलों से ही शुरुआती और आखिरी एपिसोड नंबर निकालें
        start_ep = extract_ep_number(chunk[0])
        end_ep = extract_ep_number(chunk[-1])
        
        if start_ep is None or end_ep is None:
            start_ep = base_counter
            end_ep = base_counter + len(chunk) - 1
            base_counter = end_ep + 1

        batch_key = f"batch_{int(time.time())}_{idx+1}"
        
        # डेटाबेस में फोटो + 5 ऑडियो का पूरा बैच सेव करें
        active_db['file_batches'].insert_one({
            "batch_key": batch_key, 
            "files": batch_files, 
            "timestamp": time.time()
        })
        registry_col.insert_one({"batch_key": batch_key, "db_name": active_name})
        
        link = f"https://t.me/{bot_info.username}?start={batch_key}"
        line = f"✅ 🇮🇳 Hindi Ep {start_ep} x {end_ep} - {link}"
        response_lines.append(line)
        await asyncio.sleep(0.05)
    
    backup_queues.pop(user_id, None)
    
    final_output = "\n\n".join(response_lines)
    
    if len(final_output) > 4000:
        for i in range(0, len(response_lines), 10):
            batch_part = "\n\n".join(response_lines[i:i + 10])
            await update.message.reply_text(batch_part, disable_web_page_preview=True)
    else:
        await update.message.reply_text(final_output, disable_web_page_preview=True)

async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: join_req_col.update_one({"user_id": update.chat_join_request.from_user.id, "channel_id": update.chat_join_request.chat.id}, {"$set": {"status": "requested", "time": time.time()}}, upsert=True)
    except: pass

def main():
    request_kwargs = HTTPXRequest(connect_timeout=20.0, read_timeout=20.0)
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).request(request_kwargs).post_init(run_post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("getlink", get_link_manually))
    app.add_handler(CommandHandler("plan", show_plans))
    app.add_handler(CommandHandler("profile", user_profile))
    app.add_handler(CommandHandler("logs", check_logs))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("addchannel", add_channel))
    app.add_handler(CommandHandler("delchannel", del_channel))
    app.add_handler(CommandHandler("channels", list_channels))
    
    app.add_handler(CallbackQueryHandler(handle_callback_queries))
    app.add_handler(ChatJoinRequestHandler(handle_join_request))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_messages))
    app.add_handler(MessageHandler(filters.PHOTO, handle_incoming_files))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.VIDEO | filters.AUDIO, handle_incoming_files))

    print("🤖 Direct UPI & Admin Screen Verification Bot Running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
