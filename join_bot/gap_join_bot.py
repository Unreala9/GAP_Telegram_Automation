import os
import asyncio
import datetime
import logging
import random
import string
import base64
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from supabase import create_async_client, AsyncClient
from telethon import TelegramClient, events
from telethon.tl.functions.messages import ExportChatInviteRequest, GetExportedChatInvitesRequest
from telethon.tl.functions.channels import GetParticipantRequest, GetParticipantsRequest
from telethon.tl.types import (
    UpdateBotChatInviteRequester,
    ChannelParticipantsAdmins,
    UpdateChannelParticipant,
    UpdateChatParticipantAdmin,
    UpdateChatParticipant
)
from telethon.errors import UserNotParticipantError
from telethon.tl.custom import Button
import aiohttp

# ---- 1. Logging Setup ----
# Get script directory for absolute logging path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(BASE_DIR, "bot.log")

# Set root level to WARNING to silence noisy libraries like httpx and telethon
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.WARNING,
    handlers=[
        logging.StreamHandler(), # Console
        logging.FileHandler(LOG_FILE, encoding='utf-8') # File
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

# Dictionary to cache the bot configurations from the database
GLOBAL_BOT_CONFIGS = {} 
# Dictionary to store channel mappings for each bot
GLOBAL_CHANNEL_MAPPINGS = {}
# Dictionary to cache when a channel was last detected to avoid spam (bot_id_channel_id -> datetime)
CHANNEL_LAST_DETECTED = {}

API_ID = int(os.environ.get("TELEGRAM_API_ID", "12345678"))
API_HASH = os.environ.get("TELEGRAM_API_HASH", "dummyhash")

# Store the main event loop globally to be accessed by realtime threads
MAIN_LOOP = None

# Ensure sessions directory exists
if not os.path.exists("sessions"):
    os.makedirs("sessions")


def render_template(template_str: str, user_name: str = None, username: str = None, channel_name: str = None) -> str:
    """
    Safely replaces {first_name}, {name}, {username}, and {channel_name} in templates.
    Never throws exceptions even if values are None.
    """
    if not template_str:
        return ""
    
    name = (user_name or "there").strip()
    u_name = f"@{username.lstrip('@')}" if username else name
    ch_name = (channel_name or "our channel").strip()
    
    return str(template_str)\
        .replace("{first_name}", name)\
        .replace("{name}", name)\
        .replace("{username}", u_name)\
        .replace("{channel_name}", ch_name)


async def fetch_channel_photo_b64(token: str, channel_id: int or str) -> str or None:
    """Fetches the channel profile photo via Telegram Bot API and returns it as a Base64 data URL."""
    try:
        cid_str = str(channel_id)
        api_chat_id = cid_str if cid_str.startswith("-100") else f"-100{cid_str}"
        async with aiohttp.ClientSession() as http_session:
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
                            async with http_session.get(download_url) as img_resp:
                                if img_resp.status == 200:
                                    img_bytes = await img_resp.read()
                                    b64_encoded = base64.b64encode(img_bytes).decode('utf-8')
                                    return f"data:image/jpeg;base64,{b64_encoded}"
    except Exception as photo_err:
        logger.warning(f"Could not fetch or convert channel photo for {channel_id}: {photo_err}")
    return None


async def fetch_and_sync_channel_invites(client: TelegramClient, bot_id: str, channel_id: int or str, mapping_id: str = None, user_id: str = None):
    """
    Auto-fetches the channel's Primary Invite Link and creates/retrieves a dedicated
    Request-to-Join tracking link, syncing them to tg_bot_join_links.
    """
    if not supabase:
        return []

    try:
        cid_str = str(channel_id)
        full_channel_id = cid_str if cid_str.startswith("-100") else f"-100{cid_str}"

        logger.info(f"Bot {bot_id}: Starting invite links sync for channel {full_channel_id}...")

        # If mapping_id or user_id is missing, look them up
        if not user_id or not mapping_id:
            m_res = await supabase.table('tg_bot_channel_mappings').select('*').eq('bot_id', bot_id).execute()
            m_data = getattr(m_res, 'data', []) or []
            for m in m_data:
                m_cid_str = str(m.get('channel_id', ''))
                if m_cid_str in cid_str or cid_str in m_cid_str:
                    mapping_id = m.get('id')
                    break

        token = None
        if not user_id or not token:
            b_res = await supabase.table('tg_tracker').select('user_id, bot_token').eq('id', bot_id).execute()
            b_data = getattr(b_res, 'data', []) or []
            if b_data:
                user_id = user_id or b_data[0].get('user_id')
                token = b_data[0].get('bot_token')

        if not user_id or not token:
            logger.warning(f"Bot {bot_id}: Cannot sync invite links without user_id or token.")
            return []

        fetched_invites = []
        seen_links = set()

        async with aiohttp.ClientSession() as session:
            # 1. Fetch official Primary Invite Link via getChat
            try:
                async with session.get(f"https://api.telegram.org/bot{token}/getChat?chat_id={full_channel_id}") as resp:
                    chat_data = await resp.json()
                    if chat_data.get('ok') and chat_data.get('result', {}).get('invite_link'):
                        p_link = chat_data['result']['invite_link']
                        seen_links.add(p_link)
                        fetched_invites.append({
                            'link': p_link,
                            'title': "Primary Channel Link",
                            'request_needed': False
                        })
            except Exception as e_gc:
                logger.warning(f"Bot {bot_id}: getChat invite link note: {e_gc}")

            # 2. Get or Create dedicated Request-to-Join Link
            existing_rtj = None
            if mapping_id:
                db_res = await supabase.table('tg_bot_join_links').select('*').eq('channel_mapping_id', mapping_id).eq('is_request_needed', True).execute()
                if db_res.data:
                    existing_rtj = db_res.data[0].get('invite_link')
                    if existing_rtj and existing_rtj not in seen_links:
                        seen_links.add(existing_rtj)
                        fetched_invites.append({
                            'link': existing_rtj,
                            'title': db_res.data[0].get('name') or "Auto Join Request Link",
                            'request_needed': True
                        })

            if not existing_rtj:
                try:
                    payload = {
                        "chat_id": full_channel_id,
                        "name": "Auto Join Request Link",
                        "creates_join_request": True
                    }
                    async with session.post(f"https://api.telegram.org/bot{token}/createChatInviteLink", json=payload) as resp:
                        c_data = await resp.json()
                        if c_data.get('ok') and c_data.get('result', {}).get('invite_link'):
                            rtj_link = c_data['result']['invite_link']
                            if rtj_link not in seen_links:
                                seen_links.add(rtj_link)
                                fetched_invites.append({
                                    'link': rtj_link,
                                    'title': "Auto Join Request Link",
                                    'request_needed': True
                                })
                except Exception as e_cr:
                    logger.warning(f"Bot {bot_id}: createChatInviteLink note: {e_cr}")

        logger.info(f"Bot {bot_id}: Found {len(fetched_invites)} invite link(s) to sync for channel {full_channel_id}.")

        # 3. Save / Upsert to tg_bot_join_links table
        for inv in fetched_invites:
            link_url = inv['link']
            try:
                existing_res = await supabase.table('tg_bot_join_links')\
                    .select('id, name')\
                    .eq('bot_id', bot_id)\
                    .eq('invite_link', link_url)\
                    .execute()
                existing_data = getattr(existing_res, 'data', []) or []

                if not existing_data:
                    # Check if an auto-fetched link already exists for this mapping with matching type
                    existing_by_map = None
                    if mapping_id:
                        map_check = await supabase.table('tg_bot_join_links')\
                            .select('id, name')\
                            .eq('channel_mapping_id', mapping_id)\
                            .eq('is_auto_fetched', True)\
                            .eq('is_request_needed', inv.get('request_needed', False))\
                            .execute()
                        existing_by_map = getattr(map_check, 'data', []) or []

                    if existing_by_map and len(existing_by_map) > 0:
                        await supabase.table('tg_bot_join_links').update({
                            "invite_link": link_url,
                            "name": inv['title']
                        }).eq('id', existing_by_map[0]['id']).execute()
                        logger.info(f"Bot {bot_id}: Updated existing auto-fetched link ID {existing_by_map[0]['id']} with URL: {link_url}")
                    else:
                        slug = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
                        insert_payload = {
                            "user_id": user_id,
                            "bot_id": bot_id,
                            "channel_mapping_id": mapping_id,
                            "name": inv['title'],
                            "slug": slug,
                            "invite_link": link_url,
                            "is_auto_fetched": True,
                            "is_request_needed": inv.get('request_needed', False),
                            "telegram_message": "Click the button below to join the private channel.",
                            "button_text": "Join Channel"
                        }
                        await supabase.table('tg_bot_join_links').insert(insert_payload).execute()
                        logger.info(f"Bot {bot_id}: Synced invite link: {link_url} ('{inv['title']}')")
            except Exception as db_link_err:
                logger.error(f"Bot {bot_id}: Failed to save invite link {link_url}: {db_link_err}")

        # 4. Update mapping
        if mapping_id:
            try:
                now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
                upd_payload = {
                    'last_links_synced_at': now_iso,
                    'sync_links_requested': False
                }
                if fetched_invites:
                    upd_payload['invite_link'] = fetched_invites[0]['link']
                await supabase.table('tg_bot_channel_mappings').update(upd_payload).eq('id', mapping_id).execute()
            except Exception as m_upd_err:
                logger.error(f"Bot {bot_id}: Note updating mapping sync timestamp: {m_upd_err}")

        return fetched_invites

    except Exception as top_err:
        logger.error(f"Bot {bot_id}: Error in fetch_and_sync_channel_invites: {top_err}")
        return []

    except Exception as top_err:
        logger.error(f"Bot {bot_id}: Error in fetch_and_sync_channel_invites: {top_err}")
        return []


async def start_bot(token: str, bot_id: str):
    logger.info(f"Starting bot: {bot_id}")
    try:
        # File-based persistent sessions
        client = TelegramClient(f"sessions/bot_{bot_id}", API_ID, API_HASH)
        
        await client.start(bot_token=token)
        logger.info(f"Bot {bot_id} started successfully!")
        
        # 1. On startup, auto-sync invite links for any existing active channel mappings
        async def initial_channel_sync():
            await asyncio.sleep(2)
            try:
                m_res = await supabase.table('tg_bot_channel_mappings').select('*').eq('bot_id', bot_id).eq('status', 'Active').execute()
                active_maps = getattr(m_res, 'data', []) or []
                for m in active_maps:
                    cid = m.get('channel_id')
                    if cid:
                        await fetch_and_sync_channel_invites(client, bot_id, cid, m.get('id'))
            except Exception as init_err:
                logger.debug(f"Bot {bot_id}: Initial sync note: {init_err}")

        asyncio.create_task(initial_channel_sync())

        # Handler for /start <slug>
        @client.on(events.NewMessage(pattern=r'^/start(?: (.*))?'))
        async def on_user_joined(event):
            if not supabase: return
            
            # If this bot has no active channel mappings, ignore /start
            mappings = GLOBAL_CHANNEL_MAPPINGS.get(bot_id, [])
            if not mappings:
                logger.info(f"Bot {bot_id}: Ignoring /start because it has no active channel mappings.")
                return

            payload = event.pattern_match.group(1)
            sender = await event.get_sender()
            user_id = sender.id
            
            if payload:
                logger.info(f"Bot {bot_id}: User {user_id} started with payload/slug: {payload}")
                
                # Fetch the bot join link configuration with its mapping
                link_res = await supabase.table('tg_bot_join_links').select('*, mapping:tg_bot_channel_mappings(*)').eq('slug', payload).eq('bot_id', bot_id).execute()
                
                if link_res.data:
                    link_config = link_res.data[0]
                    link_id = link_config['id']
                    admin_id = link_config['user_id']
                    mapping = link_config.get('mapping')
                    
                    if not mapping and link_config.get('channel_mapping_id'):
                        try:
                            m_res = await supabase.table('tg_bot_channel_mappings').select('*').eq('id', link_config['channel_mapping_id']).execute()
                            if m_res.data:
                                mapping = m_res.data[0]
                        except: pass
                    
                    if not mapping:
                        try:
                            m_res = await supabase.table('tg_bot_channel_mappings').select('*').eq('bot_id', bot_id).eq('status', 'Active').limit(1).execute()
                            if m_res.data:
                                mapping = m_res.data[0]
                        except: pass

                    channel_id = mapping.get('channel_id') if mapping else None
                    existing_invite_link = link_config.get('invite_link') or (mapping.get('invite_link') if mapping else None)
                    
                    already_joined = False
                    channel_link_str = existing_invite_link or "https://t.me/"
                    
                    if channel_id:
                        try:
                            cid_str = str(channel_id)
                            full_channel_id = int(cid_str if cid_str.startswith("-100") else f"-100{cid_str}")
                            
                            try:
                                participant = await client(GetParticipantRequest(channel=full_channel_id, participant=user_id))
                                if participant:
                                    already_joined = True
                            except UserNotParticipantError:
                                already_joined = False
                            except Exception as e:
                                logger.warning(f"Bot {bot_id}: Participant check note: {e}")

                            # Fallback link generation if not present
                            if not channel_link_str or channel_link_str == "https://t.me/":
                                try:
                                    invite_link = await client(ExportChatInviteRequest(
                                        peer=full_channel_id,
                                        request_needed=True,
                                        title=f"GAP Join Link - {bot_id[:8]}"
                                    ))
                                    channel_link_str = invite_link.link
                                except Exception as req_err:
                                    try:
                                        invite_link = await client(ExportChatInviteRequest(peer=full_channel_id))
                                        channel_link_str = invite_link.link
                                    except Exception as e2:
                                        try:
                                            ent = await client.get_entity(full_channel_id)
                                            if hasattr(ent, 'username') and ent.username:
                                                channel_link_str = f"https://t.me/{ent.username}"
                                        except: pass

                                if channel_link_str and channel_link_str != "https://t.me/" and mapping and mapping.get('id'):
                                    await supabase.table('tg_bot_channel_mappings').update({"invite_link": channel_link_str}).eq('id', mapping['id']).execute()
                        except Exception as eOuter:
                            logger.error(f"Bot {bot_id}: Critical error processing channel link: {eOuter}")
                    
                    # Log user start event in tg_bot_join_users
                    try:
                        existing_res = await supabase.table('tg_bot_join_users').select('*').eq('link_id', link_id).eq('telegram_user_id', str(user_id)).execute()
                        existing_data = getattr(existing_res, 'data', []) or []
                        current_status = existing_data[0].get('status') if existing_data else None
                        
                        upsert_data = {
                            "user_id": admin_id,
                            "bot_id": bot_id,
                            "link_id": link_id,
                            "telegram_user_id": str(user_id),
                            "telegram_username": getattr(sender, 'username', None),
                            "telegram_first_name": getattr(sender, 'first_name', None),
                            "joined_channel": already_joined,
                            "last_reminded_at": None,
                        }
                        
                        if already_joined:
                            upsert_data["status"] = "active"
                            upsert_data["left_channel"] = False
                            if not existing_data or not existing_data[0].get('joined_at'):
                                upsert_data["joined_at"] = datetime.datetime.utcnow().isoformat()
                        else:
                            if current_status not in ('pending', 'active', 'leaved'):
                                upsert_data["status"] = "bot_started"
                            
                        await supabase.table('tg_bot_join_users').upsert(upsert_data, on_conflict="link_id,telegram_user_id").execute()
                    except Exception as log_err:
                        logger.error(f"Failed to log bot start: {log_err}")

                    # Dynamic user & channel details
                    user_first_name = getattr(sender, 'first_name', None) or "there"
                    user_tg_username = getattr(sender, 'username', None)
                    resolved_ch_name = (mapping.get('channel_name') if mapping else None) or "the channel"

                    # Dynamic default message if none set in DB
                    raw_welcome_msg = link_config.get('telegram_message') or (
                        "👋 **Hello {first_name}!**\n\n"
                        "Click the button below to join **{channel_name}**."
                    )
                    rendered_welcome_msg = render_template(
                        raw_welcome_msg,
                        user_name=user_first_name,
                        username=user_tg_username,
                        channel_name=resolved_ch_name
                    )

                    # Dynamic button label
                    raw_btn_text = link_config.get('button_text') or f"👉 Join {resolved_ch_name}"
                    rendered_btn_text = render_template(
                        raw_btn_text,
                        user_name=user_first_name,
                        username=user_tg_username,
                        channel_name=resolved_ch_name
                    )
                    keyboard = [[Button.url(rendered_btn_text, channel_link_str)]]

                    has_extra = bool(link_config.get('telegram_extra_message'))
                    rendered_extra_msg = render_template(
                        link_config.get('telegram_extra_message'),
                        user_name=user_first_name,
                        username=user_tg_username,
                        channel_name=resolved_ch_name
                    ) if has_extra else None

                    # Safe media send with text fallback
                    img_url = link_config.get('telegram_image_url')
                    if img_url:
                        try:
                            await event.respond(
                                rendered_welcome_msg,
                                file=img_url,
                                buttons=None if has_extra else keyboard
                            )
                        except Exception as img_err:
                            logger.warning(f"Bot {bot_id}: Image send note ({img_err}), falling back to text")
                            await event.respond(
                                rendered_welcome_msg,
                                buttons=None if has_extra else keyboard
                            )
                    else:
                        await event.respond(
                            rendered_welcome_msg,
                            buttons=None if has_extra else keyboard
                        )
                        
                    if has_extra and rendered_extra_msg:
                        await event.respond(rendered_extra_msg, buttons=keyboard)
                else:
                    await event.respond("Invalid or expired join link.")
            else:
                await event.respond("Welcome to the bot! Use a valid join link to get started.")

        # ---- Handle Chat Actions (Joins, Leaves, Admin Promotions, Channel Detection) ----
        @client.on(events.ChatAction)
        async def chat_handler(event):
            try:
                chat = await event.get_chat()
                full_channel_id = chat.id
                chat_id_str = str(full_channel_id)

                # ── A. Instant Auto-Detection on Channel Admin Promotion / Addition ──
                if event.is_channel and not event.is_group:
                    try:
                        channel_name = getattr(chat, 'title', f"Channel {full_channel_id}")
                        channel_username = getattr(chat, 'username', None)
                        icon_url = await fetch_channel_photo_b64(token, full_channel_id)

                        # Upsert detected channel immediately
                        await supabase.table('tg_bot_detected_channels').upsert({
                            'bot_id': bot_id,
                            'channel_id': str(full_channel_id),
                            'channel_name': channel_name,
                            'channel_username': channel_username,
                            'channel_icon_url': icon_url
                        }, on_conflict='bot_id,channel_id').execute()

                        logger.info(f"Bot {bot_id}: Auto-detected channel via ChatAction: '{channel_name}' ({full_channel_id})")

                        # Auto-fetch invite links in background
                        asyncio.create_task(fetch_and_sync_channel_invites(client, bot_id, full_channel_id))
                    except Exception as det_err:
                        logger.debug(f"Bot {bot_id}: Channel auto-detect note: {det_err}")

                # ── B. Join / Leave Tracking ──
                mappings = GLOBAL_CHANNEL_MAPPINGS.get(bot_id, [])
                if not mappings:
                    return
                
                is_monitored = any(
                    str(m['channel_id']) in chat_id_str or chat_id_str in str(m['channel_id'])
                    for m in mappings
                )
                if not is_monitored:
                    return

                is_join = getattr(event, 'user_joined', False) or getattr(event, 'user_added', False)
                is_leave = getattr(event, 'user_left', False) or getattr(event, 'user_kicked', False)

                if is_join:
                    user_event = await event.get_user()
                    if not user_event: return
                    user_tg_id = str(user_event.id)
                    logger.info(f"Bot {bot_id}: DETECTED JOIN - User {user_tg_id} in Chat {full_channel_id}")
                    
                    try:
                        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
                        prev_res = await supabase.table('tg_bot_join_users').select('id, status, rejoin_count, left_channel, link_id').eq('bot_id', bot_id).eq('telegram_user_id', user_tg_id).execute()
                        prev_data = getattr(prev_res, 'data', []) or []

                        if prev_data:
                            update_data = {
                                "joined_channel": True,
                                "left_channel": False,
                                "joined_at": now_iso,
                                "status": "active",
                                "last_reminded_at": None,
                                "is_bot_blocked": False,
                            }

                            if prev_data[0].get('status') == 'leaved' or prev_data[0].get('left_channel') is True:
                                update_data["rejoined_at"] = now_iso
                                update_data["rejoin_count"] = (prev_data[0].get('rejoin_count') or 0) + 1
                                logger.info(f"Bot {bot_id}: User {user_tg_id} REJOINED. rejoin_count={update_data['rejoin_count']}")

                            await supabase.table('tg_bot_join_users').update(update_data).eq('id', prev_data[0]['id']).execute()
                        else:
                            # Direct channel join without starting the bot first!
                            target_mapping_id = None
                            for m in mappings:
                                if str(m.get('channel_id', '')) in chat_id_str or chat_id_str in str(m.get('channel_id', '')):
                                    target_mapping_id = m.get('id')
                                    break

                            target_link_id = None
                            if target_mapping_id:
                                l_res = await supabase.table('tg_bot_join_links')\
                                    .select('id')\
                                    .eq('channel_mapping_id', target_mapping_id)\
                                    .order('is_auto_fetched', desc=True)\
                                    .limit(1)\
                                    .execute()
                                if l_res.data:
                                    target_link_id = l_res.data[0]['id']

                            bot_cfg = GLOBAL_BOT_CONFIGS.get(bot_id)
                            u_id = bot_cfg.get('user_id') if bot_cfg else None

                            if target_link_id and u_id:
                                await supabase.table('tg_bot_join_users').insert({
                                    "user_id": u_id,
                                    "bot_id": bot_id,
                                    "link_id": target_link_id,
                                    "telegram_user_id": int(user_tg_id),
                                    "telegram_first_name": getattr(user_event, 'first_name', '') or '',
                                    "telegram_username": getattr(user_event, 'username', None),
                                    "joined_channel": True,
                                    "joined_at": now_iso,
                                    "status": "active"
                                }).execute()
                                logger.info(f"Bot {bot_id}: Registered direct channel join for user {user_tg_id} on link {target_link_id}")
                    except Exception as log_err:
                        logger.error(f"Failed to update channel join stats: {log_err}")

                elif is_leave:
                    user_event = await event.get_user()
                    if not user_event: return
                    user_tg_id = str(user_event.id)
                    logger.info(f"Bot {bot_id}: DETECTED LEAVE - User {user_tg_id} from Chat {full_channel_id}")
                    
                    try:
                        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
                        await supabase.table('tg_bot_join_users').update({
                            "left_channel": True,
                            "left_at": now_iso,
                            "joined_channel": False,
                            "status": "leaved",
                            "rejoined_at": None,
                            "last_reminded_at": None,
                        }).eq('bot_id', bot_id).eq('telegram_user_id', user_tg_id).execute()
                    except Exception as log_err:
                        logger.error(f"Failed to update channel leave stats: {log_err}")
                        
            except Exception as ev_err:
                logger.error(f"Error in chat handler: {ev_err}")

        # ---- Handle Raw Participant & Admin Updates for Channels ----
        @client.on(events.Raw(UpdateChannelParticipant))
        @client.on(events.Raw(UpdateChatParticipantAdmin))
        @client.on(events.Raw(UpdateChatParticipant))
        async def raw_admin_update_handler(event):
            try:
                channel_id = getattr(event, 'channel_id', None) or getattr(event, 'chat_id', None)
                invite_obj = getattr(event, 'invite', None)
                user_joined_id = getattr(event, 'user_id', None)
                
                # Auto-discover invite link created by other admins and track the join
                if invite_obj and channel_id:
                    link_url = getattr(invite_obj, 'link', None)
                    link_title = getattr(invite_obj, 'title', None) or "Admin Invite Link"
                    admin_id = getattr(invite_obj, 'admin_id', None)
                    req_needed = getattr(invite_obj, 'request_needed', False)
                    usage_lim = getattr(invite_obj, 'usage_limit', None)

                    if link_url:
                        cid_str = str(channel_id)
                        full_cid_str = cid_str if cid_str.startswith("-100") else f"-100{cid_str}"
                        logger.info(f"Bot {bot_id}: Discovered admin invite link from Admin ID {admin_id}: {link_url} ('{link_title}')")
                        
                        mappings = GLOBAL_CHANNEL_MAPPINGS.get(bot_id, [])
                        m_id = None
                        for m in mappings:
                            if str(m.get('channel_id', '')) in full_cid_str or full_cid_str in str(m.get('channel_id', '')):
                                m_id = m.get('id')
                                break

                        bot_cfg = GLOBAL_BOT_CONFIGS.get(bot_id)
                        u_id = bot_cfg.get('user_id') if bot_cfg else None

                        target_link_id = None
                        try:
                            # Extract clean prefix token to match existing links even if Telegram returns truncated "https://t.me/+RJ74MoBt..."
                            clean_token = link_url.rstrip('.').replace('https://t.me/+', '').replace('https://t.me/joinchat/', '').replace('https://t.me/', '')
                            
                            all_links_res = await supabase.table('tg_bot_join_links').select('id, name, invite_link').eq('bot_id', bot_id).execute()
                            all_links = getattr(all_links_res, 'data', []) or []
                            
                            # 1. Look for exact match or prefix token match
                            for l in all_links:
                                inv = l.get('invite_link') or ''
                                if inv == link_url or (clean_token and len(clean_token) >= 5 and clean_token in inv):
                                    target_link_id = l['id']
                                    break
                            
                            if not target_link_id and u_id:
                                slug = ''.join(random.choices(string.ascii_lowercase + string.digits, k=10))
                                ins_res = await supabase.table('tg_bot_join_links').insert({
                                    "user_id": u_id,
                                    "bot_id": bot_id,
                                    "channel_mapping_id": m_id,
                                    "name": link_title,
                                    "slug": slug,
                                    "invite_link": link_url,
                                    "is_auto_fetched": True,
                                    "telegram_admin_id": str(admin_id) if admin_id else None,
                                    "is_request_needed": bool(req_needed),
                                    "usage_limit": usage_lim,
                                    "telegram_message": "Click the button below to join the private channel.",
                                    "button_text": "Join Channel"
                                }).execute()
                                if ins_res.data:
                                    target_link_id = ins_res.data[0]['id']
                                logger.info(f"Bot {bot_id}: Registered admin invite link: {link_url}")

                            # Directly attribute the user join to THIS specific link!
                            if user_joined_id and target_link_id and u_id:
                                now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
                                u_chk = await supabase.table('tg_bot_join_users').select('id, status, left_channel').eq('bot_id', bot_id).eq('telegram_user_id', user_joined_id).execute()
                                if u_chk.data:
                                    upd = {
                                        "link_id": target_link_id,
                                        "joined_channel": True,
                                        "left_channel": False,
                                        "joined_at": now_iso,
                                        "status": "active"
                                    }
                                    if u_chk.data[0].get('status') == 'leaved' or u_chk.data[0].get('left_channel') is True:
                                        upd["rejoined_at"] = now_iso
                                    await supabase.table('tg_bot_join_users').update(upd).eq('id', u_chk.data[0]['id']).execute()
                                else:
                                    await supabase.table('tg_bot_join_users').insert({
                                        "user_id": u_id,
                                        "bot_id": bot_id,
                                        "link_id": target_link_id,
                                        "telegram_user_id": int(user_joined_id),
                                        "joined_channel": True,
                                        "joined_at": now_iso,
                                        "status": "active"
                                    }).execute()
                                logger.info(f"Bot {bot_id}: Successfully tracked direct join for user {user_joined_id} on invite link (link_id: {target_link_id})")
                        except Exception as ins_err:
                            logger.error(f"Bot {bot_id}: Error registering admin invite link or user: {ins_err}")

                if channel_id:
                    logger.info(f"Bot {bot_id}: Admin update detected on channel {channel_id}. Running auto-detection & link sync...")
                    cid_str = str(channel_id)
                    full_channel_id = int(cid_str if cid_str.startswith("-100") else f"-100{cid_str}")
                    
                    try:
                        chat = await client.get_entity(full_channel_id)
                        channel_name = getattr(chat, 'title', f"Channel {full_channel_id}")
                        channel_username = getattr(chat, 'username', None)
                        icon_url = await fetch_channel_photo_b64(token, full_channel_id)

                        await supabase.table('tg_bot_detected_channels').upsert({
                            'bot_id': bot_id,
                            'channel_id': str(full_channel_id),
                            'channel_name': channel_name,
                            'channel_username': channel_username,
                            'channel_icon_url': icon_url
                        }, on_conflict='bot_id,channel_id').execute()
                    except Exception as e_get:
                        logger.debug(f"Bot {bot_id}: Raw handler get_entity note: {e_get}")

                    asyncio.create_task(fetch_and_sync_channel_invites(client, bot_id, full_channel_id))
            except Exception as raw_err:
                logger.debug(f"Bot {bot_id}: Raw admin update note: {raw_err}")

        # ---- Handle Join Requests (user clicked Request-to-Join link) ----
        @client.on(events.Raw(UpdateBotChatInviteRequester))
        async def join_request_handler(event):
            try:
                user_tg_id = str(event.user_id)
                logger.info(f"Bot {bot_id}: JOIN REQUEST received from user {user_tg_id}")
                await supabase.table('tg_bot_join_users').update({
                    "status": "pending",
                }).eq('bot_id', bot_id).eq('telegram_user_id', user_tg_id).execute()
            except Exception as jr_err:
                logger.error(f"Bot {bot_id}: Error handling join request: {jr_err}")

        # Handle channel messages to detect channels & auto-sync
        @client.on(events.NewMessage)
        async def channel_message_handler(event):
            if event.is_channel and not event.is_group:
                chat = await event.get_chat()
                full_channel_id = chat.id
                
                cache_key = f"{bot_id}_{full_channel_id}"
                now = datetime.datetime.now(datetime.timezone.utc)
                if cache_key in CHANNEL_LAST_DETECTED:
                    last_detected = CHANNEL_LAST_DETECTED[cache_key]
                    if (now - last_detected).total_seconds() < 86400:
                        return
                
                CHANNEL_LAST_DETECTED[cache_key] = now

                try:
                    channel_name = getattr(chat, 'title', f"Channel {full_channel_id}")
                    channel_username = getattr(chat, 'username', None)
                    icon_url = await fetch_channel_photo_b64(token, full_channel_id)

                    logger.info(f"Bot {bot_id}: Detected message in Channel '{channel_name}' ({full_channel_id}).")
                    
                    try:
                        await supabase.table('tg_bot_detected_channels').upsert({
                            'bot_id': bot_id,
                            'channel_id': str(full_channel_id),
                            'channel_name': channel_name,
                            'channel_username': channel_username,
                            'channel_icon_url': icon_url
                        }, on_conflict='bot_id,channel_id').execute()
                    except Exception as db_err:
                        logger.error(f"Bot {bot_id}: Note on DB insert: {db_err}")

                    # Auto-sync invite links for this detected channel
                    asyncio.create_task(fetch_and_sync_channel_invites(client, bot_id, full_channel_id))
                    
                except Exception as e:
                    logger.error(f"Bot {bot_id}: Error fetching channel info: {e}")

        active_clients[bot_id] = client
        active_semaphores[bot_id] = asyncio.Semaphore(10)

        # ---- Background 12-hour interval reminders ----
        async def resend_reminders():
            await asyncio.sleep(300)
            while True:
                try:
                    now = datetime.datetime.now(datetime.timezone.utc)
                    cutoff_12h = (now - datetime.timedelta(hours=12)).isoformat()

                    remind_res = await supabase.table('tg_bot_join_users')\
                        .select('*, link:tg_bot_join_links(*)')\
                        .eq('bot_id', bot_id)\
                        .in_('status', ['bot_started', 'leaved'])\
                        .eq('is_bot_blocked', False)\
                        .execute()
                    all_candidates = getattr(remind_res, 'data', []) or []

                    users_to_remind = []
                    for u in all_candidates:
                        last_reminded = u.get('last_reminded_at')
                        if last_reminded:
                            if last_reminded < cutoff_12h:
                                users_to_remind.append(u)
                        else:
                            user_status = u.get('status')
                            baseline = u.get('left_at') or u.get('created_at') if user_status == 'leaved' else u.get('created_at')
                            if baseline and baseline < cutoff_12h:
                                users_to_remind.append(u)

                    for user_record in users_to_remind:
                        try:
                            user_tg_id = int(user_record['telegram_user_id'])
                            link_config = user_record.get('link')
                            user_status = user_record.get('status')
                            if not link_config:
                                continue

                            invite_link_str = link_config.get('invite_link') or "https://t.me/"
                            target_channel_name = "our private channel"

                            if link_config.get('channel_mapping_id'):
                                try:
                                    m_res = await supabase.table('tg_bot_channel_mappings')\
                                        .select('invite_link, channel_name')\
                                        .eq('id', link_config['channel_mapping_id'])\
                                        .execute()
                                    if m_res.data:
                                        if m_res.data[0].get('invite_link'):
                                            invite_link_str = m_res.data[0]['invite_link']
                                        if m_res.data[0].get('channel_name'):
                                            target_channel_name = m_res.data[0]['channel_name']
                                except Exception as m_err:
                                    logger.warning(f"Bot {bot_id}: Channel mapping lookup note: {m_err}")

                            # Dynamic user info
                            user_first_name = user_record.get('telegram_first_name') or "there"
                            user_tg_username = user_record.get('telegram_username')

                            # Dynamic Button Text
                            raw_btn_text = link_config.get('button_text') or f"👉 Join {target_channel_name}"
                            rendered_btn_text = render_template(
                                raw_btn_text,
                                user_name=user_first_name,
                                username=user_tg_username,
                                channel_name=target_channel_name
                            )
                            keyboard = [[Button.url(rendered_btn_text, invite_link_str)]]

                            if user_status == 'leaved':
                                reminder_template = (
                                    "👋 **Hey {first_name}, we miss you!**\n\n"
                                    "It looks like you left **{channel_name}**.\n"
                                    "Come back and rejoin anytime using the link below!"
                                )
                            else:
                                reminder_template = (
                                    "🔔 **Hey {first_name}!**\n\n"
                                    "You haven't joined **{channel_name}** yet.\n"
                                    "Click the button below to get instant access!"
                                )

                            rendered_reminder = render_template(
                                reminder_template,
                                user_name=user_first_name,
                                username=user_tg_username,
                                channel_name=target_channel_name
                            )

                            custom_msg = link_config.get('telegram_message') or ""
                            if custom_msg:
                                rendered_custom = render_template(
                                    custom_msg,
                                    user_name=user_first_name,
                                    username=user_tg_username,
                                    channel_name=target_channel_name
                                )
                                rendered_reminder = f"{rendered_reminder}\n\n{rendered_custom}"

                            try:
                                # Safe Media Delivery with Failsafe
                                img_url = link_config.get('telegram_image_url')
                                if img_url:
                                    try:
                                        await client.send_message(
                                            user_tg_id, rendered_reminder,
                                            file=img_url,
                                            buttons=keyboard
                                        )
                                    except Exception as img_send_err:
                                        logger.warning(f"Bot {bot_id}: Reminder image failed ({img_send_err}), sending text-only fallback")
                                        await client.send_message(
                                            user_tg_id, rendered_reminder, buttons=keyboard
                                        )
                                else:
                                    await client.send_message(
                                        user_tg_id, rendered_reminder, buttons=keyboard
                                    )

                                await supabase.table('tg_bot_join_users')\
                                    .update({'last_reminded_at': now.isoformat(), 'reminder_sent': True})\
                                    .eq('id', user_record['id'])\
                                    .execute()

                            except Exception as send_err:
                                err_str = str(send_err).lower()
                                if 'blocked' in err_str or 'user is blocked' in err_str or 'forbidden' in err_str:
                                    await supabase.table('tg_bot_join_users')\
                                        .update({'is_bot_blocked': True})\
                                        .eq('id', user_record['id'])\
                                        .execute()

                            await asyncio.sleep(0.5)

                        except Exception as remind_user_err:
                            logger.error(f"Bot {bot_id}: Failed to remind user: {remind_user_err}")

                except Exception as remind_loop_err:
                    logger.error(f"Bot {bot_id}: Error in resend_reminders loop: {remind_loop_err}")

                await asyncio.sleep(1800)

        asyncio.create_task(resend_reminders())
        logger.info(f"Bot {bot_id}: 12h reminder task started.")

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
        
        mapping_res = await supabase.table('tg_bot_channel_mappings').select('*').eq('channel_id', target_channel_id).eq('status', 'Active').execute()
        mappings = getattr(mapping_res, 'data', []) or []
        
        for mapping in mappings:
            bot_id = mapping['bot_id']
            mapping_pk = mapping['id']
            
            if bot_id in active_clients:
                client = active_clients[bot_id]
                
                prog_res = await supabase.table('tg_bot_broadcast_progress').select('*').eq('task_id', task_id).eq('bot_id', bot_id).execute()
                prog_data = getattr(prog_res, 'data', [])
                if prog_data:
                    if prog_data[0]['status'] != 'pending': continue
                else:
                    await supabase.table('tg_bot_broadcast_progress').insert({'task_id': task_id, 'bot_id': bot_id, 'status': 'processing'}).execute()
                
                links_res = await supabase.table('tg_bot_join_links').select('id').eq('bot_id', bot_id).eq('channel_mapping_id', mapping_pk).execute()
                link_ids = [l['id'] for l in getattr(links_res, 'data', [])]
                
                if not link_ids:
                    links_res = await supabase.table('tg_bot_join_links').select('id').eq('bot_id', bot_id).execute()
                    link_ids = [l['id'] for l in getattr(links_res, 'data', [])]

                if not link_ids:
                    await supabase.table('tg_bot_broadcast_progress').update({'status': 'completed', 'error_log': 'No links'}).eq('task_id', task_id).eq('bot_id', bot_id).execute()
                    continue
                    
                users_res = await supabase.table('tg_bot_join_users').select('telegram_user_id').in_('link_id', link_ids).execute()
                target_users = getattr(users_res, 'data', []) or []
                
                if not target_users:
                    await supabase.table('tg_bot_broadcast_progress').update({'status': 'completed', 'error_log': 'No users'}).eq('task_id', task_id).eq('bot_id', bot_id).execute()
                    continue

                logger.info(f"Bot {bot_id}: Starting broadcast for task {task_id} to {len(target_users)} users.")
                
                async def do_broadcast(c: TelegramClient, b_id, t_id, users, msg_data):
                    import time
                    start_time = time.time()
                    stats = {'sent': 0, 'errors': 0}
                    media_path = msg_data.get('media_path')
                    raw_text = msg_data.get('raw_text', '')
                    
                    uploaded_media = None
                    if media_path and os.path.exists(media_path):
                        try:
                            uploaded_media = await c.upload_file(media_path)
                        except Exception as e:
                            logger.error(f"Bot {b_id}: Failed to pre-upload media: {e}")
                    
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
                                await asyncio.sleep(0.05)
                            except Exception as e:
                                stats['errors'] += 1
                                logger.warning(f"Bot {b_id}: Failed to send to {user['telegram_user_id']}: {e}")

                    tasks = [send_to_user(u) for u in users]
                    await asyncio.gather(*tasks)
                    
                    if media_path and os.path.exists(media_path):
                        try: os.remove(media_path)
                        except: pass
                    
                    duration = time.time() - start_time
                    await supabase.table('tg_bot_broadcast_progress').update({
                        'status': 'completed', 'sent_count': stats['sent'],
                        'total_targeted': len(users),
                        'error_log': f"Finished in {duration:.2f}s with {stats['errors']} errors"
                    }).eq('task_id', t_id).eq('bot_id', b_id).execute()
                    
                    await supabase.table('tg_broadcast_tasks').update({'status': 'completed'}).eq('id', t_id).execute()
                    logger.info(f"Bot {b_id}: Completed broadcast for task {t_id}. Sent: {stats['sent']}/{len(users)}")

                asyncio.create_task(do_broadcast(client, bot_id, task_id, target_users, message_data))
    except Exception as e:
        logger.error(f"Error processing task: {e}")


async def synchronize_bots():
    """Syncs the current running bots and checks for link sync requests."""
    try:
        logger.info("Synchronizing bots with database...")
        # 1. Fetch bots
        response = await supabase.table('tg_tracker').select('*').in_('status', ['Pending', 'Active', 'pending', 'active']).execute()
        bots = getattr(response, 'data', []) or []
        
        # 2. Fetch mappings
        mapping_res = await supabase.table('tg_bot_channel_mappings').select('*').execute()
        all_mappings = getattr(mapping_res, 'data', []) or []
        
        current_bot_ids = set()
        for bot in bots:
            bot_id = bot['id']
            token = bot['bot_token']
            GLOBAL_BOT_CONFIGS[bot_id] = bot
            bot_active_maps = [m for m in all_mappings if m['bot_id'] == bot_id and m.get('status') == 'Active']
            GLOBAL_CHANNEL_MAPPINGS[bot_id] = bot_active_maps
            current_bot_ids.add(bot_id)
            
            if bot_id not in running_tasks:
                task = asyncio.create_task(start_bot(token, bot_id))
                running_tasks[bot_id] = task

        # 3. Check for any mappings with sync_links_requested = True
        for m in all_mappings:
            if m.get('sync_links_requested') is True:
                b_id = m.get('bot_id')
                cid = m.get('channel_id')
                if b_id in active_clients and cid:
                    logger.info(f"Triggering on-demand invite link sync for bot {b_id}, channel {cid}...")
                    asyncio.create_task(fetch_and_sync_channel_invites(
                        active_clients[b_id], b_id, cid, m.get('id')
                    ))
                
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

        # Check for any pending broadcast tasks
        tasks_res = await supabase.table('tg_broadcast_tasks').select('*').eq('status', 'pending').execute()
        pending_tasks = getattr(tasks_res, 'data', []) or []
        for task in pending_tasks:
            await process_task(task)
            
    except Exception as e:
        logger.error(f"Error in synchronization: {e}")


async def bot_runner():
    global supabase
    logger.info("Bot Manager Started (Async Realtime Mode). Setting up listeners...")
    
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

                if isinstance(payload, dict) and 'data' in payload:
                    inner = payload['data']
                    table = inner.get('table')
                    raw_type = inner.get('type')
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
                    return
                    
                logger.info(f"Realtime Event: {event_type} on {table}")
                
                if MAIN_LOOP:
                    if table == 'tg_broadcast_tasks' and event_type.upper() == 'INSERT':
                        asyncio.run_coroutine_threadsafe(process_task(record), MAIN_LOOP)
                    elif table in ['tg_tracker', 'tg_bot_channel_mappings']:
                        asyncio.run_coroutine_threadsafe(synchronize_bots(), MAIN_LOOP)
            except Exception as e:
                logger.error(f"Error in on_realtime_event: {e}")

        channel = supabase.channel('db-changes')
        
        channel.on_postgres_changes(
            event="INSERT",
            schema="public",
            table="tg_broadcast_tasks",
            callback=on_realtime_event
        )
        
        channel.on_postgres_changes(
            event="*",
            schema="public",
            table="tg_tracker",
            callback=on_realtime_event
        )
        
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

    # Periodic "Safety" Sync
    while True:
        try:
            await asyncio.sleep(30)
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