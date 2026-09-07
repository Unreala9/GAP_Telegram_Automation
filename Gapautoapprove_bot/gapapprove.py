import asyncio
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

# Indian time zone (UTC+05:30)
IST = timezone(timedelta(hours=5, minutes=30))

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    ChatJoinRequest,
    ChatAdministratorRights,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from supabase import create_client, Client

# ---------------- ENV LOAD ----------------

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME")  # without @ e.g. Getai_approvedbot

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

EXPIRY_REMINDER_DAYS = 3          # send reminder this many days before expiry
REMINDER_CHECK_INTERVAL_SEC = 12 * 60 * 60  # check every 12 hours

# in-memory structures to prevent spamming reminders
_reminded_users: set = set()
_expired_notified_users: dict = {}

if not BOT_TOKEN:
    raise RuntimeError("❌ BOT_TOKEN missing in .env file")

assert SUPABASE_URL and SUPABASE_KEY, "Set SUPABASE_URL and SUPABASE_KEY / SUPABASE_SERVICE_ROLE_KEY in .env"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# ---------------- SUBSCRIPTION HELPERS ----------------


def sp_get_subscription(user_id: int) -> Optional[dict]:
    try:
        res = (
            supabase.table("app_user_subscriptions")
            .select("*")
            .eq("telegram_user_id", user_id)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None
    except Exception as ex:
        print("sp_get_subscription error:", ex)
        return None


def is_plan_active(sub: dict) -> bool:
    """Check if plan is active using IST time."""
    try:
        exp = sub.get("expires_at")
        if not exp:
            return False

        exp_dt = datetime.fromisoformat(str(exp).replace("Z", ""))
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=timezone.utc)

        exp_dt_ist = exp_dt.astimezone(IST)
        now_ist = datetime.now(IST)
        return now_ist < exp_dt_ist
    except Exception as e:
        print("is_plan_active error:", e)
        return False


def has_active_plan(user_id: int) -> bool:
    sub = sp_get_subscription(user_id)
    return bool(sub and is_plan_active(sub))


def format_plan_status(user_id: int) -> str:
    sub = sp_get_subscription(user_id)
    if not sub:
        return (
            "🔴 <b>No active plan found.</b>\n"
            "Please purchase a plan from the GetAIPilot dashboard."
        )

    import html
    if not is_plan_active(sub):
        label = sub.get("plan_label") or "Unknown Plan"
        safe_label = html.escape(label)
        return (
            f"🟠 <b>Plan Expired: {safe_label}</b>\n\n"
            "Please renew your plan from the GetAIPilot dashboard.\n"
        )

    exp_raw = sub.get("expires_at")
    exp_dt = datetime.fromisoformat(str(exp_raw).replace("Z", ""))
    if exp_dt.tzinfo is None:
        exp_dt = exp_dt.replace(tzinfo=timezone.utc)
    exp_dt_ist = exp_dt.astimezone(IST)
    expires = exp_dt_ist.strftime("%d %b %Y, %I:%M %p IST")

    now_ist = datetime.now(IST)
    remaining = (exp_dt_ist - now_ist).days

    label = sub.get("plan_label") or "Unknown Plan"
    safe_label = html.escape(label)

    return (
        f"🟢 <b>Active Plan: {safe_label}</b>\n"
        f"📅 <b>Expires:</b> <code>{expires}</code>\n"
        f"⏳ <b>Remaining:</b> <code>{remaining} day(s)</code>\n\n"
        "Auto-approve is active. Enjoy! 🎉"
    )


# ---------------- BOT COMMAND HANDLERS ----------------


async def cmd_start(message: Message):
    if not BOT_USERNAME:
        await message.answer("❌ BOT_USERNAME missing in .env file")
        return

    user_id = message.from_user.id

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📢 Add to Channel",
                    url=f"https://t.me/{BOT_USERNAME}?startchannel=auto_approve",
                )
            ],
            [
                InlineKeyboardButton(
                    text="👥 Add to Group",
                    url=f"https://t.me/{BOT_USERNAME}?startgroup=auto_approve",
                )
            ],
        ]
    )

    text = (
        "Add this bot to your channel or group to auto-approve join requests 😊\n\n"
        "➕ Just add me as admin with <i>Add Members</i> rights in your "
        "channel or group. I will auto-approve all join requests ✅"
    )

    try:
        photo = FSInputFile("image.png")
        await message.answer_photo(
            photo=photo,
            caption=text,
            reply_markup=kb,
            parse_mode="HTML",
        )
    except Exception as e:
        print(f"Could not send image.png: {e}")
        # Fallback if image is missing or error occurs
        await message.answer(
            text,
            reply_markup=kb,
            parse_mode="HTML",
        )


async def cmd_status(message: Message):
    """Show current subscription status."""
    await message.answer(format_plan_status(message.from_user.id), parse_mode="HTML")


async def handle_join_request(event: ChatJoinRequest, bot: Bot):
    user_id = event.from_user.id
    chat_id = event.chat.id

    # Find the owner/creator of the channel/group
    owner_id = None
    try:
        admins = await bot.get_chat_administrators(chat_id)
        for admin in admins:
            if admin.status == "creator":
                owner_id = admin.user.id
                break
    except Exception as e:
        print(f"Error getting admins for chat {chat_id}: {e}")
        # Continue to approve request even if admin lookup fails/times out

    # If owner doesn't have an active plan, send notification but still auto-approve
    if owner_id and not has_active_plan(owner_id):
        now = datetime.now(timezone.utc)
        last_notified = _expired_notified_users.get(owner_id)
        if last_notified is None or (now - last_notified).total_seconds() >= 86400:
            _expired_notified_users[owner_id] = now
            try:
                kb_notice = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="Purchase", url="https://getaipilot.in/pricing")]
                    ]
                )
                await bot.send_message(
                    owner_id,
                    "⚠️ <b>Notice:</b> Your GetAIPilot subscription has ended.\n\n"
                    "Don't worry, <b>Auto-Approve feature is completely FREE</b> and will not stop working.\n"
                    "However, if you want to use other premium features, please renew your subscription.",
                    reply_markup=kb_notice,
                    parse_mode="HTML",
                    disable_web_page_preview=True
                )
            except Exception as e:
                print("Could not send expiry notice to owner:", e)

    # ✅ unconditionally approve request
    try:
        await bot.approve_chat_join_request(
            chat_id=chat_id,
            user_id=user_id,
        )

        try:
            import html
            safe_title = html.escape(event.chat.title) if event.chat.title else "the channel"
            await bot.send_message(
                user_id,
                f"✅ Your request to join <b>{safe_title}</b> has been approved automatically.",
                parse_mode="HTML",
            )
        except Exception:
            pass

    except Exception as e:
        print("Error approving join request:", e)


# ---------------- EXPIRY REMINDER LOOP ----------------


def sp_get_expiring_soon() -> list:
    """Return subscriptions expiring within EXPIRY_REMINDER_DAYS days."""
    try:
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(days=EXPIRY_REMINDER_DAYS)
        res = (
            supabase.table("app_user_subscriptions")
            .select("telegram_user_id, plan_label, expires_at")
            .gt("expires_at", now.isoformat())       # not yet expired
            .lte("expires_at", cutoff.isoformat())   # but will expire within 3 days
            .execute()
        )
        return res.data or []
    except Exception as ex:
        print("sp_get_expiring_soon error:", ex)
        return []


async def send_expiry_reminders(bot: Bot):
    """Check expiring plans and send reminders to users not yet notified."""
    rows = sp_get_expiring_soon()
    for row in rows:
        tg_id = row.get("telegram_user_id")
        if not tg_id or tg_id in _reminded_users:
            continue

        exp_raw = row.get("expires_at")
        exp_dt = datetime.fromisoformat(str(exp_raw).replace("Z", ""))
        if exp_dt.tzinfo is None:
            exp_dt = exp_dt.replace(tzinfo=timezone.utc)
        exp_dt_ist = exp_dt.astimezone(IST)
        exp_str = exp_dt_ist.strftime("%d %b %Y, %I:%M %p IST")

        remaining = (exp_dt_ist - datetime.now(IST)).days
        label = row.get("plan_label") or "Your plan"

        import html
        safe_label = html.escape(label)
        text = (
            f"⚠️ <b>Plan Expiry Reminder</b>\n\n"
            f"📦 Plan: <b>{safe_label}</b>\n"
            f"📅 Expires on: <code>{exp_str}</code>\n"
            f"⏳ Only <b>{remaining} day(s)</b> remaining!\n\n"
            "Renew your plan to keep auto-approve running smoothly."
        )

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Renew Plan", url="https://getaipilot.in/pricing")]
            ]
        )

        try:
            await bot.send_message(tg_id, text, reply_markup=kb, parse_mode="HTML")
            _reminded_users.add(tg_id)
            print(f"✅ Reminder sent to telegram_user_id={tg_id}")
        except Exception as ex:
            print(f"send_expiry_reminders: could not message {tg_id}: {ex}")


async def reminder_loop(bot: Bot):
    """Background task: check every 12 hours and send expiry reminders."""
    print("⏰ Expiry reminder loop started (checks every 12 hours)...")
    while True:
        try:
            await send_expiry_reminders(bot)
        except Exception as ex:
            print("reminder_loop error:", ex)
        await asyncio.sleep(REMINDER_CHECK_INTERVAL_SEC)


# ---------------- MAIN ----------------


async def main():
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()

    try:
        # Attempt to set default admin rights (non-fatal if restricted by Telegram)
        try:
            rights = ChatAdministratorRights(
                is_anonymous=False,
                can_manage_chat=False,
                can_delete_messages=False,
                can_manage_video_chats=False,
                can_restrict_members=False,
                can_promote_members=False,
                can_change_info=False,
                can_invite_users=True,   # ✅ "Add members" ON
                can_post_stories=False,
                can_edit_stories=False,
                can_delete_stories=False,
            )
            await bot.set_my_default_administrator_rights(rights=rights, for_channels=False)
            await bot.set_my_default_administrator_rights(rights=rights, for_channels=True)
        except Exception as ex:
            print(f"⚠️ Could not set default admin rights ({ex}), continuing startup...")

        # Commands
        dp.message.register(cmd_start, CommandStart())
        dp.message.register(cmd_status, Command("status"))

        # Join request
        dp.chat_join_request.register(handle_join_request)

        # Clear any existing webhook & drop pending updates to prevent conflicts
        await bot.delete_webhook(drop_pending_updates=True)

        print("🤖 Auto Approve Bot running (dashboard-managed subscriptions)...")

        # Start expiry reminder background loop
        asyncio.create_task(reminder_loop(bot))

        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

