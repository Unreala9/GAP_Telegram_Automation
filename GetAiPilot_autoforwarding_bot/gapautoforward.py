import os, re, asyncio, json, base64, sys
from telethon.tl import types
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo  # <-- NEW

IST = ZoneInfo("Asia/Kolkata")  # <-- NEW: India time zone

from typing import Dict, List, Tuple, Set, Any, Optional

from dotenv import load_dotenv
from supabase import create_client, Client

from telethon import TelegramClient, events, errors, Button
from telethon.tl import functions
from telethon.tl.functions.bots import SetBotCommandsRequest
from telethon.types import BotCommand, BotCommandScopeDefault
from telethon.utils import get_peer_id

# Resolve paths relative to this script directory
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOTENV_PATH = os.path.join(SCRIPT_DIR, ".env")
if os.path.exists(DOTENV_PATH):
    load_dotenv(DOTENV_PATH)
else:
    load_dotenv()

API_ID        = int(os.getenv("API_ID", "0"))
API_HASH      = os.getenv("API_HASH", "").strip()
BOT_TOKEN     = os.getenv("BOT_TOKEN", "").strip()
SUPABASE_URL  = os.getenv("SUPABASE_URL", "").strip()
# Prioritize service role key for backend bots, fallback to SUPABASE_KEY
SUPABASE_KEY  = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY", "")).strip()

raw_session_dir = os.getenv("SESSION_DIR", "sessions")
SESSION_DIR   = raw_session_dir if os.path.isabs(raw_session_dir) else os.path.join(SCRIPT_DIR, raw_session_dir)
TOP_N         = 14
FORWARD_THROTTLE = 0.2  # seconds between sends to avoid spam & rate limits

# Razorpay / Plan (BOT side disabled now)
# Dashboard manages payments + subscriptions
DASHBOARD_PLANS_URL = os.getenv("DASHBOARD_PLANS_URL", "").strip()  # e.g. https://getaipilot.in/pricing
DASHBOARD_SUPPORT_URL = os.getenv("DASHBOARD_SUPPORT_URL", "").strip()  # optional support link

# Keep these envs only if you still want to show prices in bot UI
PLAN_PRICE_INR      = int(os.getenv("PLAN_PRICE_INR", "699"))
PLAN_DURATION_DAYS  = int(os.getenv("PLAN_DURATION_DAYS", "30"))

if not (API_ID and API_HASH and BOT_TOKEN):
    print("❌ ERROR: Missing API_ID, API_HASH, or BOT_TOKEN in .env")
    raise RuntimeError("Missing API_ID, API_HASH, or BOT_TOKEN in .env")

if not (SUPABASE_URL and SUPABASE_KEY):
    print("❌ ERROR: Missing SUPABASE_URL or SUPABASE_KEY/SUPABASE_SERVICE_ROLE_KEY in .env")
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_KEY in .env")

os.makedirs(SESSION_DIR, exist_ok=True)

try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
except Exception as ex:
    print(f"❌ Failed to initialize Supabase client: {ex}")
    raise

bot_session_path = os.path.join(SCRIPT_DIR, "login_bot_runner")
bot = TelegramClient(bot_session_path, API_ID, API_HASH).start(bot_token=BOT_TOKEN)

PHONE_RE = re.compile(r"^\+\d{6,15}$", re.IGNORECASE)
OTP_RE   = re.compile(r"^(?:LOGIN\s*)?(\d{4,8})$", re.IGNORECASE)  # 123456 or LOGIN123456

# --- in-memory states ---
login_state: Dict[int, Dict[str, Any]]  = {}
select_state: Dict[int, Dict[str, Any]] = {}   # for incoming/outgoing & remove flows
forward_loops: Dict[int, Dict[str, Any]] = {}  # uid -> {"client": user_client}

# Ek hi user ke liye ek hi TelegramClient use karne ke liye
USER_CLIENT_CACHE: Dict[int, TelegramClient] = {}


# ---------------- COMMANDS (single source of truth) ----------------
COMMANDS: List[Tuple[str, str]] = [
    ("start_demo", "Start 7-day FREE demo (temporary PREMIUM)"),
    ("start", "Show all commands & how to use"),
    ("help", "Provide help information, regarding bot usage and support"),
    ("status", "Check login status"),
    ("login", "Login your Telegram account"),
    ("config", "View current mapping"),
    ("tg_plans", "View plans & features"),
    ("upgrade", f"Buy/Renew Premium (₹{PLAN_PRICE_INR} / {PLAN_DURATION_DAYS} days)"),
    ("stoplogin", "Cancel an in-progress /login flow"),
    ("upgrade_status", "Check subscription status"),
    ("logout", "Delete session & logout"),
    # premium-only below:
    ("incoming", "Select source chats (Basic)"),
    ("outgoing", "Select target chats (Basic)"),
    ("work", "Start auto-forward (Basic)"),
    ("stop", "Stop auto-forward (Basic)"),
    ("remove_incoming", "Remove saved sources (Basic)"),
    ("remove_outgoing", "Remove saved targets (Basic)"),
    ("addfilter", "Replace @left with @right in forwarded text/captions (Pro)"),
    ("showfilter", "Show all saved replace filters (Pro)"),
    ("removefilter", "Delete a filter by its left name (Pro)"),
    ("deleteallfilters", "Delete all your text-replace filters (Pro)"),
    ("delay", "Set send delay in seconds (0-999) (Pro)"),
    ("removedelay", "Remove any set forwarding delay (Pro)"),
    ("start_text", "Add a custom starting text to all forwarded messages (premium)"),
    ("end_text", "Add a custom ending text to all forwarded messages (premium)"),
    ("remove_text", "Remove saved start/end texts (premium)"),
    ("blacklist_word", "Remove inappropriate words from forwarded text (premium)"),
    ("remove_blacklist", "Delete blacklisted words via buttons (premium)"),
    ("my_id", "Show your Telegram user ID and username"),



]

def commands_text() -> str:
    lines = ["👋 **Welcome!** Here are all available commands:", ""]
    icons = {
        "start": "🏁","start_demo": "🎁", "help": "ℹ️", "status": "🧩", "login": "📱", "config": "📋",
        "upgrade": "💳", "upgrade_status": "📊", "logout": "🚪",
        "incoming": "📥", "outgoing": "📤", "work": "▶️", "stop": "⏸️",
        "remove_incoming": "❌", "remove_outgoing": "❌",
        "addfilter": "🧩", "showfilter": "🧾", "removefilter": "🗑️", "deleteallfilters": "🧨",
        "delay": "⏱️", "blacklist_word": "🚫", "remove_blacklist": "🧹", "my_id": "🆔",

    }
    for cmd, desc in COMMANDS:
        lines.append(f"• {icons.get(cmd, '•')} /{cmd} — {desc}")
    lines.append("")
    lines.append("_Premium needed for forwarding, filters, remove/add targets, delay._")
    return "\n".join(lines)

# ---------------- SUPABASE HELPERS ----------------
# ---------- FILTERS ----------
# ---------- START/END TEXT HELPERS ----------
# ---------- BLACKLIST WORDS (for inappropriate terms) ----------


def sp_add_blacklist_word(uid: int, word: str) -> Tuple[bool, str]:
    w = (word or "").strip()
    if not w:
        return False, "⚠️ Use: `/blacklist_word WORD`"
    try:
        supabase.table("tg_user_blacklist_words").insert({
            "user_id": uid,
            "word": w,
            "word_lower": w.lower(),
        }).execute()
        return True, f"✅ Added to blacklist: `{w}`\n use /work to start forwarding with updated blacklist."
    except Exception as ex:
        msg = str(ex).lower()
        if "duplicate" in msg or "unique" in msg or "23505" in msg:
            return False, "ℹ️ This word is already in your blacklist."
        return False, f"❌ Failed: {ex}"

def sp_list_blacklist(uid: int) -> List[dict]:
    try:
        res = supabase.table("tg_user_blacklist_words").select("*")\
            .eq("user_id", uid).order("created_at", desc=True).execute()
        return res.data or []
    except Exception as ex:
        print(f"sp_list_blacklist error for user {uid}: {ex}")
        return []

def sp_delete_blacklist_word(uid: int, word: str) -> Tuple[bool, str]:
    w = (word or "").strip().lower()
    if not w:
        return False, "⚠️ Use: `/remove_blacklist` buttons se delete karo."
    try:
        supabase.table("tg_user_blacklist_words").delete()\
            .eq("user_id", uid).eq("word_lower", w).execute()
        check = supabase.table("tg_user_blacklist_words").select("id")\
            .eq("user_id", uid).eq("word_lower", w).limit(1).execute()
        if check and check.data:
            return False, "❌ Remove failed (DB). Try again."
        return True, f"🗑️ Removed `{word}` from blacklist"
    except Exception as ex:
        print(f"sp_delete_blacklist_word error for user {uid}: {ex}")
        return False, f"❌ Remove failed: {ex}"

def sp_delete_blacklist_batch(uid: int, words: List[str]) -> int:
    lowers = [ (w or "").strip().lower() for w in words if (w or "").strip() ]
    if not lowers: return 0
    try:
        supabase.table("tg_user_blacklist_words").delete()\
            .eq("user_id", uid).in_("word_lower", lowers).execute()
        rem = supabase.table("tg_user_blacklist_words").select("id")\
            .eq("user_id", uid).in_("word_lower", lowers).execute().data or []
        return max(0, len(lowers) - len(rem))
    except Exception as ex:
        print(f"sp_delete_blacklist_batch error for user {uid}: {ex}")
        return 0

def sp_set_start_text(uid: int, text: str):
    payload = {
        "user_id": uid,
        "start_text": text.strip(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        supabase.table("tg_user_text_addons").upsert(payload, on_conflict="user_id").execute()
    except Exception as ex:
        print(f"sp_set_start_text error for user {uid}: {ex}")

def sp_set_end_text(uid: int, text: str):
    payload = {
        "user_id": uid,
        "end_text": text.strip(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        supabase.table("tg_user_text_addons").upsert(payload, on_conflict="user_id").execute()
    except Exception as ex:
        print(f"sp_set_end_text error for user {uid}: {ex}")

def sp_remove_texts(uid: int):
    try:
        supabase.table("tg_user_text_addons").delete().eq("user_id", uid).execute()
    except Exception as ex:
        print(f"sp_remove_texts error for user {uid}: {ex}")

def sp_get_text_addons(uid: int) -> dict:
    try:
        res = supabase.table("tg_user_text_addons").select("*").eq("user_id", uid).limit(1).execute()
        return res.data[0] if (res and res.data) else {"start_text": "", "end_text": ""}
    except Exception as ex:
        print(f"sp_get_text_addons error for user {uid}: {ex}")
        return {"start_text": "", "end_text": ""}

def sp_add_filter(uid: int, from_name: str, to_name: str) -> Tuple[bool, str]:
    from_name = from_name.strip(); to_name = to_name.strip()
    if not from_name or not to_name:
        return False, "⚠️ Dono words do: `/addfilter old==new`"

    if from_name.lower() == to_name.lower():
        return False, "⚠️ Left aur right same nahi ho sakte."

    try:
        supabase.table("tg_user_text_filters").insert({
            "user_id": uid,
            "from_name": from_name,
            "to_name": to_name,
        }).execute()
        return True, f"✅ Filter set: `{from_name}` → `{to_name}`"
    except Exception as ex:
        msg = str(ex).lower()
        if ("duplicate" in msg or "unique" in msg or
            "idx_user_text_filters_unique" in msg or "23505" in msg):
            return False, ("❌ Same left name already exists for this user.\n"
                           "Try another left value or remove the old one via `/removefilter old`.")
        return False, f"❌ Failed: {ex}"

def sp_list_filters(uid: int) -> List[dict]:
    try:
        res = supabase.table("tg_user_text_filters").select("*")\
            .eq("user_id", uid).order("created_at", desc=True).execute()
        return res.data or []
    except Exception as ex:
        print(f"sp_list_filters error for user {uid}: {ex}")
        return []

def sp_delete_filter(uid: int, from_name: str) -> Tuple[bool, str]:
    from_name = from_name.strip()
    if not from_name: return False, "⚠️ Use: `/removefilter old` (ya `@old`)"
    try:
        supabase.table("tg_user_text_filters").delete()\
            .eq("user_id", uid).eq("from_name_lower", from_name.lower()).execute()
        check = supabase.table("tg_user_text_filters").select("id").eq("user_id", uid)\
            .eq("from_name_lower", from_name.lower()).limit(1).execute()
        if check and check.data: return False, "❌ Could not remove (DB). Try again."
        return True, f"🗑️ Removed filter for `{from_name}`"
    except Exception as ex:
        print(f"sp_delete_filter error for user {uid}: {ex}")
        return False, f"❌ Could not remove filter: {ex}"

def sp_delete_filters_batch(uid: int, from_names: List[str]) -> int:
    if not from_names: return 0
    lowers = [fn.strip().lower() for fn in from_names if fn and fn.strip()]
    if not lowers: return 0
    try:
        supabase.table("tg_user_text_filters").delete().eq("user_id", uid)\
            .in_("from_name_lower", lowers).execute()
        rem = supabase.table("tg_user_text_filters").select("id").eq("user_id", uid)\
            .in_("from_name_lower", lowers).execute().data or []
        return max(0, len(lowers) - len(rem))
    except Exception as ex:
        print(f"sp_delete_filters_batch error for user {uid}: {ex}")
        return 0

# ---------- FILTERS: Compile & Apply ----------
def compile_filters_for_user(uid: int) -> List[Tuple[re.Pattern, str]]:
    rows = sp_list_filters(uid)
    rows.sort(key=lambda r: len(r.get("from_name") or ""), reverse=True)
    compiled = []
    for r in rows:
        src = (r.get("from_name") or "").strip()
        dst = (r.get("to_name") or "").strip()
        if not src or not dst: continue
        if src.startswith("@"):
            name = re.escape(src[1:])
            pat = re.compile(rf"(?i)(?<!\w)@{name}(?!\w)")
        else:
            name = re.escape(src)
            pat = re.compile(rf"(?i)(?<!\w){name}(?!\w)")
        compiled.append((pat, dst))
    return compiled

def apply_text_filters(text: str, compiled_filters: List[Tuple[re.Pattern, str]]) -> str:
    if not text: return text
    out = text
    for pat, repl in compiled_filters:
        out = pat.sub(repl, out)
    return out

# ---------- BLACKLIST: Compile & Apply ----------
def compile_blacklist_for_user(uid: int) -> List[re.Pattern]:
    rows = sp_list_blacklist(uid)
    rows.sort(key=lambda r: len(r.get("word") or ""), reverse=True)
    compiled: List[re.Pattern] = []
    for r in rows:
        w = (r.get("word") or "").strip()
        if not w:
            continue
        if w.startswith("@"):
            name = re.escape(w[1:])
            pat = re.compile(rf"(?i)(?<!\w)@{name}(?!\w)")
        else:
            name = re.escape(w)
            pat = re.compile(rf"(?i)(?<!\w){name}(?!\w)")
        compiled.append(pat)
    return compiled

def apply_blacklist(text: str, compiled_blacklist: List[re.Pattern]) -> str:
    if not text:
        return text
    out = text
    for pat in compiled_blacklist:
        out = pat.sub("", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()

# ---------- Sessions / Mappings / Delay ----------
def sp_get_session(uid: int) -> Optional[dict]:
    try:
        res = supabase.table("tg_user_sessions").select("*").eq("user_id", uid).limit(1).execute()
        return res.data[0] if (res and res.data) else None
    except Exception as ex:
        print(f"sp_get_session error for user {uid}: {ex}")
        return None

def sp_upsert_session(uid: int, phone: str, session_file: str):
    payload = {
        "user_id": uid, "phone": phone, "session_file": session_file, "is_active": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        supabase.table("tg_user_sessions").upsert(payload, on_conflict="user_id").execute()
    except Exception:
        try:
            existing = supabase.table("tg_user_sessions").select("user_id").eq("user_id", uid).limit(1).execute()
            if existing and existing.data:
                supabase.table("tg_user_sessions").update(payload).eq("user_id", uid).execute()
            else:
                supabase.table("tg_user_sessions").insert(payload).execute()
        except Exception as ex:
            print(f"sp_upsert_session fallback error for user {uid}: {ex}")

def sp_delete_session(uid: int):
    try:
        supabase.table("tg_user_sessions").delete().eq("user_id", uid).execute()
    except Exception as ex:
        print(f"sp_delete_session error for user {uid}: {ex}")

def sp_upsert_mapping(
    uid: int,
    sender_id: int,
    receivers: List[int],
    sender_name: Optional[str] = None,
    receivers_names: Optional[List[str]] = None,
):
    payload: Dict[str, Any] = {
        "user_id": uid,
        "sender_id": int(sender_id),
        "receivers": [int(x) for x in receivers],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    if sender_name is not None:
        payload["sender_name"] = sender_name
    if receivers_names is not None:
        payload["receivers_names"] = receivers_names

    try:
        supabase.table("tg_forward_mappings").upsert(
            payload, on_conflict="user_id,sender_id"
        ).execute()
    except Exception:
        try:
            supabase.table("tg_forward_mappings").delete()\
                .eq("user_id", uid).eq("sender_id", sender_id).execute()
            supabase.table("tg_forward_mappings").insert(payload).execute()
        except Exception as ex:
            print(f"sp_upsert_mapping error for user {uid}: {ex}")

def sp_load_rows(uid: int) -> List[dict]:
    try:
        return supabase.table("tg_forward_mappings").select("*").eq("user_id", uid).execute().data or []
    except Exception as ex:
        print(f"sp_load_rows error for user {uid}: {ex}")
        return []

def sp_load_mapping(uid: int) -> Dict[int, List[int]]:
    mp: Dict[int, List[int]] = {}
    for r in sp_load_rows(uid):
        mp[int(r["sender_id"])] = list(r.get("receivers") or [])
    return mp

def sp_delete_senders(uid: int, sender_ids: List[int]):
    for sid in sender_ids:
        try:
            supabase.table("tg_forward_mappings").delete().eq("user_id", uid).eq("sender_id", sid).execute()
        except Exception as ex:
            print(f"sp_delete_senders error for user {uid}, sid {sid}: {ex}")

def sp_remove_targets_globally(uid: int, target_ids: List[int]):
    rows = sp_load_rows(uid)
    kill = set(map(int, target_ids))

    for r in rows:
        rec = list(r.get("receivers") or [])
        rec_names = list(r.get("receivers_names") or [])
        sender_name = r.get("sender_name")

        new_rec: List[int] = []
        new_rec_names: List[str] = []

        for idx, chat_id in enumerate(rec):
            cid = int(chat_id)
            name = rec_names[idx] if idx < len(rec_names) else None
            if cid in kill:
                continue
            new_rec.append(cid)
            if name is not None:
                new_rec_names.append(name)

        if new_rec != rec:
            if new_rec:
                sp_upsert_mapping(
                    uid,
                    int(r["sender_id"]),
                    new_rec,
                    sender_name=sender_name,
                    receivers_names=new_rec_names,
                )
            else:
                try:
                    supabase.table("tg_forward_mappings").delete()\
                        .eq("user_id", uid).eq("sender_id", r["sender_id"]).execute()
                except Exception as ex:
                    print(f"sp_remove_targets_globally delete error for user {uid}: {ex}")

def sp_delete_all_filters(uid: int) -> int:
    try:
        rows = supabase.table("tg_user_text_filters").select("id").eq("user_id", uid).execute().data or []
        count = len(rows)
        if count:
            supabase.table("tg_user_text_filters").delete().eq("user_id", uid).execute()
        return count
    except Exception as ex:
        print(f"sp_delete_all_filters error for user {uid}: {ex}")
        return 0

def sp_set_delay(uid: int, seconds: int):
    payload = {
        "user_id": uid, "delay_seconds": int(seconds),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        supabase.table("tg_user_settings").upsert(payload, on_conflict="user_id").execute()
    except Exception:
        try:
            existing = supabase.table("tg_user_settings").select("user_id").eq("user_id", uid).limit(1).execute()
            if existing and existing.data:
                supabase.table("tg_user_settings").update(payload).eq("user_id", uid).execute()
            else:
                supabase.table("tg_user_settings").insert(payload).execute()
        except Exception as ex:
            print(f"sp_set_delay error for user {uid}: {ex}")

def sp_get_delay(uid: int) -> Optional[int]:
    try:
        res = supabase.table("tg_user_settings").select("delay_seconds").eq("user_id", uid).limit(1).execute()
        data = res.data or []
        if not data: return None
        return int(data[0].get("delay_seconds") or 0)
    except Exception as ex:
        print(f"sp_get_delay error for user {uid}: {ex}")
        return None

def sp_get_forwarding_users() -> List[int]:
    """
    Return list of user_ids jinke user_settings me is_forwarding = true hai.
    Restart ke baad isi list se auto-resume karenge.
    """
    try:
        res = supabase.table("tg_user_settings").select("user_id", "is_forwarding")\
            .eq("is_forwarding", True).execute()
        rows = res.data or []
        uids: List[int] = []
        for r in rows:
            uid = r.get("user_id")
            if uid is not None:
                try:
                    uids.append(int(uid))
                except Exception:
                    pass
        return uids
    except Exception as ex:
        print("sp_get_forwarding_users error:", ex)
        return []


# ---------- DASHBOARD SUBSCRIPTION HELPERS (ONLY app_user_subscriptions) ----------

def sp_get_app_subscription_by_telegram_user_id(uid_tg: int) -> Optional[dict]:
    """
    Dashboard creates/updates app_user_subscriptions.
    Bot only reads it by telegram_user_id.
    """
    try:
        res = supabase.table("app_user_subscriptions") \
            .select("*") \
            .eq("telegram_user_id", int(uid_tg)) \
            .limit(1) \
            .execute()
        rows = res.data or []
        return rows[0] if rows else None
    except Exception as ex:
        print("sp_get_app_subscription_by_telegram_user_id error:", ex)
        return None

def sp_is_sub_active(app_sub: Optional[dict]) -> bool:
    if not app_sub:
        return False
    exp_raw = app_sub.get("expires_at")
    if not exp_raw:
        return False
    try:
        # handles "2026-01-15 08:24:56.217+00" and ISO formats
        exp_str = str(exp_raw).replace("Z", "+00:00").replace(" ", "T", 1)
        exp_dt = datetime.fromisoformat(exp_str)
        return exp_dt > datetime.now(timezone.utc)
    except Exception:
        return False

def get_active_subscription(uid_tg: int) -> Optional[dict]:
    """Return app_user_subscriptions row only if active."""
    sub = sp_get_app_subscription_by_telegram_user_id(uid_tg)
    return sub if sp_is_sub_active(sub) else None

def sp_has_used_demo(uid_tg: int) -> bool:
    """Check if user has already used their one-time demo."""
    sub = sp_get_app_subscription_by_telegram_user_id(uid_tg)
    if not sub:
        return False
    # demo_used true OR demo_used_at present => already used
    if sub.get("demo_used") is True:
        return True
    return bool(sub.get("demo_used_at"))

def sp_grant_demo(uid_tg: int) -> Tuple[bool, str]:
    """
    Grants 7-day demo by updating existing app_user_subscriptions row
    (requires user to be linked already via /start <uuid>).
    """
    sub = sp_get_app_subscription_by_telegram_user_id(uid_tg)
    if not sub:
        return False, (
            "⚠️ Before starting demo, you must link your dashboard account to the bot.\n\n"
            "Dashboard → Connect Bot → run /start <uuid> in the bot."
        )

    # if already active (paid or demo), don't override
    if sp_is_sub_active(sub):
        plan_label = (sub.get("plan_label") or sub.get("plan_id") or "Active Plan").strip()
        return False, f"✅ Your plan is already active: **{plan_label}**"

    if sp_has_used_demo(uid_tg):
        return False, "⚠️ Demo already used. Demo can only be used once."

    now = datetime.now(timezone.utc)
    exp = now + timedelta(days=7)  # 7-day demo

    payload = {
        "telegram_user_id": int(uid_tg),
        "started_at": now.isoformat(),
        "expires_at": exp.isoformat(),
        "total_cycles": int(sub.get("total_cycles") or 0) + 1,
        "last_payment_id": "DEMO",
        "last_payment_status": "demo",
        "last_paymentlink_id": "DEMO",
        "last_paymentlink_url": "",
        "last_payment_verified_at": now.isoformat(),
        "plan_id": "demo_premium",
        "plan_label": "🎁 DEMO — PREMIUM (7 days)",
        "plan_price_paise": 0,
        "plan_duration_days": 7,
        "demo_used": True,
        "demo_used_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }

    try:
        # Update row by PK if present, else by telegram_user_id
        # (PK is user_id, but we don't have it here; safer to update via telegram_user_id)
        supabase.table("app_user_subscriptions") \
            .update(payload) \
            .eq("telegram_user_id", int(uid_tg)) \
            .execute()
        return True, "🎁 7-day DEMO Activated! You now have full bot access ✅"
    except Exception as ex:
        return False, f"❌ Demo could not be activated: `{ex}`"

def sp_link_telegram_to_app_user(user_uuid: str, telegram_user_id: int) -> bool:
    try:
        now = datetime.now(timezone.utc).isoformat()
        supabase.table("app_user_subscriptions").upsert(
            {
                "user_id": user_uuid,
                "telegram_user_id": int(telegram_user_id),
                "updated_at": now,
            },
            on_conflict="user_id"
        ).execute()
        return True
    except Exception as ex:
        print("sp_link_telegram_to_app_user error:", ex)
        return False

def sp_get_app_subscription_by_user_id(user_uuid: str) -> dict | None:
    try:
        res = supabase.table("app_user_subscriptions") \
            .select("*") \
            .eq("user_id", user_uuid) \
            .limit(1) \
            .execute()
        rows = res.data or []
        return rows[0] if rows else None
    except Exception as ex:
        print("sp_get_app_subscription_by_user_id error:", ex)
        return None


def format_app_plan_block(app_sub: dict | None) -> str:
    """
    Output:
    ✅ Your Plan is ACTIVE
    Plan name: Pro Semi-Annual
    Expires on: 14 Jul 2026
    """
    if not app_sub:
        return "⚠️ No dashboard subscription found for this account.\n"

    plan_label = (app_sub.get("plan_label") or app_sub.get("plan_id") or "—").strip()

    exp_raw = app_sub.get("expires_at")
    status_line = "⚠️ Your Plan status is UNKNOWN"
    exp_line = ""

    try:
        if exp_raw:
            # handle both "2026-01-15 08:24:56.217+00" and iso formats
            exp_str = str(exp_raw).replace("Z", "+00:00").replace(" ", "T", 1)
            exp_dt = datetime.fromisoformat(exp_str)
            now = datetime.now(timezone.utc)

            if exp_dt >= now:
                status_line = "✅ Your Plan is *ACTIVE*"
            else:
                status_line = "❌ Your Plan is *EXPIRED*"

            exp_line = f"Expires on: `{exp_dt.strftime('%d %b %Y')}`"
    except Exception:
        pass

    lines = [
        status_line,
        f"Plan name: **{plan_label}**",
    ]
    if exp_line:
        lines.append(exp_line)

    return "\n".join(lines) + "\n"


# ---------------- TELEGRAM HELPERS ----------------
def session_path(uid: int, phone: str) -> str:
    digits = "".join([c for c in phone if c.isdigit()])
    return os.path.join(SESSION_DIR, f"{uid}_{digits}.session")



async def get_user_client(uid: int) -> TelegramClient:
    """
    SAME user ke liye SAME TelegramClient reuse karega.
    Isse ek hi .session file ko multiple clients access nahi karenge.
    """

    # 1️⃣ Pehle cache se try karo
    client = USER_CLIENT_CACHE.get(uid)
    if client:
        try:
            # Agar already connected hai → direct return
            if client.is_connected():
                return client
        except Exception:
            pass

        # Agar cache me hai lekin disconnected lag raha hai → reconnect try karo
        try:
            await safe_connect(client)
            if await client.is_user_authorized():
                return client
        except Exception:
            # Agar reconnect bhi fail ho gaya → is client ko hata do
            try:
                await client.disconnect()
            except:
                pass
            USER_CLIENT_CACHE.pop(uid, None)

    # 2️⃣ Agar cache me valid client nahi mila → DB se session file lo
    sess = sp_get_session(uid)
    if not sess:
        raise RuntimeError("No saved session. Use /login first.")

    local = os.path.join(SESSION_DIR, sess["session_file"])
    client = TelegramClient(local, API_ID, API_HASH)

    # ✅ safer connect (handles slow network / reconnects)
    await safe_connect(client)

    # Authorized check
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("Session exists but not authorized. /login again.")

    # 3️⃣ Ab is new client ko cache me daal do future ke liye
    USER_CLIENT_CACHE[uid] = client
    return client

async def is_logged_in(uid: int) -> bool:
    """
    Sirf ye check karega ki user ka session se valid client mil raha hai ya nahi.
    Yahan client ko disconnect *nahi* karenge, taki /work ya dusre flows me
    same cached client reuse ho sake.
    """
    try:
        _ = await get_user_client(uid)
        return True
    except Exception:
        return False


def title_of(ent) -> str:
    if getattr(ent, "title", None): return ent.title
    fn = getattr(ent, "first_name", "") or ""
    ln = getattr(ent, "last_name", "") or ""
    if fn or ln: return (fn + " " + ln).strip()
    if getattr(ent, "username", None): return "@" + ent.username
    return f"id:{getattr(ent, 'id', '')}"

async def top_dialog_pairs(client: TelegramClient, limit: int = TOP_N) -> List[Tuple[int, str]]:
    dialogs = await client.get_dialogs(limit=200)
    return [(int(get_peer_id(d.entity)), title_of(d.entity)) for d in dialogs[:limit]]

async def titles_for_ids(client: TelegramClient, ids: List[int]) -> List[str]:
    names = []
    for _id in ids:
        try:
            ent = await client.get_entity(int(_id))
            names.append(title_of(ent))
        except Exception:
            names.append(f"id:{_id}")
    return names

def numbered_list_from_pairs(pairs: List[Tuple[int, str]]) -> str:
    return "\n".join([f"{i+1}. {pairs[i][1]}" for i in range(len(pairs))])

def multi_kb(n: int, selected: Set[int]) -> List[List[Button]]:
    rows, row = [], []
    for i in range(1, n + 1):
        label = f"{'✅ ' if (i - 1) in selected else ''}{i}"
        row.append(Button.inline(label, data=f"msel:{i}".encode()))
        if len(row) == 7:
            rows.append(row); row = []
    if row: rows.append(row)
    rows.append([Button.inline("✅ Done", data=b"msel_done"),
                 Button.inline("✖ Cancel", data=b"msel_cancel")])
    return rows


# ---------------- BOT PROFILE ----------------

async def setup_bot_profile():
    """
    Register bot commands with Telegram.
    Note: SetBotInfoRequest is only for bot owners (user accounts) via MTProto and errors out
    if called by the bot itself. Bot commands are set via SetBotCommandsRequest.
    """
    try:
        await bot(SetBotCommandsRequest(
            scope=BotCommandScopeDefault(),
            lang_code="en",
            commands=[BotCommand(cmd, desc) for cmd, desc in COMMANDS]
        ))
        print("✅ Telegram bot commands registered successfully.")
    except Exception as e:
        print("⚠️ Telegram bot commands set warning:", e)

# ---------------- GUARDS ----------------
async def guard_or_hint(e) -> bool:
    uid = int(e.sender_id)

    # If the user is logged in locally, allow
    if await is_logged_in(uid):
        return True

    # If user has an active dashboard subscription, allow
    sub = get_active_subscription(uid)
    if sub:
        return True

    # Otherwise block
    btns = []
    if DASHBOARD_PLANS_URL:
        btns = [[Button.url("🛒 Buy / Manage Plan (Dashboard)", DASHBOARD_PLANS_URL)]]

    await e.respond(
        "🔒 Access blocked.\n\n"
        "✅ First purchase a plan from the dashboard, then you can use the bot.\n"
        "If already purchased, ensure your Telegram User ID is linked in the dashboard.\n\n"
        "Use: /my_id (to see your Telegram ID)",
        buttons=btns if btns else None
    )
    return False

# ---------------- PLAN / COMMAND ACCESS MATRIX ----------------

# ---------------- PLAN / COMMAND ACCESS MATRIX ----------------

PLAN_LEVEL = {
    "free": 0,
    "basic": 1,
    "pro": 2,
    "premium": 3,
}

# Sab free commands
ALWAYS_ALLOWED = {
    "start","start_demo","help","login","status","config","tg_plans","upgrade",
    "upgrade_status","logout","stoplogin",
    "remove_incoming","remove_outgoing","removefilter","removedelay",
    "remove_text","remove_blacklist"
}

# Har command ke liye minimum required plan
COMMAND_MIN_PLAN = {
    # BASIC
    "incoming": "basic",
    "outgoing": "basic",
    "work": "basic",
    "stop": "basic",

    # PRO
    "addfilter": "pro",
    "showfilter": "pro",
    "deleteallfilters": "pro",
    "delay": "pro",

    # PREMIUM
    "start_text": "premium",
    "end_text": "premium",
    "blacklist_word": "premium",
}

def get_user_plan_level(uid: int):
    sub = get_active_subscription(uid)
    if not sub:
        return "free", PLAN_LEVEL["free"]

    pid = str(sub.get("plan_id") or "").lower()

    # demo = premium access
    if pid in ("demo_premium", "demo", "trial"):
        pid = "premium"

    if pid not in PLAN_LEVEL:
        pid = "premium"  # fallback for old users

    return pid, PLAN_LEVEL[pid]


async def premium_or_hint(e) -> bool:
    raw = (e.raw_text or "").strip()
    cmd = (
        raw.split()[0].lstrip("/").split("@")[0].lower()
        if raw.startswith("/")
        else ""
    )

    # free commands to sab ke liye allowed
    if not cmd or cmd in ALWAYS_ALLOWED:
        return True

    # is command ka minimum plan
    required_plan = COMMAND_MIN_PLAN.get(cmd, "premium")
    required_level = PLAN_LEVEL[required_plan]

    # user ka plan
    user_plan, user_level = get_user_plan_level(int(e.sender_id))

    # agar allowed → continue
    if user_level >= required_level:
        return True

    # warna upgrade message
    await e.respond(
        f"🔒 **Plan Upgrade Required**\n\n"
        f"Your plan: **{user_plan.upper()}**\n"
        f"Required: **{required_plan.upper()}**\n\n"
        f"Use /plans to upgrade.",
        buttons=[[Button.inline("💳 View Plans", data=b"plans_back")]],
        parse_mode="md"
    )
    return False


# ---------------- PUBLIC COMMANDS ----------------
@bot.on(events.NewMessage(pattern="/my_id"))
async def my_id_command(event):
    user = await event.get_sender()
    username = f"@{user.username}" if user.username else "Username Not Found ❌"

    reply = (
        "🆔 *Your Telegram Details*\n\n"
        f"👤 Name : *{user.first_name}*\n"
        f"🔗 Username : {username}\n"
        f"🧾 User ID : `{user.id}`"
    )
    await event.reply(reply, parse_mode="md")

@bot.on(events.NewMessage(pattern=r"^/start(?:\s+(.+))?$"))
async def start_cmd(e):
    uid_tg = int(e.sender_id)

    # payload (deeplink) like: /start <uuid>
    payload = None
    try:
        parts = (e.raw_text or "").strip().split(maxsplit=1)
        if len(parts) == 2:
            payload = parts[1].strip()
    except Exception:
        payload = None

    # ✅ Normal /start → ONLY commands (no plan/status)
    if not payload:
        return await e.respond(commands_text(), parse_mode="md")

    # ✅ Deep link /start <uuid> → map telegram_user_id + show plan block
    linked_msg = ""
    ok = sp_link_telegram_to_app_user(payload, uid_tg)

    if not ok:
        msg = (
            "⚠️ Invalid or expired dashboard link.\n"
            "Please open your dashboard and click **Connect Bot** again.\n\n"
            + commands_text()
        )
        return await e.respond(msg, parse_mode="md")

    # only fetch subscription if link succeeded
    app_sub = sp_get_app_subscription_by_user_id(payload)

    linked_msg = "✅ Dashboard account linked successfully!\n\n"

    msg = (
        linked_msg
        + format_app_plan_block(app_sub)
        + "\n"
        + commands_text()
    )
    await e.respond(msg, parse_mode="md")

@bot.on(events.NewMessage(pattern=r"^/start_demo$"))
async def cmd_start_demo(e):
    uid_tg = int(e.sender_id)

    # --- Subscription status at top ---
    app_sub = sp_get_app_subscription_by_telegram_user_id(uid_tg)
    status_block = format_app_plan_block(app_sub)

    txt = (
        f"{status_block}\n"
        "🎁 **Try AutoForward Bot — Free for 7 Days!**\n\n"
        "You can get a **free 7-day demo** of the Auto Message Forwarder bot\n"
        "by taking the **Free Trial plan** from the GetAIPilot plan page.\n\n"
        "• Forward messages between any chats\n"
        "• Set incoming & outgoing channels\n"
        "• Add prefix & suffix text to forwards\n"
        "• Blacklist words\n"
        "• Delay in messages\n"
        "• Text replace filters\n"
        "• All media types supported\n"
        "• Start / Stop anytime\n"
        "\n"
        "👇 Click the button below to get your free trial!"
    )

    await e.respond(
        txt,
        parse_mode="md",
        buttons=[
            [Button.url("🆓 Free 7 Days Trial", "https://getaipilot.in/pricing")],
        ],
    )


@bot.on(events.NewMessage(pattern=r"^/help$"))
async def help_cmd(e):
    # Provide a help screen that points users to the full guide and direct contact
    guide_url = "https://drive.google.com/file/d/1aRlsYkvofEtbn9YxCo35wm-raYPiBaZY/view?usp=sharing"
    txt = (
        "🔧 *Help & Support*\n\n"
        "Use the buttons below to get help:"
        "\n\n• *View Guide* — Open the full PDF user guide with detailed features and steps."
        "\n• *Contact & Support* — Get company email, phone and WhatsApp to reach support directly."
    )
    await e.respond(
        txt,
        parse_mode="md",
        buttons=[
            [Button.url("📄 View Guide", guide_url)],
            [Button.inline("💬 Contact & Support", data=b"help_contact")],
        ],
    )


@bot.on(events.CallbackQuery(pattern=b"^help_contact$"))
async def cb_help_contact(event):
    # Show direct contact information and provide a back button to the help screen
    contact_txt = (
        "💬 *Contact & Support*\n\n"
        "Company: GetAipilot\n"
        "Email: `support@getaipilot.com`\n\n"
        "Phone / WhatsApp: `+91 89822 85510`\n"
        "Location: Bhopal, India\n"
        "Website: https://getaipilot.com"
    )
    await event.edit(contact_txt, parse_mode="md", buttons=[[Button.inline("🔙 Back", data=b"help_back")]])


@bot.on(events.CallbackQuery(pattern=b"^help_back$"))
async def cb_help_back(event):
    guide_url = "https://drive.google.com/file/d/1aRlsYkvofEtbn9YxCo35wm-raYPiBaZY/view?usp=sharing"
    txt = (
        "🔧 *Help & Support*\n\n"
        "Use the buttons below to get help:"
        "\n\n• *View Guide* — Open the full PDF user guide with detailed features and steps."
        "\n• *Contact & Support* — Get company email, phone and WhatsApp to reach support directly."
    )
    await event.edit(
        txt,
        parse_mode="md",
        buttons=[
            [Button.url("📄 View Guide", guide_url)],
            [Button.inline("💬 Contact & Support", data=b"help_contact")],
        ],
    )

@bot.on(events.NewMessage(pattern=r"^/status$"))
async def status_cmd(e):
    data = sp_get_session(e.sender_id)
    if not data:
        return await e.respond("🔴 Not logged in. Use **/login** to connect your account.", parse_mode="md")
    if not await is_logged_in(e.sender_id):
        return await e.respond("🟠 Session found but not authorized locally. Please **/login** again.", parse_mode="md")
    await e.respond(f"🟢 Logged In\n**Phone:** {data['phone']}\n`{data['session_file']}`", parse_mode="md")

# ---------------- LOGIN FLOW (2FA SUPPORTED) ----------------
@bot.on(events.NewMessage(pattern=r"^/login$"))
async def login_cmd(e):
    uid = e.sender_id
    if await is_logged_in(uid):
        return await e.respond(
            "✅ **Already logged in.**\n"
            "Set chats: **/incoming** & **/outgoing**\n"
            "Start forwarding: **/work**"
        )
    login_state[uid] = {"step": "phone", "phone": None}
    await e.respond("📲 Send your phone number `+919876543210` in this format.\n\nYou can cancel login anytime with `/stoplogin`.")



@bot.on(events.NewMessage)
async def login_flow(e):
    uid = e.sender_id
    msg = (e.raw_text or "").strip()
    if uid not in login_state:
        return
    st = login_state[uid]

    # STEP: PHONE
    if st["step"] == "phone":
        if not PHONE_RE.match(msg):
            return await e.respond("⚠️ Please send a valid number like - `+919876543210`.")
        phone = msg
        local = session_path(uid, phone)
        client = TelegramClient(local, API_ID, API_HASH)
        st["phone"] = phone
        try:
            await client.connect()
            if await client.is_user_authorized():
                me = await client.get_me()
                sp_upsert_session(uid, phone, os.path.basename(local))
                await e.respond(f"✅ Already logged in as **{me.first_name}**.\nStart forwarding with **/work**.")
                login_state.pop(uid, None)
                return
            res = await client.send_code_request(phone)
            st["phone_code_hash"] = getattr(res, "phone_code_hash", None)
            await e.respond(
                "📩 Send OTP in this format: `LOGIN123456`.\n\n"
                "Didn't get the OTP? Resend by clicking the button 👇\n\n"
                "Cancel login with `/stoplogin` anytime.",
                buttons=[[Button.inline("🔁 Resend OTP", data=b"resend_otp")]]
            )
            st["step"] = "otp"
        except Exception as ex:
            await e.respond(f"❌ OTP send error: `{ex}`\nStart again with /login.")
            print("send_code_request error:", ex)
        finally:
            try: await client.disconnect()
            except: pass
        return

    # STEP: OTP
    if st["step"] == "otp":
        m = OTP_RE.match(msg)
        if not m:
            return await e.respond("⚠️ Send OTP in `123456` or `LOGIN123456` format.")
        otp = m.group(1)
        phone = st.get("phone")
        if not phone:
            login_state.pop(uid, None)
            return await e.respond("⚠️ Phone missing. Start /login again.")
        code_hash = st.get("phone_code_hash")
        if not code_hash:
            login_state.pop(uid, None)
            return await e.respond("❌ Phone code session expired or missing. Start again with /login.")

        local = session_path(uid, phone)
        client = TelegramClient(local, API_ID, API_HASH)
        try:
            await client.connect()
            try:
                await client.sign_in(phone, otp, phone_code_hash=code_hash)
                me = await client.get_me()
                sp_upsert_session(uid, phone, os.path.basename(local))
                await e.respond(
                    f"✅ Logged in as **{me.first_name}**.\n"
                    "Now set **/incoming** & **/outgoing**, then **/work** to start."
                )
                login_state.pop(uid, None)
                return
            except errors.SessionPasswordNeededError:
                try:
                    pwd = await client(functions.account.GetPasswordRequest())
                    hint = getattr(pwd, "hint", "") or ""
                except Exception:
                    hint = ""
                st["step"] = "2fa"
                st["twofa_session_path"] = local
                msg_hint = f" (hint: `{hint}`)" if hint else ""
                await e.respond(
                    f"🔐 2FA enabled. Please enter your **Telegram password**{msg_hint}.\n\n"
                    "You can cancel login with `/stoplogin` if you wish.\n\n"
                    "_We won't store your password; it's used once to finish login._"
                )
                return
        except errors.PhoneCodeInvalidError:
            await e.respond("❌ Wrong OTP. `/login` try again.")
        except Exception as ex:
            await e.respond(f"❌ Login failed: `{ex}`\nStart again with /login.")
            print("sign_in error:", ex)
        finally:
            try: await client.disconnect()
            except: pass
        return

    # STEP: 2FA PASSWORD
    if st["step"] == "2fa":
        password = msg
        phone = st.get("phone")
        local = st.get("twofa_session_path") or session_path(uid, phone or "")
        if not phone or not local:
            login_state.pop(uid, None)
            return await e.respond("⚠️ Session expired. Start /login again.")
        client = TelegramClient(local, API_ID, API_HASH)
        try:
            await client.connect()
            await client.sign_in(password=password)
            me = await client.get_me()
            sp_upsert_session(uid, phone, os.path.basename(local))
            await e.respond(
                f"✅ 2FA verified. Logged in as **{me.first_name}**.\n"
                "Start forwarding with **/incoming** & **/outgoing**, then **/work**."
            )
        except errors.PasswordHashInvalidError:
            return await e.respond("❌ Wrong password, try again (or `/login` to restart).")
        except Exception as ex:
            await e.respond(f"❌ 2FA login failed: `{ex}`\nUse `/login` to reset and try again.")
            print("2FA sign_in error:", ex)
        finally:
            login_state.pop(uid, None)
            try: await client.disconnect()
            except: pass
        return

# ---------------- PREMIUM-GATED COMMANDS ----------------
@bot.on(events.NewMessage(pattern=r"^/incoming$"))
async def incoming_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    await e.respond(
        "📥 **Incoming (Sources)**\n"
        "First, pin the chats you want to use. Only the top 14 will be shown.\n\n"
        "Ready? Tap:",
        buttons=[Button.inline("📌 I have pinned the chats", data=b"pin_incoming")]
    )

@bot.on(events.NewMessage(pattern=r"^/outgoing$"))
async def outgoing_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    await e.respond(
        "📤 **Outgoing (Targets)**\n"
        "Pin the chats you want to forward to. Only the top 14 will be shown.\n\n"
        "Ready? Tap:",
        buttons=[Button.inline("📌 I have pinned the chats", data=b"pin_outgoing")]
    )

@bot.on(events.CallbackQuery(pattern=b"^pin_incoming$"))
async def cb_incoming(event):
    uid = event.sender_id
    try:
        uc = await get_user_client(uid)
    except Exception:
        return await event.answer("Please login first using /login", alert=True)

    pairs = await top_dialog_pairs(uc, TOP_N)

    # 🔹 Fetch already saved incoming chats (sources) from the database
    existing_mapping = sp_load_mapping(uid)  # sender_id -> [receivers]
    existing_sources = set(int(sid) for sid in existing_mapping.keys())

    # 🔹 Pre-select chats that are already saved as incoming (show with a checkmark)
    pre_selected: Set[int] = set()
    for idx, (chat_id, _title) in enumerate(pairs):
        if int(chat_id) in existing_sources:
            pre_selected.add(idx)

    select_state[uid] = {
        "mode": "incoming",
        "pairs": pairs,
        "selected": pre_selected,
        "incoming_ids": [],
        "outgoing_ids": [],
    }

    await event.edit(
        "📥 Select **INCOMING** chats (multi-select).\n"
        "Chats that are already saved will appear with a ✅ checkmark.\n"
        "Tap the numbers to toggle selection, then press **Done**.\n\n"
        + numbered_list_from_pairs(pairs),
        buttons=multi_kb(len(pairs), pre_selected),
    )

@bot.on(events.CallbackQuery(pattern=b"^pin_outgoing$"))
async def cb_outgoing(event):
    uid = event.sender_id
    try:
        uc = await get_user_client(uid)
    except Exception:
        return await event.answer("Please login first using /login", alert=True)

    pairs = await top_dialog_pairs(uc, TOP_N)
    st = select_state.get(uid, {"incoming_ids": [], "outgoing_ids": []})

    # 🔹 Fetch already saved outgoing chats (targets) from the database
    existing_mapping = sp_load_mapping(uid)
    existing_targets: Set[int] = set()
    for _src, tgt_list in existing_mapping.items():
        for t in tgt_list or []:
            existing_targets.add(int(t))

    # 🔹 Pre-select chats that are already saved as outgoing (show with a checkmark)
    pre_selected: Set[int] = set()
    for idx, (chat_id, _title) in enumerate(pairs):
        if int(chat_id) in existing_targets:
            pre_selected.add(idx)

    select_state[uid] = {
        "mode": "outgoing",
        "pairs": pairs,
        "selected": pre_selected,
        "incoming_ids": st.get("incoming_ids", []),
        "outgoing_ids": [],
    }

    await event.edit(
        "📤 Select **OUTGOING** chats (multi-select).\n"
        "Chats that are already saved will appear with a ✅ checkmark.\n"
        "Tap the numbers to toggle selection, then press **Done**.\n\n"
        + numbered_list_from_pairs(pairs),
        buttons=multi_kb(len(pairs), pre_selected),
    )

@bot.on(events.CallbackQuery(pattern=b"^msel:"))
async def cb_toggle(event):
    uid = event.sender_id
    st = select_state.get(uid)
    if not st:
        return await event.answer("Expired. Use /incoming or /outgoing.", alert=True)
    try:
        idx = int(event.data.decode().split(":")[1]) - 1
    except Exception:
        return await event.answer("Invalid.", alert=True)
    if idx < 0 or idx >= len(st["pairs"]):
        return await event.answer("Out of range.", alert=True)

    sel: Set[int] = st["selected"]
    if idx in sel: sel.remove(idx)
    else: sel.add(idx)

    if st["mode"] == "incoming": header = "📥 Select **INCOMING** chats"
    elif st["mode"] == "outgoing": header = "📤 Select **OUTGOING** chats"
    elif st["mode"] == "remove_in": header = "❌ Select **INCOMING sources** to remove"
    elif st["mode"] == "remove_out": header = "❌ Select **OUTGOING targets** to remove"
    elif st["mode"] == "remove_filter": header = "🗑️ Select **filters** to remove"
    elif st["mode"] == "remove_blacklist": header = "🗑️ Select **blacklisted words** to remove"

    else: header = "Select items"

    await event.edit(
        f"{header} (multi-select). Numbers toggle, then **Done**.\n\n"
        + numbered_list_from_pairs(st["pairs"]),
        buttons=multi_kb(len(st["pairs"]), sel),
    )

# ---------------- MULTI-SELECT DONE HANDLER (with remove_blacklist) ----------------
@bot.on(events.CallbackQuery(pattern=b"^msel_done$"))
async def cb_msel_done(event):
    uid = event.sender_id
    st = select_state.get(uid)
    if not st:
        return await event.answer("Session expired. Start again.", alert=True)

    # --- remove_filter (existing) ---
    if st["mode"] == "remove_filter":
        chosen = sorted(st["selected"])
        rows = st.get("filter_rows", [])
        to_remove = []
        for i in chosen:
            if 0 <= i < len(rows):
                fn = (rows[i].get("from_name") or "").strip()
                if fn:
                    to_remove.append(fn)
        removed = sp_delete_filters_batch(uid, to_remove)
        select_state.pop(uid, None)
        if uid in forward_loops:
            forward_loops[uid]["filters"] = compile_filters_for_user(uid)
        pretty = "\n".join([f"- `{x}`" for x in to_remove]) or "(none)"
        return await event.edit(f"✅ Removed **{removed}** filter(s):\n{pretty}", buttons=None)

    # --- remove_blacklist (NEW) ---
    if st["mode"] == "remove_blacklist":
        chosen = sorted(st["selected"])
        rows = st.get("blacklist_rows", [])
        to_remove: List[str] = []
        for i in chosen:
            if 0 <= i < len(rows):
                w = (rows[i].get("word") or "").strip()
                if w:
                    to_remove.append(w)
        removed = sp_delete_blacklist_batch(uid, to_remove)
        select_state.pop(uid, None)
        if uid in forward_loops:
            forward_loops[uid]["blacklist"] = compile_blacklist_for_user(uid)
        pretty = "\n".join([f"- `{x}`" for x in to_remove]) or "(none)"
        return await event.edit(f"✅ Removed **{removed}** blacklisted word(s):\n{pretty}", buttons=None)

    # --- rest of the modes (incoming/outgoing/remove_in/remove_out) ---
    chosen_idxs = sorted(st.get("selected", []))
    chosen_ids = [st["pairs"][i][0] for i in chosen_idxs if 0 <= i < len(st.get("pairs", []))]

    if st["mode"] == "incoming":
        st["incoming_ids"] = chosen_ids
        st["selected"] = set()
        return await event.edit("✅ **Incoming set!**\n\nNow send **/outgoing**.", buttons=None)

    elif st["mode"] == "outgoing":
        st["outgoing_ids"] = chosen_ids
        st["selected"] = set()
        return await event.edit("✅ **Outgoing set!**\n\nStart with **/work**.", buttons=None)

    elif st["mode"] == "remove_in":
        sp_delete_senders(uid, chosen_ids)
        select_state.pop(uid, None)
        return await event.edit("✅ Selected **incoming sources** removed.", buttons=None)

    elif st["mode"] == "remove_out":
        sp_remove_targets_globally(uid, chosen_ids)
        select_state.pop(uid, None)
        return await event.edit("✅ Selected **outgoing targets** removed from all mappings.", buttons=None)


@bot.on(events.CallbackQuery(pattern=b"^msel_cancel$"))
async def cb_cancel(event):
    select_state.pop(event.sender_id, None)
    await event.edit("✖ Cancelled. Use **/incoming** or **/outgoing** again.")

@bot.on(events.NewMessage(pattern=r"^/remove_incoming$"))
async def remove_incoming_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    uid = e.sender_id
    mapping = sp_load_mapping(uid)
    senders = list(mapping.keys())
    if not senders:
        return await e.respond("ℹ️ No incoming sources saved.")
    # Try to resolve names via user client; if not available, fall back to id:NNN
    names = []
    try:
        uc = await get_user_client(uid)
        names = await titles_for_ids(uc, senders)
    except Exception:
        names = [f"id:{s}" for s in senders]
    pairs = list(zip(senders, names))
    select_state[uid] = {"mode": "remove_in", "pairs": pairs, "selected": set()}
    await e.respond(
        "❌ Select **INCOMING sources** to remove (multi-select) and press **Done**.\n\n" +
        numbered_list_from_pairs([(sid, nm) for sid, nm in pairs]),
        buttons=multi_kb(len(pairs), set())
    )

@bot.on(events.NewMessage(pattern=r"^/remove_outgoing$"))
async def remove_outgoing_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    uid = e.sender_id
    mapping = sp_load_mapping(uid)
    all_targets: List[int] = sorted({int(t) for lst in mapping.values() for t in lst})
    if not all_targets:
        return await e.respond("ℹ️ No outgoing targets saved.")
    # resolve names if possible
    names = []
    try:
        uc = await get_user_client(uid)
        names = await titles_for_ids(uc, all_targets)
    except Exception:
        names = [f"id:{t}" for t in all_targets]
    pairs = list(zip(all_targets, names))
    select_state[uid] = {"mode": "remove_out", "pairs": pairs, "selected": set()}
    await e.respond(
        "❌ Select **OUTGOING targets** to remove (multi-select) and press **Done**.\n\n" +
        numbered_list_from_pairs([(tid, nm) for tid, nm in pairs]),
        buttons=multi_kb(len(pairs), set())
    )

@bot.on(events.NewMessage(pattern=r"^/config$"))
async def cmd_config(e):
    # intentionally NOT premium-gated per your requirement
    if not await guard_or_hint(e):
        return

    uid = e.sender_id
    uid_tg = int(e.sender_id)  # ✅ FIX ADDED
    mapping = sp_load_mapping(uid)
    if not mapping:
        return await e.respond("ℹ️ No configuration yet. First set **/incoming** & **/outgoing**.")

    lines = ["**This is your current configuration:**", ""]

    # 1) Pehle try karo ki user ka client mile
    try:
        uc = await get_user_client(uid)
    except Exception:
        uc = None

    # 2) Har mapping ke liye friendly name nikalne ki try, warna id se fallback
    for src, tgts in mapping.items():
        # SOURCE name
        if uc:
            try:
                src_name = (await titles_for_ids(uc, [src]))[0]
            except Exception:
                src_name = f"id:{src}"
        else:
            src_name = f"id:{src}"

        # TARGET names
        tgt_labels = []
        for t in tgts:
            if uc:
                try:
                    n = (await titles_for_ids(uc, [t]))[0]
                    tgt_labels.append(n)
                except Exception:
                    tgt_labels.append(f"id:{t}")
            else:
                tgt_labels.append(f"id:{t}")

        lines.append(f"- **COPYING from:** `{src_name}`")
        if tgt_labels:
            pretty_targets = ", ".join([f"`{n}`" for n in tgt_labels])
        else:
            pretty_targets = "`[]`"
        lines.append(f"  **→ TARGETING to:** {pretty_targets}")
        lines.append("")

    if not uc:
        lines.append("_Tip: Use `/login` on this device to see channel names instead of IDs._")
        lines.append("")


    # --- Filters (text replacements) ---
    filters = sp_list_filters(uid)
    lines.append("**Filters (text replacements):**")
    if not filters:
        lines.append("(none)")
    else:
        for r in filters:
            lines.append(f"- `{r.get('from_name')}` → `{r.get('to_name')}`")
    lines.append("")

    # --- Delay ---
    delay = sp_get_delay(uid) or 0
    lines.append(f"**Delay between forwards:** `{delay}s`")
    lines.append("")

    # --- Start/End text ---
    addons = sp_get_text_addons(uid)
    st = (addons.get('start_text') or "").strip()
    et = (addons.get('end_text') or "").strip()

    lines.append("**Start text (prefix):**")
    lines.append(f"`{st}`" if st else "(none)")
    lines.append("")

    lines.append("**End text (suffix):**")
    lines.append(f"`{et}`" if et else "(none)")
    lines.append("")

    # --- Blacklisted words ---
    bl = sp_list_blacklist(uid)
    lines.append("**Blacklisted words:**")
    if not bl:
        lines.append("(none)")
    else:
        for r in bl:
            lines.append(f"- `{r.get('word')}`")
    lines.append("")

    # --- Subscription info ---
    sub = sp_get_app_subscription_by_telegram_user_id(uid_tg)
    if sub and sp_is_sub_active(sub):
        try:
            exp_utc = datetime.fromisoformat(str(sub["expires_at"]).replace("Z", "+00:00"))
            exp_ist = exp_utc.astimezone(IST)

            started_ist = None
            if sub.get("started_at"):
                started_utc = datetime.fromisoformat(str(sub["started_at"]).replace("Z", "+00:00"))
                started_ist = started_utc.astimezone(IST)

            now_ist = datetime.now(IST)
            left_days = max(0, (exp_ist - now_ist).days)
            plan_price = int(sub.get("plan_price_paise") or PLAN_AMOUNT_PAISE) // 100
            plan_days = int(sub.get("plan_duration_days") or PLAN_DURATION_DAYS)

            lines.append("**Subscription:**")
            lines.append(f"Plan: **₹{plan_price} / {plan_days} days**")
            lines.append(
                f"Started: `{started_ist.strftime('%Y-%m-%d %H:%M IST')}`"
                if started_ist else "Started: `-`"
            )
            lines.append(f"Expires: `{exp_ist.strftime('%Y-%m-%d %H:%M IST')}`")
            lines.append(f"Remaining: *{left_days} day(s)*")
        except Exception:
            lines.append("**Subscription:** (unable to parse subscription details)")
    else:
        lines.append("**Subscription:** None / expired")

    await e.respond("\n".join(lines), parse_mode="md")
    # new edti today 09/12/2025
@bot.on(events.NewMessage(pattern=r"^/work$"))
async def cmd_work(e):
    # Premium + login checks
    if not await guard_or_hint(e):
        return
    if not await premium_or_hint(e):
        return

    uid = e.sender_id

    # 🔴 If a forwarding loop is already running, stop it and disconnect the client
    old_state = forward_loops.pop(uid, None)
    if old_state:
        try:
            old_client = old_state["client"]
            # Remove previous event handlers
            try:
                old_client.remove_event_handlers()
            except:
                pass
            # Force disconnect
            try:
                await old_client.disconnect()
            except:
                pass
        except:
            pass
        # Remove from cache as well
        USER_CLIENT_CACHE.pop(uid, None)

    # ---- NEW: Update mapping in an additive (append) way ----
    st = select_state.get(uid, {}) or {}
    inc: List[int] = st.get("incoming_ids", []) or []
    out: List[int] = st.get("outgoing_ids", []) or []

    # Load current mapping from DB
    mapping_db = sp_load_mapping(uid)  # {sender_id: [receivers]}
    mapping: Dict[int, List[int]] = {int(k): list(v or []) for k, v in mapping_db.items()}

    existing_senders: Set[int] = set(mapping.keys())
    existing_targets: Set[int] = set()
    for lst in mapping.values():
        for t in lst or []:
            existing_targets.add(int(t))

    # --- 1) If /outgoing or /incoming + /outgoing: add new target chats ---
    if out:
        # If no mapping and no incoming selected, show error
        if not mapping and not inc:
            return await e.respond("⚠️ Please select **/incoming** first, then run /work.")

        # /outgoing only → update all existing senders
        # /incoming + /outgoing → update only the selected incoming senders
        if inc:
            target_senders: Set[int] = set(int(s) for s in inc)
        else:
            target_senders = set(existing_senders)

        for s in target_senders:
            s = int(s)
            old_list = mapping.get(s, [])
            new_list = sorted({int(x) for x in old_list} | {int(x) for x in out})
            mapping[s] = new_list

        # Clear memory state
        st["outgoing_ids"] = []

    # --- 2) If /incoming or /incoming with existing mapping: add new source chats ---
    if inc:
        # If no target chats exist (neither saved nor newly selected), user must choose outgoing
        if not out and not existing_targets and not mapping:
            return await e.respond("⚠️ Please select **/outgoing** first, then run /work.")

        if out:
            default_targets: Set[int] = set(int(x) for x in out)
        else:
            # Use already saved targets (union of all)
            default_targets = set(existing_targets)

        for s in inc:
            s = int(s)
            old_list = mapping.get(s, [])
            base = set(int(x) for x in old_list) or set(default_targets)
            mapping[s] = sorted(base)

        # Clear memory state
        st["incoming_ids"] = []

    # --- 3) If mapping is still empty, guide user ---
    if not mapping:
        return await e.respond("⚠️ Please set both **/incoming** and **/outgoing** before running /work.")

    # 🔹 Telegram client nikaal lo (names lene ke liye bhi yahi use hoga)
    try:
        uclient = await get_user_client(uid)
    except Exception as ex:
        return await e.respond(f"❌ {ex}")

    # 🔹 Ab final mapping DB me save karo, saath me sender_name + receivers_names bhi
    for s, receivers in mapping.items():
        # sender ka naam
        try:
            sender_titles = await titles_for_ids(uclient, [int(s)])
            sender_name = sender_titles[0] if sender_titles else None
        except Exception:
            sender_name = None

        # receivers ke names list me
        try:
            receivers_names = await titles_for_ids(uclient, [int(x) for x in receivers])
        except Exception:
            receivers_names = [f"id:{int(x)}" for x in receivers]

        sp_upsert_mapping(
            uid,
            int(s),
            [int(x) for x in receivers],
            sender_name=sender_name,
            receivers_names=receivers_names,
        )

    # Reload mapping (safety)
    mapping = sp_load_mapping(uid)
    if not mapping:
        return await e.respond("⚠️ Failed to save configuration. Please try again later.")

    # Load filters, blacklist, delay and cache them
    compiled_filters = compile_filters_for_user(uid)
    user_blacklist   = compile_blacklist_for_user(uid)
    user_delay       = sp_get_delay(uid) or 0

    # Load start/end text add-ons
    addons_row = sp_get_text_addons(uid)
    start_addon = (addons_row.get("start_text") or "").strip()
    end_addon   = (addons_row.get("end_text") or "").strip()

    # Save state in memory
    forward_loops[uid] = {
        "client": uclient,
        "mapping": mapping,
        "filters": compiled_filters,
        "blacklist": user_blacklist,
        "delay_seconds": user_delay,
        "start_text": start_addon,
        "end_text": end_addon,
    }

    # Attach ONE event handler for this user-client
    @uclient.on(events.NewMessage)
    async def handle_forward(evt):
        await _handle_forward_event(uid, evt)

    # Mark in DB that forwarding is active
    try:
        supabase.table("tg_user_settings").upsert({
            "user_id": uid,
            "is_forwarding": True,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }, on_conflict="user_id").execute()
    except Exception as ex:
        print(f"⚠️ Warning updating is_forwarding for user {uid}: {ex}")

    await e.respond(
        "▶️ **Forwarding started!**\n"
        "Stop anytime using **/stop**."
    )


async def _handle_forward_event(uid: int, evt):
    """
    Real forward logic – saare media types yahin handle honge.
    Har user ke liye state `forward_loops[uid]` se lete hain.
    """
    try:
        state = forward_loops.get(uid)
        if not state:
            return

        uclient: TelegramClient = state["client"]
        mapping: Dict[int, List[int]] = state["mapping"]
        compiled_now = state.get("filters", [])
        curr_blacklist = state.get("blacklist", [])
        user_delay = int(state.get("delay_seconds", 0) or 0)
        start_addon = (state.get("start_text") or "").strip()
        end_addon   = (state.get("end_text") or "").strip()

        # admin khud group/chat me normal message bhej raha hai → skip
        #if getattr(evt, "out", False) and not evt.is_channel:
        #    return

        src_id = evt.chat_id if evt.chat_id is not None else evt.sender_id
        targets = mapping.get(int(src_id))
        if not targets:
            return

        msg = evt.message
        original_text = msg.message or ""
        text = original_text

        # ---------- text processing (caption / message) ----------
        if text:
            # pehle blacklist apply
            text = apply_blacklist(text, curr_blacklist)
            # phir replace filters
            text = apply_text_filters(text, compiled_now)

            # start text (agar set hai)
            if start_addon:
                sa = apply_blacklist(start_addon, curr_blacklist)
                sa = apply_text_filters(sa, compiled_now)
                text = f"{sa}\n\n{text}".strip()

            # end text (agar set hai)
            if end_addon:
                ea = apply_blacklist(end_addon, curr_blacklist)
                ea = apply_text_filters(ea, compiled_now)
                text = f"{text}\n\n{ea}".strip()

        has_media = bool(msg.media)

        # global delay before sending
        if user_delay > 0:
            await asyncio.sleep(user_delay)

        # har target ko forward karo
        for t in targets:
            try:
                # --- SPECIAL CASE: POLL (send as real poll, no "Forwarded from") ---
                if has_media and isinstance(msg.media, types.MessageMediaPoll):
                    poll = msg.media.poll

                    # options ko naya ID deke recreate karte hain
                    new_answers = []
                    for idx, ans in enumerate(poll.answers or []):
                        new_answers.append(
                            types.PollAnswer(
                                text=ans.text,
                                option=str(idx).encode("utf-8"),
                            )
                        )

                    new_poll = types.Poll(
                        id=0,
                        question=poll.question,
                        answers=new_answers,
                        public_voters=poll.public_voters,
                        multiple_choice=poll.multiple_choice,
                        quiz=poll.quiz,
                        close_period=poll.close_period,
                        close_date=poll.close_date,
                    )

                    input_media = types.InputMediaPoll(poll=new_poll)

                    # 1) pehle graphical poll bhejte hain
                    await uclient.send_file(int(t), file=input_media)

                    # 2) agar processed text alag hai to second message me bhej do
                    if text and text != original_text:
                        await uclient.send_message(int(t), text)

                # --- GEO / VENUE: yahan geo= use nahi kar sakte, isliye direct forward ---
        # --- GEO / VENUE: "Forwarded" header avoid karne ke liye naya location create karenge ---
                elif has_media and isinstance(
                    msg.media,
                    (types.MessageMediaGeo, types.MessageMediaGeoLive, types.MessageMediaVenue),
                ):
                    geo = getattr(msg.media, "geo", None)

                    if geo and hasattr(geo, "lat") and hasattr(geo, "long"):
                        # ✅ Naya geo point banao
                        input_media = types.InputMediaGeoPoint(
                            geo_point=types.InputGeoPoint(
                                lat=geo.lat,
                                long=geo.long,
                            )
                        )

                        # Caption agar tumne text process kiya hai to use karo, warna default
                        caption = text or "📍 Location"

                        # ✅ Yahan forward_messages nahi, balki send_file se naya location bhej raha hai
                        await uclient.send_file(
                            int(t),
                            file=input_media,
                            caption=caption,
                        )

                    else:
                        # Agar geo object nahi mila to fallback: sirf text bhej do
                        if text:
                            await uclient.send_message(int(t), text)


                # --- NORMAL MEDIA: photo, video, audio, GIF, sticker, doc, etc. ---
                elif has_media:
                    if msg.photo:
                        await uclient.send_file(int(t), msg.photo, caption=text or "")

                    elif msg.video:
                        await uclient.send_file(int(t), msg.video, caption=text or "")

                    elif msg.voice:
                        await uclient.send_file(int(t), msg.voice, caption=text or "")

                    elif msg.audio:
                        await uclient.send_file(int(t), msg.audio, caption=text or "")

                    elif msg.sticker:
                        await uclient.send_file(int(t), msg.sticker, caption=text or "")

                    elif msg.animation:
                        await uclient.send_file(int(t), msg.animation, caption=text or "")

                    elif msg.video_note:
                        await uclient.send_file(int(t), msg.video_note, caption=text or "")

                    elif msg.document:
                        await uclient.send_file(int(t), msg.document, caption=text or "")

                    else:
                        await uclient.send_message(
                            int(t),
                            message=text or None,
                            file=msg.media,
                        )

                # --- TEXT ONLY ---
                else:
                    if text:
                        await uclient.send_message(int(t), text)

            except errors.FloodWaitError as fw:
                await asyncio.sleep(fw.seconds + 1)
            except Exception as ex:
                print("send err:", ex)

            # per-target chhota sa throttle
            await asyncio.sleep(FORWARD_THROTTLE)

    except Exception as ex:
        print("forward handler err:", ex)
    
@bot.on(events.NewMessage(pattern=r"^/stop$"))
async def cmd_stop(e):
    if not await guard_or_hint(e):
        return

    uid = e.sender_id
    state = forward_loops.pop(uid, None)

    if not state:
        return await e.respond("⚠️ Forwarding was not running.")

    client = state.get("client")

    # 🔴 Remove all registered event handlers for this user-client
    try:
        client.remove_event_handlers()
    except:
        pass

    # 🔴 Force-terminate Telethon connection (kills session locks)
    try:
        client._sender = None
    except:
        pass

    try:
        await client.disconnect()
    except:
        pass

    # 🔥 IMPORTANT: Remove cached client also
    USER_CLIENT_CACHE.pop(uid, None)

    # Mark forwarding as stopped
    try:
        supabase.table("tg_user_settings").update({
            "is_forwarding": False,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }).eq("user_id", uid).execute()
    except Exception as ex:
        print(f"⚠️ Warning updating is_forwarding for user {uid}: {ex}")

    await e.respond("⏹️ **Forwarding stopped**\nYou can start again with **/work**.")

# ---------------- FILTER COMMANDS (premium) ----------------
@bot.on(events.NewMessage(pattern=r"^/addfilter$"))
async def addfilter_help_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    txt = (
        "🧩 **Add a replace filter**\n"
        "Use in this format:\n"
        "`/addfilter old==new`\n"
        "`/addfilter @old==@new`\n\n"
        "**Examples:**\n"
        "`/addfilter rohit==sobhit`\n"
        "`/addfilter @anychannelname==@otherchannelname`\n\n"
        "Left side (old text) and right side (new text)."
    )
    await e.respond(txt, parse_mode="md")

@bot.on(events.NewMessage(pattern=r"^/addfilter\s+(.+)$"))
async def addfilter_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    uid = e.sender_id
    raw = (e.pattern_match.group(1) or "").strip()
    parts = re.split(r"\s*==\s*", raw, maxsplit=1)
    if len(parts) != 2:
        return await e.respond(
            "⚠️ Invalid format.\nUse: `/addfilter old==new` or `/addfilter @old==@new`",
            parse_mode="md"
        )
    left, right = parts[0].strip(), parts[1].strip()
    if not left or not right:
        return await e.respond("⚠️ Both values are required: `old==new`.", parse_mode="md")
    if left.lower() == right.lower():
        return await e.respond("⚠️ Left aur right same nahi ho sakte.")
    ok, msg = sp_add_filter(uid, left, right)
    await e.respond(msg)
    if uid in forward_loops:
        forward_loops[uid]["filters"] = compile_filters_for_user(uid)

@bot.on(events.NewMessage(pattern=r"^/showfilter$"))
async def showfilter_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    uid = e.sender_id
    rows = sp_list_filters(uid)
    if not rows:
        return await e.respond("ℹ️ No filters set. Add one: `/addfilter @old==@new`", parse_mode="md")
    lines = ["**Your filters:**", ""]
    for r in rows:
        lines.append(f"- `{r['from_name']}` → `{r['to_name']}`")
    lines.append("")
    lines.append("👉 **Tap to remove:** /removefilter ")
    await e.respond("\n".join(lines), parse_mode="md")

@bot.on(events.NewMessage(pattern=r"^/removefilter$"))
async def removefilter_ui_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    uid = e.sender_id
    rows = sp_list_filters(uid)
    if not rows:
        return await e.respond("❌ No filters set yet!\n\nAdd filters with `/addfilter old==new`.", parse_mode="md")
    pairs = [(i, f"{(r.get('from_name') or '')} → {(r.get('to_name') or '')}") for i, r in enumerate(rows)]
    select_state[uid] = {"mode": "remove_filter", "pairs": pairs, "selected": set(), "filter_rows": rows}
    await e.respond(
        "🗑️ Remove Filters — select filters to remove (multi-select) and press ✅ Done.\n\n "
        + numbered_list_from_pairs(pairs),
        buttons=multi_kb(len(pairs), set())
    )

@bot.on(events.NewMessage(pattern=r"^/removefilter\s+(\S+)$"))
async def removefilter_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    uid = e.sender_id
    left = e.pattern_match.group(1)
    ok, msg = sp_delete_filter(uid, left)
    await e.respond(msg)
    if uid in forward_loops:
        forward_loops[uid]["filters"] = compile_filters_for_user(uid)

@bot.on(events.NewMessage(pattern=r"^/deleteallfilters$"))
async def delete_all_filters_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    uid = e.sender_id
    n = sp_delete_all_filters(uid)
    if uid in forward_loops:
        forward_loops[uid]["filters"] = compile_filters_for_user(uid)
    await e.respond(f"🗑️ Deleted **{n}** filter(s). \nStart forwarding with **/work**.")

# ---------------- BLACKLIST COMMANDS (premium) ----------------
@bot.on(events.NewMessage(pattern=r"^/blacklist_word$"))
async def blacklist_help_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    txt = (
        "🚫 **Blacklist a word**\n"
        "Forwarded text/captions me se given word remove ho jayega.\n\n"
        "**Use:** `/blacklist_word WORD`\n"
        "Examples:\n"
        "`/blacklist_word sumit`\n"
        "`/blacklist_word @myhandle`\n\n"
        "Delete karne ke liye: `/remove_blacklist`"
    )
    await e.respond(txt, parse_mode="md")

@bot.on(events.NewMessage(pattern=r"^/blacklist_word\s+(.+)$"))
async def blacklist_add_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    uid = e.sender_id
    word = (e.pattern_match.group(1) or "").strip()
    ok, msg = sp_add_blacklist_word(uid, word)
    await e.respond(msg, parse_mode="md")
    # Live update if forwarding is active
    if uid in forward_loops:
        forward_loops[uid]["blacklist"] = compile_blacklist_for_user(uid)

@bot.on(events.NewMessage(pattern=r"^/remove_blacklist$"))
async def remove_blacklist_ui_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    uid = e.sender_id
    rows = sp_list_blacklist(uid)
    if not rows:
        return await e.respond("ℹ️ No blacklisted words set. Add with `/blacklist_word word`.", parse_mode="md")
    # Pairs: index with label
    pairs = [(i, (rows[i].get("word") or "")) for i in range(len(rows))]
    select_state[uid] = {"mode": "remove_blacklist", "pairs": pairs, "selected": set(), "blacklist_rows": rows}
    await e.respond(
        "🗑️ **Remove Blacklisted Words** — select words to remove (multi-select) and press ✅ Done.\n\n" +
        numbered_list_from_pairs(pairs),
        buttons=multi_kb(len(pairs), set())
    )


# ---------------- START_TEXT / END_TEXT / REMOVE_TEXT COMMANDS ----------------
@bot.on(events.NewMessage(pattern=r"^/start_text$"))
async def start_text_help_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    txt = (
        "📝 **Add Starting Text**\n\n"
        "Use this format to set the text that appears at the *start* of every forwarded message:\n"
        "\n"
        "/start_text **YOUR START TEXT HERE**\n\n"
        "Example output:\n"
        "\n"
        "**YOUR START TEXT HERE**\n"
        "_(then your forwarded message content follows)_"
    )
    await e.respond(txt, parse_mode="md")


@bot.on(events.NewMessage(pattern=r"^/start_text\s+(.+)$"))
async def start_text_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    uid = e.sender_id
    text = e.pattern_match.group(1).strip()
    sp_set_start_text(uid, text)
    await e.respond(f"✅ Starting text set:\n\n`{text}` \nStart forwarding with **/work**.", parse_mode="md")


@bot.on(events.NewMessage(pattern=r"^/end_text$"))
async def end_text_help_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    txt = (
        "📝 **Add Ending Text**\n\n"
        "Use this format to set the text that appears at the *end* of every forwarded message:\n"
        "\n"
        "/end_text **THIS MESSAGE WILL BE APPENDED TO THE END**\n\n"
        "Example output:\n"
        "\n"
        "*(forwarded message content)*\n"
        "**THIS MESSAGE WILL BE APPENDED TO THE END**"
    )
    await e.respond(txt, parse_mode="md")


@bot.on(events.NewMessage(pattern=r"^/end_text\s+(.+)$"))
async def end_text_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    uid = e.sender_id
    text = e.pattern_match.group(1).strip()
    sp_set_end_text(uid, text)
    await e.respond(f"✅ Ending text set:\n\n`{text}` \nStart forwarding with **/work**.", parse_mode="md")


@bot.on(events.NewMessage(pattern=r"^/remove_text$"))
async def remove_text_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    uid = e.sender_id
    sp_remove_texts(uid)
    await e.respond("🧹 Removed all saved start and end texts.\nStart forwarding with **/work**.")


@bot.on(events.NewMessage(pattern=r"^/delay$"))
async def delay_help_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    txt = (
        "⏱️ **Set Forwarding Delay**\n\n"
        "Use this format to set delay in seconds between each forward:\n"
        "\n"
        "`/delay 5`\n\n"
        "**Examples:**\n"
        "`/delay 0` — No delay (instant forwarding)\n"
        "`/delay 10` — 10 seconds delay\n"
        "`/delay 60` — 1 minute delay\n\n"
        "Allowed range: **0-999 seconds**\n\n"
        "To remove delay: `/removedelay`"
    )
    await e.respond(txt, parse_mode="md")


@bot.on(events.NewMessage(pattern=r"^/delay\s+(\d+)$"))
async def delay_cmd(e):
    if not await guard_or_hint(e): return
    if not await premium_or_hint(e): return
    uid = e.sender_id
    secs = int(e.pattern_match.group(1))
    if not (0 <= secs <= 999):
        return await e.respond("⚠️ Use: `/delay 0..999` seconds.", parse_mode="md")
    sp_set_delay(uid, secs)
    if uid in forward_loops:
        forward_loops[uid]["delay_seconds"] = secs
    await e.respond(f"⏱️ Delay set to **{secs}s**.\nStart forwarding with **/work**.")

@bot.on(events.NewMessage(pattern=r"^/removedelay$"))
async def remove_delay_cmd(e):
    """Removes any set delay (resets to 0 seconds)"""
    if not await guard_or_hint(e): 
        return
    if not await premium_or_hint(e): 
        return

    uid = e.sender_id

    try:
        # Reset delay_seconds back to 0
        payload = {
            "user_id": uid,
            "delay_seconds": 0,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        supabase.table("tg_user_settings").upsert(payload, on_conflict="user_id").execute()

        # If forwarding loop is active, update it immediately
        if uid in forward_loops:
            forward_loops[uid]["delay_seconds"] = 0

        await e.respond("⏱️ Delay removed successfully. Messages will now forward instantly.\nStart forwarding with **/work**.")
        
    except Exception as ex:
        await e.respond(f"❌ Failed to remove delay:\n`{ex}`", parse_mode="md")


# --- /tg_plans(public) — shows feature-only overview with plan buttons ---
@bot.on(events.NewMessage(pattern=r"^/plans$"))
async def cmd_plans(e):
    uid = int(e.sender_id)

    # --- Subscription status at top ---
    app_sub = sp_get_app_subscription_by_telegram_user_id(uid)
    status_block = format_app_plan_block(app_sub)

    features_block = (
    "✨ **Auto Forward — Features**\n\n"
    "• Auto-forward (any source → any target)\n"
    "• Add prefix & suffix text\n"
    "• Remove blacklist words\n"
    "• Set message delay\n"
    "• Replace words / usernames\n"
    "• Supports all media types\n"
    "\n"
    "▶️ Start / stop anytime\n"
    "\n"
    "\n"
)

    cta = "📋 View all plans and pricing on the **GetAIPilot** plan page:"

    txt = f"{status_block}\n{features_block}{cta}"

    await e.respond(
        txt,
        parse_mode="md",
        buttons=[
            [Button.url("📋 Plans", "https://getaipilot.in/pricing")],
        ],
    )

# --- /upgrade (public) — same overview; keeps user habit of typing /upgrade
@bot.on(events.NewMessage(pattern=r"^/upgrade$"))
async def cmd_upgrade(e):
    uid = int(e.sender_id)

    # --- Subscription status at top ---
    app_sub = sp_get_app_subscription_by_telegram_user_id(uid)
    status_block = format_app_plan_block(app_sub)

    # --- Features block (plain text, no plan catalog) ---
    features_block = (
    "✨ **Auto Forward — Features**\n\n"
    "• Auto-forward (any source → any target)\n"
    "• Add prefix & suffix text\n"
    "• Remove blacklist words\n"
    "• Set message delay\n"
    "• Replace words / usernames\n"
    "• Supports all media types\n"
    "\n"
    "▶️ Start / stop anytime\n"
    "\n\n"
)

    # --- CTA at bottom ---
    cta = (
        "👇 Click the button to purchase your plan on **GetAIPilot** dashboard.\n"
        "You will be redirected to the plan purchase page."
    )

    txt = f"{status_block}\n{features_block}{cta}"

    await e.respond(
        txt,
        parse_mode="md",
        buttons=[
            [Button.url("💳 Purchase Plan", "https://getaipilot.in/pricing")],
        ],
    )



@bot.on(events.NewMessage(pattern=r"^/upgrade_status$"))
async def cmd_upgrade_status(e):
    uid = int(e.sender_id)
    sub = sp_get_app_subscription_by_telegram_user_id(uid)
    await e.respond(format_app_plan_block(sub), parse_mode="md")


@bot.on(events.CallbackQuery(pattern=b"upgrade_check"))
async def cb_upgrade_check(event):
    class FakeE:
        sender_id = event.sender_id
        async def respond(self, *a, **kw): return await event.reply(*a, **kw)
    await cmd_upgrade_status(FakeE())

# ---------------- LOGOUT ----------------
@bot.on(events.NewMessage(pattern=r"^/logout$"))
async def logout_cmd(e):
    # /logout is in ALWAYS_ALLOWED (free user ok), but needs to be logged in to actually delete session
    if not await guard_or_hint(e): return
    await e.respond(
        "⚠️ Are you sure you want to logout? After logout all your data will be deleted.\n\n"
        "Your session file and mappings will be removed permanently.\n\n"
        "Confirm to proceed:",
        buttons=[[Button.inline("✅ Yes, logout", data=b"logout_confirm"),
                  Button.inline("✖ Cancel", data=b"logout_cancel")]]
    )
@bot.on(events.CallbackQuery(pattern=b"logout_confirm"))
async def logout_confirm_cb(event):
    uid = event.sender_id
    data = sp_get_session(uid)
    if not data:
        return await event.edit("ℹ️ No session found.")
    if uid in forward_loops:
        try:
            await forward_loops[uid]["client"].disconnect()
        except:
            pass
        forward_loops.pop(uid, None)

    # 🔥 YAHAN NEW LINE ADD KARO
    USER_CLIENT_CACHE.pop(uid, None)

    path = os.path.join(SESSION_DIR, data["session_file"])
    if os.path.exists(path):
        try:
            os.remove(path)
        except Exception as ex:
            print("remove session file err:", ex)
    sp_delete_session(uid)
    await event.edit("👋 Logged out successfully. All your data removed. You can /login again anytime.")
 

@bot.on(events.CallbackQuery(pattern=b"logout_cancel"))
async def logout_cancel_cb(event):
    await event.edit("✖ Logout cancelled. You are still logged in.")

@bot.on(events.NewMessage(pattern=r"^/stoplogin$"))
async def stoplogin_cmd(e):
    """Cancel any ongoing /login flow for the user."""
    uid = e.sender_id
    if uid in login_state:
        login_state.pop(uid, None)
        await e.respond("✖ Login process cancelled. You can start again with /login if you want.")
        return
    await e.respond("ℹ️ No login in progress.")


@bot.on(events.CallbackQuery(pattern=b"resend_otp"))
async def cb_resend_otp(event):
    uid = event.sender_id
    st = login_state.get(uid)
    if not st or not st.get("phone"):
        return await event.answer("No login in progress. Use /login.", alert=True)

    phone = st["phone"]
    local = session_path(uid, phone)
    client = TelegramClient(local, API_ID, API_HASH)
    try:
        await safe_connect(client)
        res = await client.send_code_request(phone)
        st["phone_code_hash"] = getattr(res, "phone_code_hash", None)
        await event.edit(
            "📩 OTP resent. Send like `123456` or `LOGIN123456`.\n\n"
            "If still not received, try again after a minute.",
            buttons=[[Button.inline("🔁 Resend OTP", data=b"resend_otp")]]
        )
    except Exception as ex:
        print("resend_code_request error:", ex)
        await event.edit(f"❌ Resend failed: {ex}\nStart /login again.")
    finally:
        try: await client.disconnect()
        except: pass

async def safe_connect(client, retries=3, delay=2):
    try:
        if hasattr(client, "is_connected") and callable(client.is_connected) and client.is_connected():
            return
    except Exception:
        pass
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            await client.connect()
            try:
                if hasattr(client, "is_connected") and callable(client.is_connected) and client.is_connected():
                    return
            except Exception:
                return
            return
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                await asyncio.sleep(delay * attempt)
    raise last_exc or RuntimeError("safe_connect: failed to connect")

async def resume_forwarding_on_start():
    """
    Bot restart hone par un sab users ka forwarding resume karega 
    jinke user_settings me is_forwarding = true hai.
    """
    print("🔍 Checking which users had forwarding enabled before restart...")

    uids = sp_get_forwarding_users()
    if not uids:
        print("ℹ️ No users with forwarding ON previously.")
        return

    print(f"🔁 Resuming forwarding for {len(uids)} user(s): {uids}")

    for uid in uids:
        try:
            # Check if user logged-in properly
            try:
                _ = await get_user_client(uid)
            except Exception as ex:
                print(f"⚠️ Cannot resume forwarding for {uid}: login error -> {ex}")
                continue

            # User ko info de do ki forwarding dobara start ho gayi
            try:
                await bot.send_message(
                    uid,
                    "🔄 Bot restarted.\n"
                    "▶️ Forwarding started again automatically for your chats."
                )
            except Exception as ex:
                print(f"⚠️ Could not send message to {uid}: {ex}")

            # `/work` ko background se run karo, lekin jo bhi /work respond karega
            # woh REAL user chat me hi jayega (terminal pe nahi)
            class FakeEvent:
                sender_id = uid
                raw_text = "/work"

                async def respond(self, *args, **kwargs):
                    # /work normally e.respond(...) use karta hai,
                    # hum usko bridge kar rahe hain bot.send_message se.
                    await bot.send_message(self.sender_id, *args, **kwargs)

            await cmd_work(FakeEvent())


        except Exception as ex:
            print(f"❌ Error resuming forwarding for {uid}: {ex}")


# ---------------- RUN ----------------
if __name__ == "__main__":
    print("🤖 Auto-Forward Login Bot ready!")
    try:
        asyncio.get_event_loop().run_until_complete(setup_bot_profile())
    except Exception as ex:
        print(f"⚠️ Non-fatal error during bot profile setup: {ex}")

    try:
        asyncio.get_event_loop().run_until_complete(resume_forwarding_on_start())
    except Exception as ex:
        print(f"⚠️ Non-fatal error during resume forwarding on start: {ex}")

    try:
        bot.run_until_disconnected()
    except (KeyboardInterrupt, SystemExit):
        print("👋 Bot stopped gracefully.")
    except Exception as ex:
        print(f"❌ Bot disconnected with error: {ex}")
