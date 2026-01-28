import os
import re
import asyncio
import json
import time
from datetime import datetime
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
from telethon.tl.types import MessageService, MessageMediaWebPage, MessageMediaUnsupported
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)
from aiohttp import web

# ==================== ENVIRONMENT VARIABLES ====================
load_dotenv()
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID"))
TARGET_CHANNEL = int(os.getenv("TARGET_CHANNEL"))
PORT = int(os.getenv("PORT", 8000))
DATABASE_FILE = "monitoring_channels.json"
TEMP_DIR = "temp_media"

# ==================== REGEX PATTERNS ====================
MESSAGE_REGEX = r"https://t\.me/(?:c/)?([\w\d_]+)/(\d+)"

# ==================== CREATE TEMP DIRECTORY ====================
if not os.path.exists(TEMP_DIR):
    os.makedirs(TEMP_DIR)
    print(f"✅ Created temp directory: {TEMP_DIR}")

# ==================== USERBOT ====================
userbot = TelegramClient(
    StringSession(SESSION_STRING),
    API_ID,
    API_HASH
)

# ==================== GLOBAL STATE ====================
monitoring_channels = {}
user_sessions = {}

# ==================== DATABASE FUNCTIONS ====================
def load_monitoring_db():
    """Load monitored channels from JSON"""
    if os.path.exists(DATABASE_FILE):
        try:
            with open(DATABASE_FILE, 'r') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_monitoring_db(data):
    """Save monitored channels to JSON"""
    with open(DATABASE_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def add_monitoring_channel(chat_id, chat_name, last_msg_id):
    """Add channel to monitoring database"""
    db = load_monitoring_db()
    db[str(chat_id)] = {
        "name": chat_name,
        "added_at": datetime.now().isoformat(),
        "last_msg_id": last_msg_id
    }
    save_monitoring_db(db)

# ==================== HEALTH CHECK SERVER ====================
async def health_check(request):
    """Health check endpoint"""
    return web.Response(text="OK", status=200)

async def start_health_server():
    """Start health check server"""
    app = web.Application()
    app.router.add_get("/health", health_check)
    app.router.add_get("/", health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    print(f"✅ Health check server running on port {PORT}")

# ==================== EXTRACT MESSAGE IDS FROM LINKS ====================
async def get_message_ids(link: str) -> tuple:
    """Extract chat_id and message_id from link"""
    msg_match = re.search(MESSAGE_REGEX, link)
    if msg_match:
        chat = msg_match.group(1)
        msg_id = int(msg_match.group(2))
        
        if chat.isdigit():
            chat_id = int("-100" + chat)
        else:
            chat_id = chat
        
        try:
            entity = await userbot.get_entity(chat_id)
            return entity.id, entity.title, msg_id
        except Exception as e:
            return None, f"Error: {e}", None
    
    return None, "Invalid link format", None

# ==================== DOWNLOAD AND UPLOAD ====================
async def download_and_upload_media(source_chat_id: int, msg_id: int, temp_dir: str) -> bool:
    """Download media from source, upload to target, delete temp"""
    temp_file = None
    try:
        # Get message
        msg = await userbot.get_messages(source_chat_id, ids=msg_id)
        
        if not msg or not msg.media:
            return False
        
        # Generate temp file path
        timestamp = int(time.time() * 1000)
        temp_file = os.path.join(temp_dir, f"media_{msg_id}_{timestamp}.tmp")
        
        # Download
        print(f"  📥 Downloading #{msg_id}...")
        await userbot.download_media(msg.media, file=temp_file)
        
        if not os.path.exists(temp_file):
            print(f"  ❌ Download failed - file not created")
            return False
        
        # Upload from file path (not media object)
        print(f"  📤 Uploading #{msg_id}...")
        await userbot.send_file(
            TARGET_CHANNEL,
            temp_file,
            caption=""
        )
        
        # Delete temp file
        if os.path.exists(temp_file):
            os.remove(temp_file)
            print(f"  🗑️  Deleted temp file")
        
        return True
        
    except Exception as e:
        print(f"  ❌ Error: {e}")
        # Cleanup on error
        if temp_file and os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except:
                pass
        return False

# ==================== DOWNLOAD-UPLOAD RANGE ====================
async def download_upload_range(chat_id: int, chat_name: str, start_id: int, end_id: int, status_msg):
    """Download & upload media from start_id to end_id"""
    
    if start_id > end_id:
        start_id, end_id = end_id, start_id
    
    total_uploaded = 0
    total_skipped = 0
    message_id = start_id
    
    await status_msg.edit_text(
        f"📥 **Download & Upload Started**\n"
        f"📢 {chat_name}\n"
        f"🆔 `{chat_id}`\n\n"
        f"📍 Range: #{start_id} → #{end_id}\n"
        f"🚀 Progress: 0 uploaded..."
    )
    
    while message_id <= end_id:
        try:
            # Get message info
            msg = await userbot.get_messages(chat_id, ids=message_id)
            
            if msg is None or isinstance(msg, MessageService):
                message_id += 1
                total_skipped += 1
                continue
            
            if not msg.media:
                message_id += 1
                total_skipped += 1
                continue
            
            if isinstance(msg.media, (MessageMediaWebPage, MessageMediaUnsupported)):
                message_id += 1
                total_skipped += 1
                continue
            
            # Check media type
            if msg.photo or msg.document or msg.video:
                # Download and upload
                success = await download_and_upload_media(chat_id, message_id, TEMP_DIR)
                
                if success:
                    total_uploaded += 1
                    print(f"✅ Uploaded #{message_id}")
                else:
                    total_skipped += 1
                    print(f"⚠️ Skipped #{message_id}")
            else:
                total_skipped += 1
            
            # Update status every 10 messages
            if message_id % 10 == 0:
                try:
                    await status_msg.edit_text(
                        f"⏳ **Processing...**\n"
                        f"📢 {chat_name}\n"
                        f"📍 Current: #{message_id}/{end_id}\n"
                        f"✅ Uploaded: {total_uploaded}"
                    )
                except:
                    pass
            
            # Telegram ToS delay
            await asyncio.sleep(5)
            message_id += 1
            
        except FloodWaitError as e:
            await status_msg.edit_text(
                f"⏳ **Telegram Rate Limited**\n"
                f"⏰ Waiting {e.seconds} seconds...\n\n"
                f"✅ Uploaded: {total_uploaded}\n"
                f"📍 Resuming at: #{message_id}"
            )
            print(f"⏳ FloodWait {e.seconds}s - Waiting...")
            await asyncio.sleep(e.seconds)
            print(f"✅ Resuming...")
            
        except Exception as e:
            print(f"❌ Error at #{message_id}: {e}")
            message_id += 1
            total_skipped += 1
    
    await status_msg.edit_text(
        f"✅ **Complete!**\n\n"
        f"📢 {chat_name}\n"
        f"✅ Uploaded: {total_uploaded}\n"
        f"⏭️ Skipped: {total_skipped}\n"
        f"📍 Range: #{start_id} → #{end_id}"
    )
    
    return chat_id, chat_name, end_id

# ==================== MONITOR CHANNEL ====================
async def monitor_channel_for_new_media(chat_id: int, chat_name: str, last_msg_id: int):
    """Monitor channel for new media"""
    
    print(f"\n{'='*60}")
    print(f"🔔 MONITORING: {chat_name}")
    print(f"{'='*60}\n")
    
    current_last_id = last_msg_id
    
    while True:
        try:
            if chat_id not in monitoring_channels:
                print(f"❌ STOPPED: {chat_name}\n")
                break
            
            messages = await userbot.get_messages(chat_id, limit=1)
            if messages:
                latest_msg_id = messages[0].id
                
                if latest_msg_id > current_last_id:
                    new_count = 0
                    
                    for msg_id in range(current_last_id + 1, latest_msg_id + 1):
                        if chat_id not in monitoring_channels:
                            break
                        
                        try:
                            msg = await userbot.get_messages(chat_id, ids=msg_id)
                            
                            if msg and msg.media:
                                if not isinstance(msg.media, (MessageMediaWebPage, MessageMediaUnsupported)):
                                    if msg.photo or msg.document or msg.video:
                                        # Download and upload
                                        success = await download_and_upload_media(chat_id, msg_id, TEMP_DIR)
                                        
                                        if success:
                                            media_type = '📷' if msg.photo else '📄' if msg.document else '🎬'
                                            print(f"🚀 NEW MEDIA! #{msg_id} {media_type}")
                                            new_count += 1
                                        
                                        # 2 second delay
                                        await asyncio.sleep(2)
                        
                        except FloodWaitError as e:
                            print(f"⏳ FloodWait {e.seconds}s...")
                            await asyncio.sleep(e.seconds)
                        except Exception as e:
                            print(f"❌ Error #{msg_id}: {e}")
                    
                    current_last_id = latest_msg_id
                    
                    if new_count > 0:
                        print(f"✅ Processed: {new_count} new media\n")
            
            await asyncio.sleep(10)
            
        except Exception as e:
            print(f"❌ Monitor error: {e}")
            await asyncio.sleep(10)

# ==================== BOT COMMANDS ====================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command"""
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ Unauthorized")
        return
    
    await update.message.reply_text(
        "👋 **Telegram Media Download-Upload Bot**\n\n"
        "📝 **How to use:**\n"
        "1. Send source channel link\n"
        "2. Send start message link\n"
        "3. Send end message link\n"
        "4. Bot downloads & uploads media\n"
        "5. Auto-monitoring starts\n\n"
        "📋 **Commands:**\n"
        "/channels - View monitored channels\n\n"
        "**Works with:**\n"
        "✅ Restricted channels\n"
        "✅ Protected chats\n"
        "✅ Normal channels\n"
        "✅ Private groups"
    )

async def channels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show monitored channels"""
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("⛔ Unauthorized")
        return
    
    db = load_monitoring_db()
    
    if not db:
        await update.message.reply_text("❌ No channels monitored")
        return
    
    text = "📊 **Monitoring:**\n\n"
    
    for chat_id, data in db.items():
        chat_id_int = int(chat_id)
        status = "🟢" if chat_id_int in monitoring_channels else "⚪"
        
        text += f"{status} **{data['name']}**\n"
        text += f"   🆔 `{chat_id}`\n"
        text += f"   📍 Last: #{data['last_msg_id']}\n"
        text += f"   📅 {data['added_at'][:10]}\n\n"
    
    await update.message.reply_text(text)

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle user input"""
    if update.effective_user.id != OWNER_ID:
        return
    
    user_id = update.effective_user.id
    link = update.message.text.strip()
    
    if user_id not in user_sessions:
        user_sessions[user_id] = {"step": 0}
    
    session = user_sessions[user_id]
    
    # STEP 1: Source channel
    if session["step"] == 0:
        processing = await update.message.reply_text("🔍 Extracting channel...")
        chat_id, result, msg_id = await get_message_ids(link)
        
        if not chat_id:
            await processing.edit_text(f"❌ Invalid link")
            return
        
        session["source_chat_id"] = chat_id
        session["source_chat_name"] = result
        session["step"] = 1
        
        await processing.edit_text(
            f"✅ Channel: {result}\n\n"
            f"Send START message link"
        )
    
    # STEP 2: Start message
    elif session["step"] == 1:
        chat_id, result, start_msg_id = await get_message_ids(link)
        
        if not start_msg_id:
            await update.message.reply_text("❌ Invalid message ID")
            return
        
        session["start_msg_id"] = start_msg_id
        session["step"] = 2
        
        await update.message.reply_text(
            f"✅ Start: #{start_msg_id}\n\n"
            f"Send END message link"
        )
    
    # STEP 3: End message
    elif session["step"] == 2:
        chat_id, result, end_msg_id = await get_message_ids(link)
        
        if not end_msg_id:
            await update.message.reply_text("❌ Invalid message ID")
            return
        
        source_chat_id = session["source_chat_id"]
        source_chat_name = session["source_chat_name"]
        start_msg_id = session["start_msg_id"]
        
        # Start download-upload
        status_msg = await update.message.reply_text(
            f"⏳ **Starting...**\n\n"
            f"📢 {source_chat_name}\n"
            f"📍 #{start_msg_id} → #{end_msg_id}"
        )
        
        # Download & upload
        final_chat_id, final_chat_name, final_last_id = await download_upload_range(
            source_chat_id, 
            source_chat_name, 
            start_msg_id, 
            end_msg_id, 
            status_msg
        )
        
        # Add to monitoring
        add_monitoring_channel(final_chat_id, final_chat_name, final_last_id)
        
        # Start monitoring
        if final_chat_id not in monitoring_channels:
            monitoring_channels[final_chat_id] = {
                "name": final_chat_name,
                "last_msg_id": final_last_id,
                "task": None
            }
            
            task = asyncio.create_task(
                monitor_channel_for_new_media(final_chat_id, final_chat_name, final_last_id)
            )
            monitoring_channels[final_chat_id]["task"] = task
            
            await status_msg.edit_text(
                f"✅ **Complete!**\n\n"
                f"📢 {final_chat_name}\n"
                f"📍 #{start_msg_id} → #{end_msg_id}\n\n"
                f"🔔 Monitoring..."
            )
        
        user_sessions[user_id] = {"step": 0}

# ==================== START USERBOT ====================
async def start_userbot():
    """Start userbot"""
    await userbot.start()
    me = await userbot.get_me()
    print(f"✅ UserBot: {me.first_name} (@{me.username or 'no username'})")

# ==================== RESTORE MONITORING ====================
async def restore_monitoring():
    """Restore monitoring on startup"""
    db = load_monitoring_db()
    
    for chat_id, data in db.items():
        chat_id_int = int(chat_id)
        
        if chat_id_int not in monitoring_channels:
            monitoring_channels[chat_id_int] = {
                "name": data["name"],
                "last_msg_id": data["last_msg_id"],
                "task": None
            }
            
            task = asyncio.create_task(
                monitor_channel_for_new_media(chat_id_int, data["name"], data["last_msg_id"])
            )
            monitoring_channels[chat_id_int]["task"] = task
            
            print(f"✅ Monitoring: {data['name']}")

# ==================== MAIN ====================
async def main():
    await start_health_server()
    await start_userbot()
    await restore_monitoring()
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("channels", channels_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    
    print("✅ Bot started!")
    
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    
    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, SystemExit):
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())
