import os
import asyncio
import logging
from dotenv import load_dotenv
from supabase import create_async_client, AsyncClient
from telethon import TelegramClient, events, Button

# ---- Logging Setup ----
# Get script directory for absolute logging path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")

if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

# Set root level to WARNING to avoid noise from libraries
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.WARNING,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(LOG_DIR, "broadcast_bot.log"))
    ]
)
logger = logging.getLogger("BroadcastBot")
logger.setLevel(logging.INFO)

# Silence specific libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)

load_dotenv(os.path.join(BASE_DIR, ".env"))
load_dotenv()

SUPABASE_URL = os.environ.get("VITE_SUPABASE_URL") or os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("VITE_SUPABASE_ANON_KEY") or os.environ.get("SUPABASE_KEY")
API_ID = int(os.environ.get("TELEGRAM_API_ID", "12345678"))
API_HASH = os.environ.get("TELEGRAM_API_HASH", "dummyhash")
BOT_TOKEN = os.environ.get("BROADCAST_BOT_TOKEN")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Supabase URL and Key must be defined in environment variables")

# Initialize globally as None, then await in main()
supabase: AsyncClient = None

# ---- Supabase Optimization Removed ----
# AsyncClient handles concurrency natively.

# {user_id: {'step': 'selecting_channel', 'channel_id': '...', 'channel_name': '...', 'message_data': {...}}}
user_states = {}

async def get_owner_data(tg_user_id):
    if not supabase: return None
    # Try profiles first
    res = await supabase.table('profiles').select('id').eq('telegram_user_id', tg_user_id).execute()
    data = getattr(res, 'data', [])
    if data:
        return data[0]
    
    # Try subscriptions as fallback
    res = await supabase.table('app_user_subscriptions').select('user_id').eq('telegram_user_id', tg_user_id).execute()
    data = getattr(res, 'data', [])
    if data:
        return {'id': data[0]['user_id']}
        
    return None

async def get_owner_channels(user_id):
    if not supabase: return []
    bots_res = await supabase.table('tg_tracker').select('id').eq('user_id', user_id).execute()
    bots_data = getattr(bots_res, 'data', [])
    if not bots_data:
        return []
    bot_ids = [b['id'] for b in bots_data]
    mappings_res = await supabase.table('tg_bot_channel_mappings').select('channel_id, channel_name').in_('bot_id', bot_ids).eq('status', 'Active').execute()
    mappings_data = getattr(mappings_res, 'data', [])
    return mappings_data

async def main():
    global supabase
    if not BOT_TOKEN:
        logger.error("BROADCAST_BOT_TOKEN not found in .env")
        return
    
    # Initialize the Async client
    supabase = await create_async_client(SUPABASE_URL, SUPABASE_KEY)

    # Ensure sessions directory exists relative to the script
    base_dir = os.path.dirname(os.path.abspath(__file__))
    sessions_dir = os.path.join(base_dir, "sessions")
    if not os.path.exists(sessions_dir):
        os.makedirs(sessions_dir)

    session_path = os.path.join(sessions_dir, "broadcast_master")
    
    client = TelegramClient(session_path, API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)
    logger.info("Broadcast Master Bot (@Gapgrowbot) started!")

    @client.on(events.NewMessage)
    async def global_message_handler(event):
        sender_id = event.sender_id
        if not event.is_private:
            return

        text = event.text or ""
        
        # 1. Handle commands always
        if text.startswith('/start'):
            payload = None
            if ' ' in text:
                payload = text.split(' ', 1)[1]
            
            sender = await event.get_sender()
            logger.info(f"Handling /start for {sender.id} with payload: {payload}")

            if payload and payload.lower() != "true":
                logger.info(f"Attempting to link account for UUID: {payload} with TG ID: {sender.id}")
                try:
                    await supabase.table('profiles').update({'telegram_user_id': sender.id}).eq('id', payload).execute()
                    await supabase.table('app_user_subscriptions').update({'telegram_user_id': sender.id}).eq('user_id', payload).execute()
                    await event.respond("✅ **Telegram Account Connected Successfully!**")
                    return
                except Exception as e:
                    logger.error(f"Error linking account: {e}")
                    await event.respond(f"❌ Failed to link account: {str(e)}")
                    return

            owner = await get_owner_data(sender.id)
            if not owner:
                await event.respond("🚀 **Welcome!** Please connect your account in the dashboard first.")
                return

            await event.respond(
                "🚀 **Welcome to GAP Grow Broadcast Bot**\n\n"
                "Use /send to start building your broadcast."
            )
            return

        if text.startswith('/send'):
            sender = await event.get_sender()
            owner_data = await get_owner_data(sender.id)
            if not owner_data:
                await event.respond("Owner verification failed.")
                return

            channels = await get_owner_channels(owner_data.get('id'))
            if not channels:
                await event.respond("No active channels found.")
                return

            buttons = []
            seen_ids = set()
            for ch in channels:
                cid = ch.get('channel_id')
                if cid and cid not in seen_ids:
                    buttons.append([Button.inline(ch.get('channel_name') or "Unnamed", data=f"selchan_{cid}")])
                    seen_ids.add(cid)

            user_states[sender.id] = {'step': 'selecting_channel'}
            await event.respond("Select the target channel audience:", buttons=buttons)
            return

        # 2. Handle state-based messages
        state = user_states.get(sender_id)
        if not state:
            return

        if state.get('step') == 'awaiting_message':
            media_path = None
            if event.media:
                media_dir = os.path.join(os.path.dirname(__file__), "broadcast_media")
                os.makedirs(media_dir, exist_ok=True)
                media_path = await event.download_media(file=os.path.join(media_dir, f"tmp_{event.id}"))
                logger.info(f"Downloaded media: {media_path}")

            state.update({
                'step': 'verifying',
                'original_msg': event.message,
                'media_path': media_path,
                'message_data': {
                    'text': event.text,
                    'media': event.media is not None,
                    'raw_text': event.message.message
                }
            })
            
            await event.respond("📝 **Preview of your broadcast message:**")
            preview_msg = await event.message.reply(
                f"Broadcast to audience of **{state['channel_name']}**?",
                buttons=[
                    [Button.inline("✅ Send Broadcast", data="confirm_send")],
                    [Button.inline("❌ Cancel", data="cancel_broadcast")]
                ]
            )
            state['preview_msg_id'] = preview_msg.id

    @client.on(events.CallbackQuery(data=lambda d: d.decode().startswith('selchan_')))
    async def channel_selection_handler(event):
        channel_id = event.data.decode().split('_')[1]
        sender_id = event.sender_id
        
        try:
            res = await supabase.table('tg_bot_channel_mappings').select('channel_name').eq('channel_id', channel_id).limit(1).execute()
            channel_name = res.data[0]['channel_name'] if res.data else "Unknown"
            user_states[sender_id] = {'step': 'awaiting_message', 'channel_id': channel_id, 'channel_name': channel_name}
            await event.edit(f"✅ **{channel_name}** selected. Send your message now.")
        except Exception as e:
            logger.error(f"Selection error: {e}")
            await event.answer("Error selecting channel.")

    @client.on(events.CallbackQuery(data='confirm_send'))
    async def confirm_handler(event):
        sender_id = event.sender_id
        state = user_states.get(sender_id)
        if not state or state.get('step') != 'verifying':
            await event.answer("Session expired or invalid state.")
            return

        try:
            owner_data = await get_owner_data(sender_id)
            orig_msg = state.get('original_msg')
            task_data = {
                'user_id': owner_data.get('id'),
                'channel_id': state.get('channel_id'),
                'message_data': {
                    'text': orig_msg.text if orig_msg else "",
                    'has_media': bool(orig_msg.media) if orig_msg else False,
                    'raw_text': orig_msg.message if orig_msg else "",
                    'media_path': state.get('media_path')
                },
                'status': 'pending'
            }
            await supabase.table('tg_broadcast_tasks').insert(task_data).execute()
            await event.edit("✅ **Broadcast task created!**")
            user_states.pop(sender_id, None)
        except Exception as e:
            logger.error(f"Confirm error: {e}")
            await event.edit("❌ Failed to create task.")

    @client.on(events.CallbackQuery(data='cancel_broadcast'))
    async def cancel_handler(event):
        user_states.pop(event.sender_id, None)
        await event.edit("❌ Broadcast cancelled.")

    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
