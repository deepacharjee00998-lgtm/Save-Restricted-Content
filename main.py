# BeastX Save Restricted Content Bot + Payment System
# Updated with Free Trial + UPI Payment + Destination Chat
# Using pyrofork

import os
import asyncio
import datetime
import time
import traceback
import threading
from datetime import timedelta

import motor.motor_asyncio
from pyrogram import Client, filters, enums
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.errors import (
    FloodWait, UserIsBlocked, InputUserDeactivated, UserAlreadyParticipant,
    InviteHashExpired, UsernameNotOccupied, PeerIdInvalid,
    ApiIdInvalid, PhoneNumberInvalid, PhoneCodeInvalid, PhoneCodeExpired,
    SessionPasswordNeeded, PasswordHashInvalid
)
from pyrogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery
)
from asyncio.exceptions import TimeoutError
from http.server import BaseHTTPRequestHandler, HTTPServer

# =========================
# Configuration
# =========================
def required_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value

LOGIN_SYSTEM = str(os.environ.get("LOGIN_SYSTEM", "True")).lower() in ("true", "1", "yes", "on")
STRING_SESSION = os.environ.get("STRING_SESSION", "").strip() if not LOGIN_SYSTEM else None
BOT_TOKEN = required_env("BOT_TOKEN")
API_ID = int(required_env("API_ID"))
API_HASH = required_env("API_HASH")
ADMINS = int(required_env("ADMINS"))
CHANNEL_ID = os.environ.get("CHANNEL_ID", "").strip()
DB_URI = required_env("DB_URI")
DB_NAME = os.environ.get("DB_NAME", "beastxsavecontentbot").strip() or "beastxsavecontentbot"
WAITING_TIME = int(os.environ.get("WAITING_TIME", "10"))
ERROR_MESSAGE = str(os.environ.get("ERROR_MESSAGE", "True")).lower() in ("true", "1", "yes", "on")
UPI_ID = required_env("UPI_ID")
FREE_TRIALS = int(os.environ.get("FREE_TRIALS", "2"))

if not LOGIN_SYSTEM and not STRING_SESSION:
    raise RuntimeError("STRING_SESSION is required when LOGIN_SYSTEM=False")

# Plans: days -> price
PLANS = {
    1: 20,
    5: 50,
    7: 60,
    30: 130
}

# =========================
# Database
# =========================
class Database:
    def __init__(self, uri, database_name):
        self._client = motor.motor_asyncio.AsyncIOMotorClient(uri)
        self.db = self._client[database_name]
        self.col = self.db.users

    def new_user(self, id, name):
        return {
            "id": id,
            "name": name,
            "session": None,
            "api_id": None,
            "api_hash": None,
            "free_used": 0,
            "premium_until": None,
            "dest_chat": None,
            "dest_topic": None,
            "pending_plan": None
        }

    async def add_user(self, id, name):
        if not await self.is_user_exist(id):
            await self.col.insert_one(self.new_user(id, name))

    async def is_user_exist(self, id):
        user = await self.col.find_one({"id": int(id)})
        return bool(user)

    async def total_users_count(self):
        return await self.col.count_documents({})

    async def get_all_users(self):
        return self.col.find({})

    async def delete_user(self, user_id):
        await self.col.delete_many({"id": int(user_id)})

    async def set_session(self, id, session):
        await self.col.update_one({"id": int(id)}, {"$set": {"session": session}})

    async def get_session(self, id):
        user = await self.col.find_one({"id": int(id)})
        return user.get("session") if user else None

    async def set_api_id(self, id, api_id):
        await self.col.update_one({"id": int(id)}, {"$set": {"api_id": api_id}})

    async def get_api_id(self, id):
        user = await self.col.find_one({"id": int(id)})
        return user.get("api_id") if user else None

    async def set_api_hash(self, id, api_hash):
        await self.col.update_one({"id": int(id)}, {"$set": {"api_hash": api_hash}})

    async def get_api_hash(self, id):
        user = await self.col.find_one({"id": int(id)})
        return user.get("api_hash") if user else None

    async def get_user(self, id):
        return await self.col.find_one({"id": int(id)})

    async def update_user(self, id, data: dict):
        await self.col.update_one({"id": int(id)}, {"$set": data})

    async def increment_free(self, id):
        await self.col.update_one({"id": int(id)}, {"$inc": {"free_used": 1}})

    async def set_premium(self, id, days: int):
        until = datetime.datetime.utcnow() + timedelta(days=days)
        await self.col.update_one(
            {"id": int(id)},
            {"$set": {"premium_until": until, "pending_plan": None}}
        )

    async def clear_pending(self, id):
        await self.col.update_one({"id": int(id)}, {"$set": {"pending_plan": None}})

db = Database(DB_URI, DB_NAME)

# =========================
# Account Session
# =========================
if STRING_SESSION and not LOGIN_SYSTEM:
    BeastXUser = Client("beastx", api_id=API_ID, api_hash=API_HASH, session_string=STRING_SESSION)
    BeastXUser.start()
else:
    BeastXUser = None

# =========================
# Helpers
# =========================
async def has_access(user_id: int):
    """Returns (has_access, reason)"""
    user = await db.get_user(user_id)
    if not user:
        return False, "User not found"

    # Check premium
    premium_until = user.get("premium_until")
    if premium_until:
        if isinstance(premium_until, str):
            premium_until = datetime.datetime.fromisoformat(premium_until)
        if premium_until > datetime.datetime.utcnow():
            return True, "premium"

    # Check free trial
    free_used = user.get("free_used", 0)
    if free_used < FREE_TRIALS:
        return True, "free"

    return False, "no_access"


async def get_remaining_free(user_id: int) -> int:
    user = await db.get_user(user_id)
    if not user:
        return FREE_TRIALS
    return max(0, FREE_TRIALS - user.get("free_used", 0))


def get_plans_keyboard():
    buttons = []
    for days, price in PLANS.items():
        buttons.append([
            InlineKeyboardButton(
                f"{days} Day{'s' if days > 1 else ''} - ₹{price}",
                callback_data=f"plan_{days}_{price}"
            )
        ])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_pay")])
    return InlineKeyboardMarkup(buttons)


# =========================
# Start / Help
# =========================
HELP_TXT = """**🌟 Help Menu**

**__FOR PRIVATE CHATS__**
First send the invite link of the chat, then send the post link.

**__FOR BOT CHATS__**
Send link like:
`https://t.me/b/botusername/4321`

**__MULTI POSTS__**
`https://t.me/xxxx/1001-1010`
`https://t.me/c/xxxx/101-120`

**__Payment Commands__**
/plans - See available plans
/myplan - Check your current plan
/setchat <chat_id> - Set destination chat
/settopic <topic_id> - Set topic (for forum groups)
/clearchat - Clear destination

**__Other__**
/login - Login your account
/logout - Logout
/cancel - Cancel batch
"""

@Client.on_message(filters.command(["start"]))
async def send_start(client: Client, message: Message):
    if not await db.is_user_exist(message.from_user.id):
        await db.add_user(message.from_user.id, message.from_user.first_name)

    remaining = await get_remaining_free(message.from_user.id)
    access, reason = await has_access(message.from_user.id)

    status = ""
    if reason == "premium":
        user = await db.get_user(message.from_user.id)
        until = user.get("premium_until")
        status = f"\n\n✅ **Premium Active** till `{until}`"
    elif reason == "free":
        status = f"\n\n🎁 **Free Trial Remaining:** `{remaining}`"

    buttons = [[
        InlineKeyboardButton("💳 Buy Premium", callback_data="show_plans")
    ], [
        InlineKeyboardButton("❣️ Developer", url="https://t.me/Godmrunal")
    ], [
        InlineKeyboardButton('🔍 Support', url='https://t.me/Beastx_Support'),
        InlineKeyboardButton('🤖 Updates', url='https://t.me/Beastx_Bots')
    ]]
    reply_markup = InlineKeyboardMarkup(buttons)

    await client.send_message(
        chat_id=message.chat.id,
        text=f"<b>👋 Hi {message.from_user.mention}, I am Save Restricted Content Bot.\n\nI can send you restricted content by its post link.\n\nFor downloading restricted content /login first.\n\nKnow how to use → /help</b>{status}",
        reply_markup=reply_markup,
        reply_to_message_id=message.id
    )


@Client.on_message(filters.command(["help"]))
async def send_help(client: Client, message: Message):
    await client.send_message(chat_id=message.chat.id, text=HELP_TXT)


@Client.on_message(filters.command(["cancel"]))
async def send_cancel(client: Client, message: Message):
    batch_temp.IS_BATCH[message.from_user.id] = True
    await client.send_message(chat_id=message.chat.id, text="**Batch Successfully Cancelled.**")


# =========================
# Payment Commands
# =========================
@Client.on_message(filters.command(["plans"]))
async def show_plans_cmd(client: Client, message: Message):
    text = "**💳 Available Plans**\n\n"
    for days, price in PLANS.items():
        text += f"• {days} Day{'s' if days > 1 else ''} → ₹{price}\n"
    text += "\nSelect a plan below:"
    await message.reply(text, reply_markup=get_plans_keyboard())


@Client.on_message(filters.command(["myplan"]))
async def my_plan(client: Client, message: Message):
    user = await db.get_user(message.from_user.id)
    if not user:
        await message.reply("You are not registered. Send /start")
        return

    free_used = user.get("free_used", 0)
    remaining = max(0, FREE_TRIALS - free_used)
    premium_until = user.get("premium_until")

    text = f"**📊 Your Plan Status**\n\n"
    text += f"🎁 Free Trial Used: `{free_used}/{FREE_TRIALS}`\n"
    text += f"🎁 Remaining Free: `{remaining}`\n\n"

    if premium_until:
        if isinstance(premium_until, str):
            premium_until = datetime.datetime.fromisoformat(premium_until)
        if premium_until > datetime.datetime.utcnow():
            text += f"✅ **Premium Active**\nExpires: `{premium_until.strftime('%Y-%m-%d %H:%M')} UTC`"
        else:
            text += "❌ Premium Expired"
    else:
        text += "❌ No active Premium"

    await message.reply(text)


@Client.on_message(filters.command(["setchat"]))
async def set_chat(client: Client, message: Message):
    try:
        chat_id = int(message.command[1])
        await db.update_user(message.from_user.id, {"dest_chat": chat_id})
        await message.reply(f"**Destination Chat set to:** `{chat_id}`\n\nNow all downloads will go there.")
    except:
        await message.reply("**Usage:** `/setchat -100xxxxxxxxxx`")


@Client.on_message(filters.command(["settopic"]))
async def set_topic(client: Client, message: Message):
    try:
        topic_id = int(message.command[1])
        await db.update_user(message.from_user.id, {"dest_topic": topic_id})
        await message.reply(f"**Topic ID set to:** `{topic_id}`")
    except:
        await message.reply("**Usage:** `/settopic 123`")


@Client.on_message(filters.command(["clearchat"]))
async def clear_chat(client: Client, message: Message):
    await db.update_user(message.from_user.id, {"dest_chat": None, "dest_topic": None})
    await message.reply("**Destination cleared.** Downloads will come to your private chat.")


# =========================
# Callback Handlers
# =========================
@Client.on_callback_query(filters.regex(r"^show_plans$"))
async def cb_show_plans(client: Client, query: CallbackQuery):
    await query.message.edit_text(
        "**💳 Select a Plan:**",
        reply_markup=get_plans_keyboard()
    )


@Client.on_callback_query(filters.regex(r"^plan_(\d+)_(\d+)$"))
async def cb_select_plan(client: Client, query: CallbackQuery):
    days = int(query.matches[0].group(1))
    price = int(query.matches[0].group(2))

    await db.update_user(query.from_user.id, {
        "pending_plan": {"days": days, "price": price}
    })

    text = (
        f"**✅ Plan Selected**\n\n"
        f"📅 Duration: **{days} Day{'s' if days > 1 else ''}**\n"
        f"💰 Amount: **₹{price}**\n\n"
        f"**UPI ID:** `{UPI_ID}`\n\n"
        f"1️⃣ Is UPI ID pe ₹{price} pay karo\n"
        f"2️⃣ Payment ke baad neeche **Maine Payment Kar Diya** button dabao\n"
        f"3️⃣ Admin approve karega → Premium start ho jayega"
    )

    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Maine Payment Kar Diya", callback_data=f"paid_{days}_{price}")],
        [InlineKeyboardButton("🔙 Back", callback_data="show_plans")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_pay")]
    ])
    await query.message.edit_text(text, reply_markup=buttons)


@Client.on_callback_query(filters.regex(r"^paid_(\d+)_(\d+)$"))
async def cb_paid(client: Client, query: CallbackQuery):
    days = int(query.matches[0].group(1))
    price = int(query.matches[0].group(2))
    user_id = query.from_user.id
    name = query.from_user.first_name
    username = query.from_user.username or "NoUsername"

    # Notify Admin
    admin_text = (
        f"**💰 New Payment Request**\n\n"
        f"👤 User: [{name}](tg://user?id={user_id})\n"
        f"🆔 ID: `{user_id}`\n"
        f"🔗 Username: @{username}\n"
        f"📅 Plan: **{days} Days**\n"
        f"💰 Amount: **₹{price}**\n\n"
        f"Approve karein?"
    )
    admin_buttons = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_{user_id}_{days}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_{user_id}")
        ]
    ])
    try:
        await client.send_message(ADMINS, admin_text, reply_markup=admin_buttons)
    except Exception as e:
        await query.message.edit_text(f"Error sending to admin: {e}")
        return

    await query.message.edit_text(
        "**✅ Payment request Admin ko bhej diya gaya hai.**\n\n"
        "Admin approve karega to aapka Premium plan activate ho jayega.\n"
        "Thoda wait karein."
    )


@Client.on_callback_query(filters.regex(r"^approve_(\d+)_(\d+)$"))
async def cb_approve(client: Client, query: CallbackQuery):
    if query.from_user.id != ADMINS:
        await query.answer("Only Admin can approve!", show_alert=True)
        return

    user_id = int(query.matches[0].group(1))
    days = int(query.matches[0].group(2))

    await db.set_premium(user_id, days)

    try:
        await client.send_message(
            user_id,
            f"**🎉 Payment Approved!**\n\n"
            f"Aapka **{days} Days Premium** plan activate ho gaya hai.\n"
            f"Ab aap restricted content download kar sakte ho.\n\n"
            f"Check status: /myplan"
        )
    except:
        pass

    await query.message.edit_text(
        f"**✅ Approved**\nUser `{user_id}` ko {days} days premium de diya gaya."
    )
    await query.answer("Approved!")


@Client.on_callback_query(filters.regex(r"^reject_(\d+)$"))
async def cb_reject(client: Client, query: CallbackQuery):
    if query.from_user.id != ADMINS:
        await query.answer("Only Admin can reject!", show_alert=True)
        return

    user_id = int(query.matches[0].group(1))
    await db.clear_pending(user_id)

    try:
        await client.send_message(
            user_id,
            "**❌ Payment Rejected**\n\n"
            "Aapka payment request reject kar diya gaya.\n"
            "Agar aapne payment kiya hai to Admin se contact karein."
        )
    except:
        pass

    await query.message.edit_text(f"**❌ Rejected** User `{user_id}`")
    await query.answer("Rejected!")


@Client.on_callback_query(filters.regex(r"^cancel_pay$"))
async def cb_cancel_pay(client: Client, query: CallbackQuery):
    await db.clear_pending(query.from_user.id)
    await query.message.edit_text("**Payment cancelled.**")


# =========================
# Batch Temp
# =========================
class batch_temp(object):
    IS_BATCH = {}


async def downstatus(client, statusfile, message, chat):
    while True:
        if os.path.exists(statusfile):
            break
        await asyncio.sleep(3)
    while os.path.exists(statusfile):
        with open(statusfile, "r") as downread:
            txt = downread.read()
        try:
            await client.edit_message_text(chat, message.id, f"**Downloaded:** **{txt}**")
            await asyncio.sleep(10)
        except:
            await asyncio.sleep(5)


async def upstatus(client, statusfile, message, chat):
    while True:
        if os.path.exists(statusfile):
            break
        await asyncio.sleep(3)
    while os.path.exists(statusfile):
        with open(statusfile, "r") as upread:
            txt = upread.read()
        try:
            await client.edit_message_text(chat, message.id, f"**Uploaded:** **{txt}**")
            await asyncio.sleep(10)
        except:
            await asyncio.sleep(5)


def progress(current, total, message, type):
    with open(f'{message.id}{type}status.txt', "w") as fileup:
        fileup.write(f"{current * 100 / total:.1f}%")


# =========================
# Main Save Handler (with access check)
# =========================
@client.on_message(
    filters.text
    & filters.private
    & ~filters.command(["start", "help", "cancel", "login", "logout", "broadcast", "plans", "myplan", "letschat", "settopid", "clearchat"])
)
async def save(client: Client, message: Message):
    # Access Check
    access, reason = await has_access(message.from_user.id)
    if not access:
        await message.reply(
            "**❌ Free Trial Khatam Ho Gaya!**\n\n"
            "Premium plan kharido to continue kar sako.\n"
            "Neeche se plan select karo:",
            reply_markup=get_plans_keyboard()
        )
        return

    # Joining chat
    if ("https://t.me/+" in message.text or "https://t.me/joinchat/" in message.text) and not LOGIN_SYSTEM:
        if BeastXUser is None:
            await client.send_message(message.chat.id, "String Session is not Set", reply_to_message_id=message.id)
            return
        try:
            try:
                await BeastXUser.join_chat(message.text)
            except Exception as e:
                await client.send_message(message.chat.id, f"Error : {e}", reply_to_message_id=message.id)
                return
            await client.send_message(message.chat.id, "Chat Joined", reply_to_message_id=message.id)
        except UserAlreadyParticipant:
            await client.send_message(message.chat.id, "Chat already Joined", reply_to_message_id=message.id)
        except InviteHashExpired:
            await client.send_message(message.chat.id, "Invalid Link", reply_to_message_id=message.id)
        return

    if "https://t.me/" in message.text:
        if batch_temp.IS_BATCH.get(message.from_user.id) == False:
            return await message.reply_text(
                "**One Task Is Already Processing. Wait For Complete It. If You Want To Cancel This Task Then Use - /cancel**"
            )

        datas = message.text.split("/")
        temp = datas[-1].replace("?single", "").split("-")
        fromID = int(temp[0].strip())
        try:
            toID = int(temp[1].strip())
        except:
            toID = fromID

        if LOGIN_SYSTEM:
            user_data = await db.get_session(message.from_user.id)
            if user_data is None:
                await message.reply("**For Downloading Restricted Content You Have To /login First.**")
                return
            api_id = int(await db.get_api_id(message.from_user.id))
            api_hash = await db.get_api_hash(message.from_user.id)
            try:
                acc = Client("saverestricted", session_string=user_data, api_hash=api_hash, api_id=api_id)
                await acc.connect()
                        except:
                return await message.reply(
                    "**Your Login Session Expired. So /logout First Then Login Again By - /login**"
            )
                    
            else:
            if BeastXUser is None:
                await client.send_message(
                    message.chat.id,
                    "**String Session is not Set**",
                    reply_to_message_id=message.id
                )
                return
            acc = BeastXUser

        batch_temp.IS_BATCH[message.from_user.id] = False

        # Get destination
        user = await db.get_user(message.from_user.id)
        dest_chat = user.get("dest_chat") if user else None
        dest_topic = user.get("dest_topic") if user else None
        if not dest_chat:
            dest_chat = message.chat.id

        for msgid in range(fromID, toID + 1):
            if batch_temp.IS_BATCH.get(message.from_user.id):
                break

            if "https://t.me/c/" in message.text:
                chatid = int("-100" + datas[4])
                try:
                    await handle_private(client, acc, message, chatid, msgid, dest_chat, dest_topic)
                except Exception as e:
                    if ERROR_MESSAGE:
                        await client.send_message(message.chat.id, f"Error: {e}", reply_to_message_id=message.id)

            elif "https://t.me/b/" in message.text:
                username = datas[4]
                try:
                    await handle_private(client, acc, message, username, msgid, dest_chat, dest_topic)
                except Exception as e:
                    if ERROR_MESSAGE:
                        await client.send_message(message.chat.id, f"Error: {e}", reply_to_message_id=message.id)

            else:
                username = datas[3]
                try:
                    msg = await client.get_messages(username, msgid)
                except UsernameNotOccupied:
                    await client.send_message(message.chat.id, "The username is not occupied by anyone", reply_to_message_id=message.id)
                    return
                try:
                    kwargs = {"reply_to_message_id": message.id}
                    if dest_topic:
                        kwargs["message_thread_id"] = dest_topic
                    await client.copy_message(dest_chat, msg.chat.id, msg.id, **kwargs)
                except:
                    try:
                        await handle_private(client, acc, message, username, msgid, dest_chat, dest_topic)
                    except Exception as e:
                        if ERROR_MESSAGE:
                            await client.send_message(message.chat.id, f"Error: {e}", reply_to_message_id=message.id)

            await asyncio.sleep(WAITING_TIME)

        # Consume free trial if used
        if reason == "free":
            await db.increment_free(message.from_user.id)

        if LOGIN_SYSTEM:
            try:
                await acc.disconnect()
            except:
                pass
        batch_temp.IS_BATCH[message.from_user.id] = True


# =========================
# Handle Private
# =========================
async def handle_private(client: Client, acc, message: Message, chatid, msgid: int, dest_chat=None, dest_topic=None):
    msg: Message = await acc.get_messages(chatid, msgid)
    if msg.empty:
        return
    msg_type = get_message_type(msg)
    if not msg_type:
        return

    if not dest_chat:
        dest_chat = message.chat.id

    if batch_temp.IS_BATCH.get(message.from_user.id):
        return

    if "Text" == msg_type:
        try:
            kwargs = {
                "chat_id": dest_chat,
                "text": msg.text,
                "entities": msg.entities,
                "reply_to_message_id": message.id if dest_chat == message.chat.id else None,
                "parse_mode": enums.ParseMode.HTML
            }
            if dest_topic:
                kwargs["message_thread_id"] = dest_topic
            await client.send_message(**kwargs)
            return
        except Exception as e:
message(message.chat.id, f"Error: {e}", reply_to_message_id=message.id)
            return

    smsg = await client.send_message(message.chat.id, '**Downloading**', reply_to_message_id=message.id)
    asyncio.create_task(downstatus(client, f'{message.id}downstatus.txt', smsg, message.chat.id))
    try:
        file = await acc.download_media(msg, progress=progress, progress_args=[message, "down"])
        os.remove(f'{message.id}downstatus.txt')
    except Exception as e:
        if ERROR_MESSAGE:
            await client.send_message(message.chat.id, f"Error: {e}", reply_to_message_id=message.id)
        return await smsg.delete()

    if batch_temp.IS_BATCH.get(message.from_user.id):
        return

    asyncio.create_task(upstatus(client, f'{message.id}upstatus.txt', smsg, message.chat.id))

    caption = msg.caption if msg.caption else None

    send_kwargs = {
        "chat_id": dest_chat,
        "reply_to_message_id": message.id if dest_chat == message.chat.id else None,
        "parse_mode": enums.ParseMode.HTML,
        "progress": progress,
        "progress_args": [message, "up"]
    }
    if dest_topic:
        send_kwargs["message_thread_id"] = dest_topic

    if "Document" == msg_type:
        try:
            ph_path = await acc.download_media(msg.document.thumbs[0].file_id)
        except:
            ph_path = None
        try:
            await client.send_document(document=file, thumb=ph_path, caption=caption, **send_kwargs)
        except Exception as e:
            if ERROR_MESSAGE:
                await client.send_message(message.chat.id, f"Error: {e}", reply_to_message_id=message.id)
        if ph_path:
            os.remove(ph_path)

    elif "Video" == msg_type:
        try:
            ph_path = await acc.download_media(msg.video.thumbs[0].file_id)
        except:
            ph_path = None
        try:
            await client.send_video(
                video=file,
                duration=msg.video.duration,
                width=msg.video.width,
                height=msg.video.height,
                thumb=ph_path,
                caption=caption,
                **send_kwargs
            )
        except Exception as e:
            if ERROR_MESSAGE:
                await client.send_message(message.chat.id, f"Error: {e}", reply_to_message_id=message.id)
        if ph_path:
            os.remove(ph_path)

    elif "Animation" == msg_type:
        try:
            await client.send_animation(animation=file, **send_kwargs)
        except Exception as e:
            if ERROR_MESSAGE:
                await client.send_message(message.chat.id, f"Error: {e}", reply_to_message_id=message.id)

    elif "Sticker" == msg_type:
        try:
            await client.send_sticker(sticker=file, **send_kwargs)
        except Exception as e:
            if ERROR_MESSAGE:
                await client.send_message(message.chat.id, f"Error: {e}", reply_to_message_id=message.id)

    elif "Voice" == msg_type:
        try:
            await client.send_voice(voice=file, caption=caption, **send_kwargs)
        except Exception as e:
            if ERROR_MESSAGE:
                await client.send_message(message.chat.id, f"Error: {e}", reply_to_message_id=message.id)

    elif "Audio" == msg_type:
        try:
            ph_path = await acc.download_media(msg.audio.thumbs[0].file_id)
        except:
            ph_path = None
        try:
            await client.send_audio(audio=file, thumb=ph_path, caption=caption, **send_kwargs)
        except Exception as e:
            if ERROR_MESSAGE:
                await client.send_message(message.chat.id, f"Error: {e}", reply_to_message_id=message.id)
        if ph_path:
            os.remove(ph_path)

    elif "Photo" == msg_type:
        try:
            await client.send_photo(photo=file, caption=caption, **send_kwargs)
        except Exception as e:
            if ERROR_MESSAGE:
                await client.send_message(message.chat.id, f"Error: {e}", reply_to_message_id=message.id)

    if os.path.exists(f'{message.id}upstatus.txt'):
        os.remove(f'{message.id}upstatus.txt')
        if os.path.exists(file):
            os.remove(file)
    await client.delete_messages(message.chat.id, [smsg.id])


def get_message_type(msg):
    try:
        msg.document.file_id
        return "Document"
    except:
        pass
    try:
        msg.video.file_id
        return "Video"
    except:
        pass
    try:
        msg.animation.file_id
        return "Animation"
    except:
        pass
    try:
        msg.sticker.file_id
        return "Sticker"
    except:
        pass
    try:
        msg.voice.file_id
        return "Voice"
    except:
        pass
    try:
        msg.audio.file_id
        return "Audio"
    except:
        pass
    try:
        msg.photo.file_id
        return "Photo"
    except:
        pass
    try:
        msg.text
        return "Text"
    except:
        pass


# =========================
# Login / Logout
# =========================
SESSION_STRING_SIZE = 351

PLAIN_TEXT = filters.text & ~filters.regex(r"^/")
LOGIN_INPUT = PLAIN_TEXT | filters.command(["cancel"])
API_ID_INPUT = PLAIN_TEXT | filters.command(["skip", "cancel"])


async def logout(client, message):
    user_data = await db.get_session(message.from_user.id)
    if user_data is None:
        await message.reply("**You are not logged in.**")
        return
    await db.set_session(message.from_user.id, session=None)
    await db.set_api_id(message.from_user.id, api_id=None)
    await db.set_api_hash(message.from_user.id, api_hash=None)
    await message.reply("**Logout Successfully** ♦")


async def login_command(bot: Client, message: Message):
    user_data = await db.get_session(message.from_user.id)
    if user_data is not None:
        await message.reply("**You Are Already Logged In. First /logout Your Old Session. Then Do Login.**")
        return
    user_id = int(message.from_user.id)

    api_id_msg = await bot.ask(
        user_id,
        "<b>Send Your API ID.\n\nClick On /skip To Skip This Process\n\nNOTE :- If You Skip This Then Your Account Ban Chance Is High.</b>",
        filters=API_ID_INPUT
    )
    if api_id_msg.text == "/cancel":
        return await api_id_msg.reply("<b>Process cancelled.</b>")
    if api_id_msg.text == "/skip":
        api_id = API_ID
        api_hash = API_HASH
    else:
        try:
            api_id = int(api_id_msg.text)
        except ValueError:
            await api_id_msg.reply(
                "**Api id must be an integer, start your process again by /login**",
                quote=True,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔄 /login", url="https://t.me/" + (await bot.get_me()).username)]])
            )
            return
        api_hash_msg = await bot.ask(user_id, "**Now Send Me Your API HASH**", filters=LOGIN_INPUT)
        if api_hash_msg.text == "/cancel":
            return await api_hash_msg.reply("<b>Process cancelled.</b>")
        api_hash = api_hash_msg.text

    phone_number_msg = await bot.ask(
        chat_id=user_id,
        text="<b>Please send your phone number which includes country code</b>\n<b>Example:</b> <code>+13124562345, +9171828181889</code>",
        filters=LOGIN_INPUT
    )
    if phone_number_msg.text == '/cancel':
        return await phone_number_msg.reply('<b>process cancelled !</b>')
    phone_number = phone_number_msg.text
    client = Client("beastx_login", api_id=api_id, api_hash=api_hash, in_memory=True)
    await client.connect()
    await phone_number_msg.reply("Sending OTP...")
    try:
        code = await client.send_code(phone_number)
        phone_code_msg = await bot.ask(
            user_id,
            "Please check for an OTP in official telegram account. If you got it, send OTP here after reading the below format. \n\nIf OTP is `12345`, **please send it as** `1 2 3 4 5`.\n\n**Enter /cancel to cancel The Procces**",
            filters=LOGIN_INPUT,
            timeout=600
        )
    except PhoneNumberInvalid:
        await phone_number_msg.reply('`PHONE_NUMBER` **is invalid.**')
        return
    if phone_code_msg.text == '/cancel':
        return await phone_code_msg.reply('<b>process cancelled !</b>')
    try:
        phone_code = phone_code_msg.text.replace(" ", "")
        await client.sign_in(phone_number, code.phone_code_hash, phone_code)
    except PhoneCodeInvalid:
        await phone_code_msg.reply('**OTP is invalid.**')
        return
    except PhoneCodeExpired:
        await phone_code_msg.reply('**OTP is expired.**')
        return
    except SessionPasswordNeeded:
        two_step_msg = await bot.ask(
            user_id,
            '**Your account has enabled two-step verification. Please provide the password.\n\nEnter /cancel to cancel The Procces**',
            filters=LOGIN_INPUT,
            timeout=300
        )
        if two_step_msg.text == '/cancel':
            return await two_step_msg.reply('<b>process cancelled !</b>')
        try:
            password = two_step_msg.text
            await client.check_password(password=password)
        except PasswordHashInvalid:
            await two_step_msg.reply('**Invalid Password Provided**')
            return
    string_session = await client.export_session_string()
    await client.disconnect()
    if len(string_session) < SESSION_STRING_SIZE:
        return await message.reply('<b>invalid session string</b>')
    try:
        uclient = Client("beastx_verify", session_string=string_session, api_id=api_id, api_hash=api_hash, in_memory=True)
        await uclient.connect()
        await db.set_session(message.from_user.id, session=string_session)
        await db.set_api_id(message.from_user.id, api_id=api_id)
        await db.set_api_hash(message.from_user.id, api_hash=api_hash)
        try:
            await uclient.disconnect()
        except:
            pass
    except Exception as e:
        return await message.reply_text(f"<b>ERROR IN LOGIN:</b> `{e}`")
    await bot.send_message(
        message.from_user.id,
        "<b>Account Login Successfully.\n\nIf You Get Any Error Related To AUTH KEY Then /logout first and /login again</b>"
    )


# =========================
# Broadcast
# =========================
async def broadcast_messages(user_id, message):
    try:
        await message.copy(chat_id=user_id)
        return True, "Success"
    except FloodWait as e:
        await asyncio.sleep(e.value)
        return await broadcast_messages(user_id, message)
    except InputUserDeactivated:
        await db.delete_user(int(user_id))
        return False, "Deleted"
    except UserIsBlocked:
        await db.delete_user(int(user_id))
        return False, "Blocked"
    except PeerIdInvalid:
        await db.delete_user(int(user_id))
        return False, "Error"
    except Exception:
        return False, "Error"


@Client.on_message(filters.command("broadcast") & filters.user(ADMINS))
async def broadcast(bot, message):
    users = await db.get_all_users()
    b_msg = message.reply_to_message
    if not b_msg:
        return await message.reply_text("**Reply This Command To Your Broadcast Message**")
    sts = await message.reply_text(text='Broadcasting your messages...')
    start_time = time.time()
    total_users = await db.total_users_count()
    done = 0
    blocked = 0
    deleted = 0
    failed = 0
    success = 0
    async for user in users:
        if 'id' in user:
            pti, sh = await broadcast_messages(int(user['id']), b_msg)
            if pti:
                success += 1
            elif pti == False:
                if sh == "Blocked":
                    blocked += 1
                elif sh == "Deleted":
                    deleted += 1
                elif sh == "Error":
                    failed += 1
            done += 1
            if not done % 20:
                await sts.edit(
                    f"Broadcast in progress:\n\nTotal Users {total_users}\nCompleted: {done} / {total_users}\nSuccess: {success}\nBlocked: {blocked}\nDeleted: {deleted}"
                )
        else:
            done += 1
            failed += 1
    time_taken = datetime.timedelta(seconds=int(time.time() - start_time))
    await sts.edit(
        f"Broadcast Completed:\nCompleted in {time_taken} seconds.\n\nTotal Users {total_users}\nCompleted: {done} / {total_users}\nSuccess: {success}\nBlocked: {blocked}\nDeleted: {deleted}"
    )


# =========================
# Bot Class
# =========================
class Bot(Client):
    def __init__(self):
        super().__init__(
            "beastx",
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=BOT_TOKEN,
            plugins=None,
            workers=150,
            sleep_threshold=5,
        )

    async def start(self, *args, **kwargs):
        await super().start(*args, **kwargs)
        print("BeastX Bot + Payment System Started")

    async def stop(self, *args):
        await super().stop()
        print("BeastX Bot Stopped")


HEALTH_PORT = int(os.environ.get("PORT", 8000))


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass


def _start_health_server():
    server = HTTPServer(("0.0.0.0", HEALTH_PORT), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Health check server listening on port {HEALTH_PORT}")


if __name__ == "__main__":
    bot = Bot()
    _start_health_server()

    # Register handlers
    bot.add_handler(MessageHandler(send_start, filters.private & filters.command(["start"])))
    bot.add_handler(MessageHandler(send_help, filters.private & filters.command(["help"])))
    bot.add_handler(MessageHandler(send_cancel, filters.private & filters.command(["cancel"])))
    bot.add_handler(MessageHandler(logout, filters.private & filters.command(["logout"])))
    bot.add_handler(MessageHandler(login_command, filters.private & filters.command(["login"])))
    bot.add_handler(MessageHandler(show_plans_cmd, filters.private & filters.command(["plans"])))
    bot.add_handler(MessageHandler(my_plan, filters.private & filters.command(["myplan"])))
    bot.add_handler(MessageHandler(set_chat, filters.private & filters.command(["setchat"])))
    bot.add_handler(MessageHandler(set_topic, filters.private & filters.command(["settopic"])))
    bot.add_handler(MessageHandler(clear_chat, filters.private & filters.command(["clearchat"])))
    bot.add_handler(MessageHandler(
        save,
        filters.private & filters.text & ~filters.command([
            "start", "help", "cancel", "login", "logout", "broadcast",
            "plans", "myplan", "setchat", "settopic", "clearchat"
        ])
    ))
    bot.add_handler(MessageHandler(broadcast, filters.command(["broadcast"]) & filters.user(ADMINS)))

    # Callback handlers
    bot.add_handler(CallbackQueryHandler(cb_show_plans, filters.regex(r"^show_plans$")))
    bot.add_handler(CallbackQueryHandler(cb_select_plan, filters.regex(r"^plan_(\d+)_(\d+)$")))
    bot.add_handler(CallbackQueryHandler(cb_paid, filters.regex(r"^paid_(\d+)_(\d+)$")))
    bot.add_handler(CallbackQueryHandler(cb_approve, filters.regex(r"^approve_(\d+)_(\d+)$")))
    bot.add_handler(CallbackQueryHandler(cb_reject, filters.regex(r"^reject_(\d+)$")))
    bot.add_handler(CallbackQueryHandler(cb_cancel_pay, filters.regex(r"^cancel_pay$")))

    bot.run()
