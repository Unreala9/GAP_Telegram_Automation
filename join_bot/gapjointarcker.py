import os
import asyncio
import datetime
import logging
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from supabase import create_async_client, AsyncClient
from telethon import TelegramClient, events
from telethon.tl.functions.messages import ExportChatInviteRequest
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.errors import UserNotParticipantError
from telethon.tl.custom import Button
import aiohttp

# ---- 1. Logging Setup ----
# Get script directory for absolute logging path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")

if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

# Set root level to WARNING to silence noisy libraries like httpx and telethon
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.WARNING,
    handlers=[
        logging.StreamHandler(), # Console
        logging.FileHandler(os.path.join(LOG_DIR, "bot.log"), encoding='utf-8') # File
    ]
)
# Manually set our bot's logger to INFO
logger = logging.getLogger("BotManager")
logger.setLevel(logging.INFO)

# Silence specific noisy loggers even more if needed
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)

# Load environment variables
load_dotenv()

SUPABASE_URL = os.environ.get("VITE_SUPABASE_URL") or os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("VITE_SUPABASE_ANON_KEY") or os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Supabase URL and Key must be defined in environment variables")

# We will initialize this inside the async runner
supabase: AsyncClient = None

# Dictionary to store running clients and their tasks
active_clients = {}
active_semaphores = {} # bot_id -> Semaphore
running_tasks = {}

# Dictionary to cache the bot configurations from the database (reduces DB calls drastically)
GLOBAL_BOT_CONFIGS = {} 
# Dictionary to store channel mappings for each bot
GLOBAL_CHANNEL_MAPPINGS = {} 

API_ID = int(os.environ.get("TELEGRAM_API_ID", "12345678"))
API_HASH = os.environ.get("TELEGRAM_API_HASH", "dummyhash")

# Store the main event loop globally to be accessed by realtime threads
MAIN_LOOP = None


# ---- 2. Supabase Optimization Removed ----
# AsyncClient handles its own concurrency natively.


# Ensure sessions directory exists
if not os.path.exists("sessions"):
    os.makedirs("sessions")

async def start_bot(token: str, bot_id: str):
    logger.info(f"Starting bot: {bot_id}")
    try:
        # ---- 3. Persistent Sessions ----
        # Using file-based sessions so when the server restarts or the bot disconnects,
        # it doesn't need to do completely new full reconnections every time.
        client = TelegramClient(f"sessions/bot_{bot_id}", API_ID, API_HASH)
        
        await client.start(bot_token=token)
        logger.info(f"Bot {bot_id} started successfully!")
        
        # Handler for /start <ad_id>
        @client.on(events.NewMessage(pattern=r'^/start(?: (.*))?'))
        async def on_user_joined(event):
            if not supabase: return # Added check
            
            # Smart check: If this bot has no active channel mappings, ignore /start!
            # It means this is a pure AI chatbot, not a join bot.
            mappings = GLOBAL_CHANNEL_MAPPINGS.get(bot_id, [])
            if not mappings:
                logger.info(f"Bot {bot_id}: Ignoring /start because it has no active channel mappings (pure support bot).")
                return

            payload = event.pattern_match.group(1)
            sender = await event.get_sender()
            user_id = sender.id
            
            if payload:
                if not supabase: return # Existing check
                logger.info(f"Bot {bot_id}: User {user_id} started with payload/ad_id: {payload}")
                
                # Removed the duplicate 'if not supabase: await event.respond...' block
                # 1. Fetch the bot join link configuration with its mapping
                link_res = await supabase.table('tg_bot_join_links').select('*, mapping:tg_bot_channel_mappings(*)').eq('slug', payload).eq('bot_id', bot_id).execute()
                
                if link_res.data:
                    link_config = link_res.data[0]
                    link_id = link_config['id']
                    admin_id = link_config['user_id']
                    
                    mapping = link_config.get('mapping')
                    
                    # Resilience: If join failed but we have mapping_id, fetch it explicitly
                    if not mapping and link_config.get('channel_mapping_id'):
                        try:
                            m_res = await supabase.table('tg_bot_channel_mappings').select('*').eq('id', link_config['channel_mapping_id']).execute()
                            if m_res.data:
                                mapping = m_res.data[0]
                        except: pass
                    
                    # Deep resilience: if still no mapping, take the first active one for this bot
                    if not mapping:
                        try:
                            m_res = await supabase.table('tg_bot_channel_mappings').select('*').eq('bot_id', bot_id).eq('status', 'Active').limit(1).execute()
                            if m_res.data:
                                mapping = m_res.data[0]
                        except: pass

                    channel_id = mapping.get('channel_id') if mapping else None
                    existing_invite_link = mapping.get('invite_link') if mapping else None
                    
                    # ---- 4. Check if user already joined ----
                    already_joined = False
                    channel_link_str = existing_invite_link or "https://t.me/"
                    
                    if channel_id:
                        try:
                            # 1. Handle channel_id prefixing (-100)
                            cid_str = str(channel_id)
                            if not cid_str.startswith("-100"):
                                full_channel_id = int(f"-100{cid_str}")
                            else:
                                full_channel_id = int(cid_str)
                            
                            try:
                                # Active check with Telegram API
                                participant = await client(GetParticipantRequest(channel=full_channel_id, participant=user_id))
                                if participant:
                                    already_joined = True
                            except UserNotParticipantError:
                                already_joined = False
                            except Exception as e:
                                logger.warning(f"Bot {bot_id}: Participant check failed (bot might not be admin): {e}")

                            # 2. Link Generation
                            if not existing_invite_link:
                                logger.info(f"Bot {bot_id}: No existing link found for channel {full_channel_id}. Generating...")
                                try:
                                    # Try Request-to-Join link first
                                    invite_link = await client(ExportChatInviteRequest(
                                        peer=full_channel_id,
                                        request_needed=True,
                                        title=f"GAP Join Link - {bot_id[:8]}"
                                    ))
                                    channel_link_str = invite_link.link
                                except Exception as req_err:
                                    logger.warning(f"Bot {bot_id}: RTJ link failed: {req_err}. Trying normal link...")
                                    try:
                                        invite_link = await client(ExportChatInviteRequest(peer=full_channel_id))
                                        channel_link_str = invite_link.link
                                    except Exception as e2:
                                        logger.error(f"Bot {bot_id}: Final link fallback triggered: {e2}")
                                        # Fallback to public username if available
                                        try:
                                            ent = await client.get_entity(full_channel_id)
                                            if hasattr(ent, 'username') and ent.username:
                                                channel_link_str = f"https://t.me/{ent.username}"
                                        except: pass

                                # Save the generated link back to the mapping
                                if channel_link_str and channel_link_str != "https://t.me/" and mapping and mapping.get('id'):
                                    await supabase.table('tg_bot_channel_mappings').update({"invite_link": channel_link_str}).eq('id', mapping['id']).execute()
                                    logger.info(f"Bot {bot_id}: Saved new link: {channel_link_str}")
                        except Exception as eOuter:
                            logger.error(f"Bot {bot_id}: Critical error processing channel link: {eOuter}")
                    
                    # Log the start event in bot_join_users
                    try:
                        # Check for existing record to preserve history
                        existing_res = await supabase.table('tg_bot_join_users').select('*').eq('link_id', link_id).eq('telegram_user_id', str(user_id)).execute()
                        
                        upsert_data = {
                            "user_id": admin_id,
                            "bot_id": bot_id,
                            "link_id": link_id,
                            "telegram_user_id": str(user_id),
                            "telegram_username": getattr(sender, 'username', None),
                            "telegram_first_name": getattr(sender, 'first_name', None),
                            "joined_channel": already_joined
                        }
                        
                        if already_joined:
                            # Only set joined_at if not already set or if rejoining
                            data = getattr(existing_res, 'data', [])
                            if not data or not data[0].get('joined_at'):
                                upsert_data["joined_at"] = datetime.datetime.utcnow().isoformat()
                            
                            # If they are already in the channel, they are NOT "currently left"
                            upsert_data["left_channel"] = False
                            
                        # Perform upsert
                        await supabase.table('tg_bot_join_users').upsert(upsert_data, on_conflict="link_id,telegram_user_id").execute()
                        logger.info(f"Bot {bot_id}: upserted user {user_id} record. Already joined: {already_joined}")
                    except Exception as log_err:
                        logger.error(f"Failed to log bot start: {log_err}")

                    # Construct customized message to send
                    keyboard = [[Button.url(link_config.get('button_text') or "Join Channel", channel_link_str)]]
                    has_extra = bool(link_config.get('telegram_extra_message'))

                    if link_config.get('telegram_image_url'):
                        await event.respond(
                            link_config.get('telegram_message') or "Click the button below to join the private channel.",
                            file=link_config.get('telegram_image_url'),
                            buttons=None if has_extra else keyboard
                        )
                    else:
                        await event.respond(
                            link_config.get('telegram_message') or "Click the button below to join the private channel.",
                            buttons=None if has_extra else keyboard
                        )
                        
                    # Send Extra message if configured
                    if has_extra:
                        await event.respond(link_config.get('telegram_extra_message'), buttons=keyboard)
                else:
                    await event.respond("Invalid or expired join link.")
            else:
                await event.respond("Welcome to the bot! Use a valid join link to get started.")
                
        # Handle chat member updates for join and leave tracking
        @client.on(events.ChatAction)
        async def chat_handler(event):
            try:
                # Optimized: Use global channel mappings
                mappings = GLOBAL_CHANNEL_MAPPINGS.get(bot_id, [])
                if not mappings:
                    return
                
                # Robust channel ID matching
                chat_id = event.chat_id
                chat_id_str = str(chat_id)
                is_monitored = False
                for m in mappings:
                    m_id_str = str(m['channel_id'])
                    # Match both short and long format (-100 prefix)
                    if m_id_str in chat_id_str or chat_id_str in m_id_str:
                        is_monitored = True
                        break
                
                if not is_monitored:
                    return

                # Determine Action
                is_join = getattr(event, 'user_joined', False) or getattr(event, 'user_added', False)
                is_leave = getattr(event, 'user_left', False) or getattr(event, 'user_kicked', False)

                if is_join:
                    user_event = await event.get_user()
                    if not user_event: return
                    user_tg_id = str(user_event.id)
                    logger.info(f"Bot {bot_id}: DETECTED JOIN - User {user_tg_id} in Chat {chat_id}")
                    
                    try:
                        now_iso = datetime.datetime.utcnow().isoformat()
                        # Update ALL records for this user and bot to mark them as Active
                        await supabase.table('tg_bot_join_users').update({
                            "joined_channel": True,
                            "left_channel": False,
                            "joined_at": now_iso
                        }).eq('bot_id', bot_id).eq('telegram_user_id', user_tg_id).execute()
                        logger.info(f"Bot {bot_id}: Updated record for user {user_tg_id} as ACTIVE.")
                    except Exception as log_err:
                        logger.error(f"Failed to update channel join stats: {log_err}")

                elif is_leave:
                    user_event = await event.get_user()
                    if not user_event: return
                    user_tg_id = str(user_event.id)
                    logger.info(f"Bot {bot_id}: DETECTED LEAVE - User {user_tg_id} from Chat {chat_id}")
                    
                    try:
                        now_iso = datetime.datetime.utcnow().isoformat()
                        # Update ALL records to mark them as Leaved
                        await supabase.table('tg_bot_join_users').update({
                            "left_channel": True,
                            "left_at": now_iso
                        }).eq('bot_id', bot_id).eq('telegram_user_id', user_tg_id).execute()
                        logger.info(f"Bot {bot_id}: Updated record for user {user_tg_id} as LEFT.")
                    except Exception as log_err:
                        logger.error(f"Failed to update channel leave stats: {log_err}")
                        
            except Exception as ev_err:
                logger.error(f"Error in chat handler: {ev_err}")

        # Handle channel messages to link channels
        @client.on(events.NewMessage)
        async def channel_message_handler(event):
            # Only process if the message is in a channel
            if event.is_channel and not event.is_group:
                # We now ALWAYS listen for channel messages to allow multi-channel mapping
                # even if some channels are already active.

                chat = await event.get_chat()
                full_channel_id = chat.id
                
                try:
                    channel_name = getattr(chat, 'title', f"Channel {full_channel_id}")
                    channel_username = getattr(chat, 'username', None)
                    icon_url = None

                    # Try to fetch the real channel photo using the Bot API
                    try:
                        async with aiohttp.ClientSession() as http_session:
                            api_chat_id = f"-100{full_channel_id}"
                            chat_url = f"https://api.telegram.org/bot{token}/getChat?chat_id={api_chat_id}"
                            async with http_session.get(chat_url) as resp:
                                chat_data = await resp.json()
                                if chat_data.get('ok') and 'photo' in chat_data['result']:
                                    file_id = chat_data['result']['photo']['big_file_id']
                                    
                                    file_url = f"https://api.telegram.org/bot{token}/getFile?file_id={file_id}"
                                    async with http_session.get(file_url) as file_resp:
                                        file_data = await file_resp.json()
                                        if file_data.get('ok'):
                                            file_path = file_data['result']['file_path']
                                            download_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
                                            
                                            # Download the actual image and convert to base64
                                            # Telegram file URLs expire after ~1 hour, so we must store the raw data
                                            import base64
                                            async with http_session.get(download_url) as img_resp:
                                                if img_resp.status == 200:
                                                    img_bytes = await img_resp.read()
                                                    b64_encoded = base64.b64encode(img_bytes).decode('utf-8')
                                                    # Telegram profile photos are usually JPEGs
                                                    icon_url = f"data:image/jpeg;base64,{b64_encoded}"
                                                    logger.info(f"Bot {bot_id}: Successfully converted channel image to Base64.")
                    except Exception as photo_err:
                        logger.warning(f"Bot {bot_id}: Could not fetch or convert channel photo: {photo_err}")

                    logger.info(f"Bot {bot_id}: Detected message in Channel '{channel_name}' ({full_channel_id}). Adding to list...")
                    
                    try:
                        await supabase.table('tg_bot_detected_channels').upsert({
                            'bot_id': bot_id,
                            'channel_id': str(full_channel_id),
                            'channel_name': channel_name,
                            'channel_username': channel_username,
                            'channel_icon_url': icon_url
                        }, on_conflict='bot_id,channel_id').execute()
                        logger.info(f"Bot {bot_id}: Successfully logged channel '{channel_name}' for manual mapping.")
                    except Exception as db_err:
                        logger.error(f"Bot {bot_id}: Note on DB insert: {db_err}")
                    
                except Exception as e:
                    logger.error(f"Bot {bot_id}: Error fetching channel info: {e}")

        active_clients[bot_id] = client
        active_semaphores[bot_id] = asyncio.Semaphore(10)
        
        # ---- 5. Missing state transition handling fixed ----
        # The bot will run continuously without the 5-minute timeout.
        # Since the bot_runner polls Supabase every 15 seconds, your bot instantly starts
        # behaving as 'Active' when the user maps a channel, seamlessly.
        await client.run_until_disconnected()

    except Exception as e:
        logger.error(f"Failed to start bot {bot_id}: {e}")

async def process_task(task):
    """Processes a single broadcast task immediately."""
    if not supabase: return
    try:
        task_id = task['id']
        target_channel_id = task['channel_id']
        message_data = task['message_data']
        
        # 1. Fetch channel mappings to see which bots are mapped to this channel
        mapping_res = await supabase.table('tg_bot_channel_mappings').select('*').eq('channel_id', target_channel_id).eq('status', 'Active').execute()
        mappings = getattr(mapping_res, 'data', []) or []
        
        for mapping in mappings:
            bot_id = mapping['bot_id']
            mapping_pk = mapping['id']
            
            if bot_id in active_clients:
                client = active_clients[bot_id]
                
                # Check/Initialize progress
                prog_res = await supabase.table('tg_bot_broadcast_progress').select('*').eq('task_id', task_id).eq('bot_id', bot_id).execute()
                prog_data = getattr(prog_res, 'data', [])
                if prog_data:
                    if prog_data[0]['status'] != 'pending': continue
                else:
                    await supabase.table('tg_bot_broadcast_progress').insert({'task_id': task_id, 'bot_id': bot_id, 'status': 'processing'}).execute()
                
                # Fetch target users
                # Resilience: Try specific mapping first, then all links for this bot if none found
                links_res = await supabase.table('tg_bot_join_links').select('id').eq('bot_id', bot_id).eq('channel_mapping_id', mapping_pk).execute()
                link_ids = [l['id'] for l in getattr(links_res, 'data', [])]
                
                if not link_ids:
                    # Fallback: find any link for this bot that might not have channel_mapping_id set but belongs here
                    logger.info(f"Bot {bot_id}: No recipients found for mapping {mapping_pk}. Falling back to all bot links.")
                    links_res = await supabase.table('tg_bot_join_links').select('id').eq('bot_id', bot_id).execute()
                    link_ids = [l['id'] for l in getattr(links_res, 'data', [])]

                if not link_ids:
                    await supabase.table('tg_bot_broadcast_progress').update({'status': 'completed', 'error_log': 'No links'}).eq('task_id', task_id).eq('bot_id', bot_id).execute()
                    continue
                    
                users_res = await supabase.table('tg_bot_join_users').select('telegram_user_id').in_('link_id', link_ids).execute()
                target_users = getattr(users_res, 'data', []) or []
                
                if not target_users:
                    # Deep resilience: if still no users, maybe they are linked to bot_id directly
                    await supabase.table('tg_bot_broadcast_progress').update({'status': 'completed', 'error_log': 'No users'}).eq('task_id', task_id).eq('bot_id', bot_id).execute()
                    continue

                logger.info(f"Bot {bot_id}: Starting broadcast for task {task_id} to {len(target_users)} users.")
                
                async def do_broadcast(c: TelegramClient, b_id, t_id, users, msg_data):
                    import time
                    start_time = time.time()
                    stats = {'sent': 0, 'errors': 0}
                    media_path = msg_data.get('media_path')
                    raw_text = msg_data.get('raw_text', '')
                    
                    # 1. Upload media ONCE if it exists
                    uploaded_media = None
                    if media_path and os.path.exists(media_path):
                        try:
                            logger.info(f"Bot {b_id}: Pre-uploading media {media_path}")
                            uploaded_media = await c.upload_file(media_path)
                        except Exception as e:
                            logger.error(f"Bot {b_id}: Failed to pre-upload media: {e}")
                    
                    # 2. Semaphore for controlled concurrency (shared per-client)
                    sem = active_semaphores.get(b_id) or asyncio.Semaphore(10)
                    
                    async def send_to_user(user):
                        async with sem:
                            try:
                                target_id = int(user['telegram_user_id'])
                                if uploaded_media:
                                    await c.send_message(target_id, raw_text, file=uploaded_media)
                                else:
                                    await c.send_message(target_id, raw_text)
                                stats['sent'] += 1
                                await asyncio.sleep(0.05) # Safe sleep
                            except Exception as e:
                                stats['errors'] += 1
                                logger.warning(f"Bot {b_id}: Failed to send to {user['telegram_user_id']}: {e}")

                    # 3. Process all users concurrently
                    tasks = [send_to_user(u) for u in users]
                    await asyncio.gather(*tasks)
                    
                    # 4. Clean up media
                    if media_path and os.path.exists(media_path):
                        try: os.remove(media_path)
                        except: pass
                    
                    duration = time.time() - start_time
                    await supabase.table('tg_bot_broadcast_progress').update({
                        'status': 'completed', 'sent_count': stats['sent'],
                        'total_targeted': len(users),
                        'error_log': f"Finished in {duration:.2f}s with {stats['errors']} errors"
                    }).eq('task_id', t_id).eq('bot_id', b_id).execute()
                    
                    # Also mark the task as completed if this was the last bot or we want to clear it
                    await supabase.table('tg_broadcast_tasks').update({'status': 'completed'}).eq('id', t_id).execute()
                    
                    logger.info(f"Bot {b_id}: Completed broadcast for task {t_id}. Sent: {stats['sent']}/{len(users)} in {duration:.2f}s")

                asyncio.create_task(do_broadcast(client, bot_id, task_id, target_users, message_data))
    except Exception as e:
        logger.error(f"Error processing task: {e}")

async def synchronize_bots():
    """Syncs the current running bots with the database state."""
    try:
        logger.info("Synchronizing bots with database...")
        # 1. Fetch bots
        response = await supabase.table('tg_tracker').select('*').in_('status', ['Pending', 'Active', 'pending', 'active']).execute()
        bots = getattr(response, 'data', []) or []
        
        # 2. Fetch mappings
        mapping_res = await supabase.table('tg_bot_channel_mappings').select('*').eq('status', 'Active').execute()
        all_mappings = getattr(mapping_res, 'data', []) or []
        
        current_bot_ids = set()
        for bot in bots:
            bot_id = bot['id']
            token = bot['bot_token']
            GLOBAL_BOT_CONFIGS[bot_id] = bot
            GLOBAL_CHANNEL_MAPPINGS[bot_id] = [m for m in all_mappings if m['bot_id'] == bot_id]
            current_bot_ids.add(bot_id)
            
            if bot_id not in running_tasks:
                task = asyncio.create_task(start_bot(token, bot_id))
                running_tasks[bot_id] = task
                
        # Cleanup inactive bots
        for bot_id in list(running_tasks.keys()):
            if bot_id not in current_bot_ids:
                logger.info(f"Bot {bot_id} stopping...")
                running_tasks[bot_id].cancel()
                GLOBAL_BOT_CONFIGS.pop(bot_id, None)
                GLOBAL_CHANNEL_MAPPINGS.pop(bot_id, None)
                if bot_id in active_clients:
                    client_to_stop = active_clients.pop(bot_id, None)
                    if client_to_stop:
                        await client_to_stop.disconnect()
                running_tasks.pop(bot_id, None)
                await asyncio.sleep(0.5)

        # 3. Check for any pending tasks that were missed
        tasks_res = await supabase.table('tg_broadcast_tasks').select('*').eq('status', 'pending').execute()
        pending_tasks = getattr(tasks_res, 'data', []) or []
        for task in pending_tasks:
            await process_task(task)
            
    except Exception as e:
        logger.error(f"Error in synchronization: {e}")

async def bot_runner():
    global supabase
    logger.info("Bot Manager Started (Async Realtime Mode). Setting up listeners...")
    
    # Initialize the Async client
    supabase = await create_async_client(SUPABASE_URL, SUPABASE_KEY)
    
    # 1. Initial full synchronization
    await synchronize_bots()
    
    # 2. Setup Supabase Realtime Listeners
    try:
        def on_realtime_event(payload):
            try:
                table = None
                event_type = None
                record = None

                # Supabase Realtime wraps the actual data inside a 'data' key
                if isinstance(payload, dict) and 'data' in payload:
                    inner = payload['data']
                    table = inner.get('table')
                    raw_type = inner.get('type')
                    # type is an enum like <RealtimePostgresChangesListenEvent.Insert: 'INSERT'>
                    if raw_type is not None:
                        event_type = str(raw_type).split("'")[1] if "'" in str(raw_type) else str(raw_type)
                    record = inner.get('record')
                elif hasattr(payload, 'table'):
                    table = getattr(payload, 'table', None)
                    event_type = getattr(payload, 'event_type', getattr(payload, 'eventType', None))
                    record = getattr(payload, 'new', None)
                elif isinstance(payload, dict):
                    table = payload.get('table')
                    event_type = payload.get('eventType') or payload.get('event_type')
                    record = payload.get('new')
                else:
                    logger.warning(f"Unknown payload type: {type(payload)}")
                    return

                if not table or not event_type:
                    logger.warning(f"Realtime: Could not parse table/event from payload: {payload}")
                    return
                    
                logger.info(f"Realtime Event: {event_type} on {table}")
                
                if MAIN_LOOP:
                    if table == 'tg_broadcast_tasks' and event_type.upper() == 'INSERT':
                        asyncio.run_coroutine_threadsafe(process_task(record), MAIN_LOOP)
                    elif table in ['tg_tracker', 'tg_bot_channel_mappings']:
                        asyncio.run_coroutine_threadsafe(synchronize_bots(), MAIN_LOOP)
            except Exception as e:
                logger.error(f"Error in on_realtime_event: {e}")

        # Create a channel for all relevant database changes
        channel = supabase.channel('db-changes')
        
        # Listen for new broadcast tasks
        channel.on_postgres_changes(
            event="INSERT",
            schema="public",
            table="tg_broadcast_tasks",
            callback=on_realtime_event
        )
        
        # Listen for bot changes
        channel.on_postgres_changes(
            event="*",
            schema="public",
            table="tg_tracker",
            callback=on_realtime_event
        )
        
        # Listen for mapping changes
        channel.on_postgres_changes(
            event="*",
            schema="public",
            table="tg_bot_channel_mappings",
            callback=on_realtime_event
        )
        await channel.subscribe()
        
        logger.info("Realtime subscriptions active. Monitoring for database changes...")
        
    except Exception as rt_err:
        logger.error(f"Failed to setup Realtime: {rt_err}. Falling back to periodic sync.")

    # 3. Keep-alive and Periodic "Safety" Sync
    # Even with Realtime, it's good practice to sync every few minutes in case of network drops
    while True:
        try:
            await asyncio.sleep(30) # Sync every 30 seconds as safety fallback
            await synchronize_bots()
        except Exception as e:
            logger.error(f"Safety sync failed: {e}")

if __name__ == "__main__":
    if not os.environ.get("TELEGRAM_API_ID"):
        logger.warning("TELEGRAM_API_ID and TELEGRAM_API_HASH are not set in .env. Bots won't connect unless set.")
    try:
        MAIN_LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(MAIN_LOOP)
        MAIN_LOOP.run_until_complete(bot_runner())
    except KeyboardInterrupt:
        logger.info("Bot Manager manually stopped.")