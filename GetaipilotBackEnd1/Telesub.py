import os
import asyncio
import logging
from datetime import datetime
from fastapi import FastAPI, HTTPException, Header, Body, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
from telethon import TelegramClient, functions, types
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError
from supabase import create_client, Client
import hashlib
import hmac

from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

app = FastAPI()

# Build CORS origins from env + safe defaults.
# Important: keep localhost entries even when CORS_ORIGINS is set, so local
# dashboards can call production backend during development.
_env_origins = os.getenv("CORS_ORIGINS")
_default_origins = [
    "https://getaipilot.in",
    "https://www.getaipilot.in",
    "https://api.getaipilot.in",
    "http://localhost:8080",
    "http://localhost:5173",
    "http://127.0.0.1:8080",
]
_env_origin_list = [o.strip() for o in (_env_origins or "").split(",") if o.strip()]
origins = sorted(set(_default_origins + _env_origin_list))

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.getenv("VITE_SUPABASE_URL")
SUPABASE_KEY = os.getenv("VITE_SUPABASE_ANON_KEY")
API_ID = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")

if not SUPABASE_URL or not SUPABASE_KEY:
    logger.warning("⚠️  Supabase credentials missing! Auth will fail.")
else:
    logger.info("✅ Supabase credentials loaded")


if not API_ID or not API_HASH:
    logger.warning("⚠️  Telegram API credentials missing! Login will fail.")
else:
    logger.info("✅ Telegram API credentials loaded")

try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    logger.info("✅ Supabase client initialized")
except Exception as e:
    logger.error(f"❌ Supabase initialization failed: {e}")
    supabase = None


clients = {}


def get_authed_supabase(token: str) -> Client:
    """Create a per-request Supabase client bound to the end-user JWT.

    NOTE: Do not mutate the global client with .postgrest.auth(token) because
    that can leak auth between concurrent requests.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise HTTPException(status_code=500, detail="Server configuration error")
    sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    sb.postgrest.auth(token)
    return sb


class PhoneRequest(BaseModel):
    phone: str


class OtpRequest(BaseModel):
    otp: str


class PasswordRequest(BaseModel):
    password: str
    phone: Optional[str] = None


class ChatIdRequest(BaseModel):
    chat_id: int


async def get_auth_context(authorization: str = Header(None)):
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization Header")
    if supabase is None:
        logger.error("Supabase client not initialized; check SUPABASE env vars")
        raise HTTPException(status_code=500, detail="Server configuration error")

    token = authorization.replace("Bearer ", "")
    try:
        # Verify user JWT (GoTrue)
        user_res = supabase.auth.get_user(token)
        if not user_res or not user_res.user:
            raise HTTPException(status_code=401, detail="Invalid Token")
        return {"user": user_res.user, "token": token}
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))


async def get_telegram_client(user_id: str):
    os.makedirs("sessions", exist_ok=True)
    session_file = f"sessions/user_{user_id}.session"

    if user_id not in clients:
        if not API_ID or not API_HASH:
            raise HTTPException(
                status_code=500,
                detail="Missing TELEGRAM_API_ID or TELEGRAM_API_HASH in .env",
            )

        client = TelegramClient(
            session_file,
            int(API_ID),
            API_HASH,
            flood_sleep_threshold=10,  # Wait if rate limited
        )
        await client.connect()
        clients[user_id] = client

    return clients[user_id]


@app.get("/")
def read_root():
    return {
        "status": "ok",
        "service": "Telegram Backend",
        "version": "1.0.0",
        "timestamp": datetime.utcnow().isoformat(),
        "health": {
            "supabase": "connected" if supabase else "not configured",
            "telegram": "ready" if API_ID and API_HASH else "not configured",
        },
    }


@app.get("/health")
def health_check():
    """Production health check endpoint"""
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "active_sessions": len(clients),
    }


@app.get("/telegram/status")
async def get_status(ctx: dict = Depends(get_auth_context)):
    user = ctx["user"]
    user_id = user.id
    logger.info(f"Status check for user: {user_id}")
    client = await get_telegram_client(user_id)

    is_connected = await client.is_user_authorized()
    phone = None
    if is_connected:
        me = await client.get_me()
        if me:
            phone = f"+{me.phone}" if me.phone else "Unknown"

    logger.info(
        f"User {user_id} status: {'connected' if is_connected else 'disconnected'}"
    )
    return {"connected": is_connected, "phone": phone}


@app.post("/telegram/login/start")
async def login_start(request: PhoneRequest, ctx: dict = Depends(get_auth_context)):
    user = ctx["user"]
    user_id = user.id
    logger.info(f"Login start for user: {user_id}, phone: {request.phone}")
    client = await get_telegram_client(user_id)

    if await client.is_user_authorized():
        logger.info(f"User {user_id} already connected")
        return {"status": "already_connected"}

    try:
        await client.send_code_request(request.phone)
        client._phone_number = request.phone
        logger.info(f"OTP sent to {request.phone} for user {user_id}")
        return {"status": "otp_sent"}
    except Exception as e:
        logger.error(f"Login start failed for {user_id}: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/telegram/login/otp")
async def login_otp(request: OtpRequest, ctx: dict = Depends(get_auth_context)):
    user = ctx["user"]
    token = ctx["token"]
    sb = get_authed_supabase(token)
    user_id = user.id
    client = await get_telegram_client(user_id)

    if not hasattr(client, "_phone_number"):
        raise HTTPException(status_code=400, detail="Please start login first")

    try:
        await client.sign_in(client._phone_number, request.otp)

        # Save to telegram_accounts on success
        me = await client.get_me()
        account_data = {
            "user_id": user_id,
            "phone": client._phone_number,
            "session_file": f"user_{user_id}.session",
            "is_active": True,
            "updated_at": datetime.utcnow().isoformat(),
        }
        sb.table("tg_accounts").upsert(
            account_data, on_conflict="user_id"
        ).execute()

        # Update Profiles (telegram_user_id)
        sb.table("profiles").update({"telegram_user_id": me.id}).eq(
            "id", user_id
        ).execute()

        return {"status": "connected", "next": "done"}
    except SessionPasswordNeededError:
        return {"status": "needs_password", "next": "password_required"}
    except PhoneCodeInvalidError:
        raise HTTPException(status_code=400, detail="Invalid OTP")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/telegram/login/password")
async def login_password(
    request: PasswordRequest, ctx: dict = Depends(get_auth_context)
):
    user = ctx["user"]
    token = ctx["token"]
    sb = get_authed_supabase(token)
    user_id = user.id
    client = await get_telegram_client(user_id)

    try:
        # If phone is provided, ensure it's set on the client
        phone = request.phone
        if not phone and hasattr(client, "_phone_number"):
            phone = client._phone_number

        logger.info(f"Attempting password login for user {user_id} with phone {phone}")

        if phone:
            await client.sign_in(phone=phone, password=request.password)
        else:
            await client.sign_in(password=request.password)

        # Save to telegram_accounts on success
        me = await client.get_me()
        # Note: phone might stay in memory from start call or be fetched here if missing
        phone_to_save = f"+{me.phone}" if me.phone else (phone or "Unknown")
        account_data = {
            "user_id": user_id,
            "phone": phone_to_save,
            "session_file": f"user_{user_id}.session",
            "is_active": True,
            "updated_at": datetime.utcnow().isoformat(),
        }
        sb.table("tg_accounts").upsert(
            account_data, on_conflict="user_id"
        ).execute()

        # Update Profiles
        sb.table("profiles").update({"telegram_user_id": me.id}).eq(
            "id", user_id
        ).execute()

        logger.info(f"Password login successful for user {user_id}")
        return {"status": "connected"}
    except Exception as e:
        logger.error(f"Login password failed for {user_id}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/telegram/chats")
async def get_chats(ctx: dict = Depends(get_auth_context)):
    user = ctx["user"]
    user_id = user.id
    client = await get_telegram_client(user_id)

    if not await client.is_user_authorized():
        return {"data": []}

    dialogs = []
    from telethon.tl.types import ChannelParticipantAdmin, ChannelParticipantCreator
    async for dialog in client.iter_dialogs(limit=100):
        if dialog.is_channel or dialog.is_group:
            entity = dialog.entity

            # THE DEFINITIVE CHECK
            is_admin = False
            try:
                p = await client.get_participant(entity, 'me')
                print(f"🔍 [GET_CHATS] Chat: {dialog.name}")
                print(f"   Type: {type(p).__name__}")
                if isinstance(p, ChannelParticipantCreator):
                    is_admin = True
                    print(f"   ✅ CREATOR/OWNER - Including")
                elif isinstance(p, ChannelParticipantAdmin):
                    is_admin = bool(p.admin_rights.invite_users)
                    print(f"   Invite Users Permission: {p.admin_rights.invite_users}")
                    print(f"   {'✅ ADMIN with invite - Including' if is_admin else '❌ Admin without invite - Skipping'}")
                else:
                    print(f"   ❌ Regular member - Skipping")
            except Exception as e:
                print(f"   ⚠️ Error checking participant: {e}")
                is_admin = getattr(entity, 'creator', False)
                print(f"   Fallback creator check: {is_admin}")

            if not is_admin:
                print(f"   ⛔ SKIPPING\n")
                continue

            print(f"   ✅ ADDING to list\n")

            dialogs.append(
                {
                    "chat_id": dialog.id,
                    "chat_title": dialog.name,
                    "chat_type": "channel" if dialog.is_channel else "group",
                    "last_synced_at": dialog.date.isoformat() if dialog.date else None,
                }
            )

    return {"data": dialogs}


@app.post("/telegram/sync/chats")
async def sync_chats(ctx: dict = Depends(get_auth_context)):
    user = ctx["user"]
    token = ctx["token"]
    sb = get_authed_supabase(token)
    user_id = user.id
    client = await get_telegram_client(user_id)
    if not await client.is_user_authorized():
        raise HTTPException(status_code=401, detail="Telegram not connected")

    # Clear existing chats for this user to ensure old non-admin chats are removed
    sb.table("tg_chats").delete().eq("user_id", user_id).execute()

    # Fetch and save chats to telegram_chats table
    chats = []
    from telethon.tl.types import ChannelParticipantAdmin, ChannelParticipantCreator
    async for dialog in client.iter_dialogs(limit=100):
        if dialog.is_channel or dialog.is_group:
            entity = dialog.entity

            is_admin = False
            try:
                p = await client.get_participant(entity, 'me')
                print(f"🔍 Chat: {dialog.name}")
                print(f"   Type: {type(p).__name__}")
                if isinstance(p, ChannelParticipantCreator):
                    is_admin = True
                    print(f"   ✅ CREATOR - Including")
                elif isinstance(p, ChannelParticipantAdmin):
                    is_admin = bool(p.admin_rights.invite_users)
                    print(f"   Admin Rights: {p.admin_rights}")
                    print(f"   Invite Users: {p.admin_rights.invite_users}")
                    print(f"   {'✅ ADMIN with invite - Including' if is_admin else '❌ Admin without invite - Skipping'}")
                else:
                    print(f"   ❌ Regular member - Skipping")
            except Exception as e:
                print(f"   ⚠️ Error checking participant: {e}")
                is_admin = getattr(entity, 'creator', False)
                print(f"   Fallback creator check: {is_admin}")

            if not is_admin:
                print(f"   ⛔ SKIPPING this chat\n")
                continue

            print(f"   ✅ ADDING to sync list\n")

            chat_type = "channel" if dialog.is_channel else "group"
            chat_data = {
                "user_id": user_id,
                "chat_id": dialog.id,
                "chat_title": dialog.name,
                "chat_type": chat_type,
                "username": getattr(dialog.entity, "username", None),
                "last_synced_at": datetime.utcnow().isoformat(),
            }
            chats.append(chat_data)

            # Upsert into telegram_chats
            # We use a combined search or just upsert if we had a constraint,
            # but since 'id' is pk, we can use user_id + chat_id or just insert
            # Assuming schema has some way to avoid duplicates or we just update
            sb.table("tg_chats").upsert(
                chat_data, on_conflict="user_id,chat_id"
            ).execute()

    return {"status": "synced", "count": len(chats)}


@app.post("/telegram/generate-invite-link")
async def generate_invite_link(
    request: ChatIdRequest, ctx: dict = Depends(get_auth_context)
):
    """Generate admin-approval invite link for a Telegram channel/group"""
    user = ctx["user"]
    token = ctx["token"]
    sb = get_authed_supabase(token)
    user_id = user.id
    client = await get_telegram_client(user_id)

    if not await client.is_user_authorized():
        raise HTTPException(status_code=401, detail="Telegram not connected")

    try:
        # Get the chat entity
        chat = await client.get_entity(request.chat_id)

        # Create invite link with admin approval required
        result = await client(
            functions.messages.ExportChatInviteRequest(
                peer=chat,
                request_needed=True,  # REQUIRES ADMIN APPROVAL
                title="Landing Page Join Request",
            )
        )

        invite_link = result.link if hasattr(result, "link") else None

        if not invite_link:
            raise HTTPException(
                status_code=500, detail="Failed to generate invite link"
            )

        # Update the communities table with the invite link
        try:
            sb.table("tg_communities").update({"invite_link": invite_link}).eq(
                "telegram_chat_id", request.chat_id
            ).eq("user_id", user_id).execute()
        except Exception as db_error:
            logger.warning(f"Failed to update invite link in database: {db_error}")

        logger.info(f"Generated admin-approval invite link for chat {request.chat_id}")
        return {"invite_link": invite_link}

    except Exception as e:
        logger.error(f"Failed to generate invite link: {str(e)}")
        raise HTTPException(
            status_code=400, detail=f"Failed to generate invite link: {str(e)}"
        )


@app.get("/telegram/get-invite-link/{chat_id}")
async def get_invite_link(chat_id: int, ctx: dict = Depends(get_auth_context)):
    """Get existing invite link for a channel from database or generate new one with admin approval"""
    user = ctx["user"]
    token = ctx["token"]
    sb = get_authed_supabase(token)
    user_id = user.id

    try:
        # First check if we have it in the database
        result = (
            sb.table("tg_communities")
            .select("invite_link")
            .eq("telegram_chat_id", chat_id)
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )

        if result and result.data and result.data.get("invite_link"):
            logger.info(f"Found cached invite link for chat {chat_id}")
            return {"invite_link": result.data["invite_link"]}

        # If not in database, generate a new one
        client = await get_telegram_client(user_id)

        if not await client.is_user_authorized():
            raise HTTPException(status_code=401, detail="Telegram not connected")

        # Get chat entity with retry for database lock errors
        max_retries = 3
        for attempt in range(max_retries):
            try:
                chat = await client.get_entity(chat_id)
                break
            except Exception as e:
                if "database is locked" in str(e) and attempt < max_retries - 1:
                    logger.warning(
                        f"Database locked, retrying in 1 second (attempt {attempt + 1}/{max_retries})"
                    )
                    await asyncio.sleep(1)
                else:
                    raise

        # Create invite link with admin approval required
        result = await client(
            functions.messages.ExportChatInviteRequest(
                peer=chat,
                request_needed=True,  # REQUIRES ADMIN APPROVAL
                title="Landing Page Join Request",
            )
        )

        invite_link = result.link if hasattr(result, "link") else None

        if not invite_link:
            raise HTTPException(
                status_code=500, detail="Failed to generate invite link"
            )

        logger.info(f"Generated admin-approval invite link for chat {chat_id}")

        # Update database
        try:
            sb.table("tg_communities").update({"invite_link": invite_link}).eq(
                "telegram_chat_id", chat_id
            ).eq("user_id", user_id).execute()
        except Exception as db_error:
            logger.warning(f"Failed to update invite link in database: {db_error}")

        return {"invite_link": invite_link}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Unexpected error getting invite link for chat {chat_id}: {str(e)}",
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@app.post("/telegram/logout")
async def tg_logout(ctx: dict = Depends(get_auth_context)):
    user = ctx["user"]
    token = ctx["token"]
    sb = get_authed_supabase(token)
    user_id = user.id
    logger.info(f"Logout requested for user: {user_id}")

    try:
        if user_id in clients:
            client = clients[user_id]
            try:
                if await client.is_user_authorized():
                    await client.log_out()
            except Exception as e:
                logger.error(f"Error during Telegram log_out for {user_id}: {e}")

            await client.disconnect()
            del clients[user_id]

        # Clean up session file
        session_file = f"sessions/user_{user_id}.session"
        if os.path.exists(session_file):
            try:
                os.remove(session_file)
                logger.info(f"Deleted session file for user {user_id}")
            except Exception as e:
                logger.error(f"Failed to delete session file for {user_id}: {e}")

        # Update database status
        try:
            sb.table("tg_accounts").update({"is_active": False}).eq(
                "user_id", user_id
            ).execute()
        except Exception as e:
            logger.warning(
                f"Failed to update telegram_accounts in DB for {user_id}: {e}"
            )

        return {"status": "success"}
    except Exception as e:
        logger.error(f"Global logout error for {user_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8002"))
    uvicorn.run(app, host=host, port=port)
