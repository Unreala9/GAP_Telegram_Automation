# GAP Telegram Automation Suite

Welcome to the **GAP Telegram Automation Suite**—a comprehensive, multi-tenant ecosystem designed to automate subscriptions, message forwarding, marketing broadcasts, drip campaigns, and AI customer support across Telegram groups and channels, fully integrated with a Supabase database backend and a web dashboard.

init

---

## 📂 Table of Contents
1. [Auto-Approve Bot (`Gapautoapprove_bot`)](#1-auto-approve-bot-gapautoapprove_bot)
2. [Auto-Forwarder Bot (`GetAiPilot_autoforwarding_bot`)](#2-auto-forwarder-bot-getaipilot_autoforwarding_bot)
3. [Telegram Backend API (`GetaipilotBackEnd1`)](#3-telegram-backend-api-getaipilotbackend1)
4. [Subscription Bot (`SubscriptionBOT2.0`)](#4-subscription-bot-subscriptionbot20)
5. [Broadcast Bot (`broadcast_bot`)](#5-broadcast-bot-broadcast_bot)
6. [Lead Tracker & Drip Campaign Bot (`join_bot`)](#6-lead-tracker--drip-campaign-bot-join_bot)
7. [AI RAG Chatbot (`llm_bot`)](#7-ai-rag-chatbot-llm_bot)
8. [Deployment & Run Guide](#deployment--run-guide)

---

## 🤖 Component Breakdown & Feature List

### 1. Auto-Approve Bot (`Gapautoapprove_bot`)
An AIgram-powered bot that automatically and instantly approves pending join requests in Telegram groups or channels.

*   **Instant Join Approvals:** Automatically accepts users requesting to join channels or groups as long as the bot has administrative privileges.
*   **Minimalist Admin Scope:** Sets default admin rights to only require the "Invite Users / Add Members" permission, maximizing security.
*   **Supabase Billing & Plan Integration:** Fetches user subscriptions (`app_user_subscriptions`) using the chat owner's Telegram ID to verify active subscription status.
*   **Graceful Subscription Expiry:** The bot never stops approving requests (Auto-Approve is treated as a free tier), but it detects expired owner subscriptions and sends friendly reminder notifications to renew their plan.
*   **Expiry Reminder Loop:** Runs a background job checking every 12 hours for plans expiring within 3 days, proactively reminding owners via a clickable inline button to renew.

---

### 2. Auto-Forwarder Bot (`GetAiPilot_autoforwarding_bot`)
A high-performance Telethon-based userbot allowing users to automatically mirror messages from source chats to target destination chats.

*   **Interactive OTP Login Flow:** Allows users to log their own Telegram account into the bot via an interactive OTP, 2FA password, and verification flow.
*   **Source & Destination Management:** Features interactive Telegram inline grids to easily select incoming source chats and outgoing destination channels.
*   **Custom Word Filters & Replacements:** Automatically rewrites words or phrases in messages using regex mappings configured by users (`/addfilter`, `/showfilter`, `/removefilter`).
*   **Text Blacklisting:** Drops/ignores messages containing specific blacklisted words or phrases.
*   **Headers & Footers Add-ons:** Automatically appends customizable headers (`/start_text`) or footers (`/end_text`) to forwarded messages.
*   **Custom Delay & Throttling:** Introduces artificial delay configurations between messages to mimic human behavior, alongside an internal throttle (`0.2s`) to prevent rate-limiting/spam bans.
*   **Subscription & Demo Enforcement:** Checks user plan levels and restricts forwarding capabilities to active paid subscribers or users with active demo access (`/start_demo`).

---

### 3. Telegram Backend API (`GetaipilotBackEnd1`)
A FastAPI server acting as a bridge between the front-end web dashboard and the background Telethon userbot instances.

*   **FastAPI & Supabase JWT Auth:** Exposes secure REST endpoints protected by JWT validation matching the Supabase user sessions.
*   **Remote Login API:** Implements headless interactive login flows (OTP initiation, OTP validation, and 2FA password verification) so users can link their Telegram accounts from the web dashboard UI.
*   **Chat Synchronization:** Connects to Telethon and synchronizes the list of user groups, channels, and supergroups directly to the Supabase database.
*   **Custom Invite Link Generator:** Programmatically creates custom invite/subscription links for private or public communities.
*   **Session Management:** Provides endpoints to monitor login status and safely logout, cleaning up Telethon session files remotely.

---

### 4. Subscription Bot (`SubscriptionBOT2.0`)
A python-telegram-bot service that controls access to premium paid channels or groups based on payments registered in the web dashboard.

*   **Deep Link Verification:** Processes `/start <token>` URLs generated upon successful dashboard payments. Handles base64-encoded or raw UUID tokens, checking their validity against `tg_deeplinks`.
*   **Account-to-Payment Mapping:** Automatically associates the verified user's `telegram_user_id` with their active record in the database.
*   **Single-Use Tokens:** Enforces strictly one-time usage on validation tokens to prevent link sharing.
*   **Auto-Approve for Subscribers:** Listens to channel/group join requests and immediately approves them if the user has an active subscription, otherwise automatically declines the request.
*   **Automated Expired Member Eviction:** Runs an active cron-job every 120 seconds that scans for expired subscriptions, automatically kicks (bans then unbans) the expired members from Telegram chats, and updates their status in Supabase.

---

### 5. Broadcast Bot (`broadcast_bot`)
A marketing automation bot that enables administrators and creators to send formatted announcements and media broadcasts to their channels' target audiences.

*   **Interactive Audience Selection:** Uses Telegram inline button menus to display a list of active channels owned by the user.
*   **Dynamic Media Downloader:** Downloads and stores files (images, videos, documents) sent by administrators, caching them locally in a `broadcast_media` directory.
*   **Interactive Broadcast Preview:** Displays a full layout preview of the media and formatted text to the creator with confirm/cancel buttons.
*   **Queue-Based Processing:** Inserts approved messages into a database table (`tg_broadcast_tasks`) to be asynchronously sent to target users, mitigating network drops and preventing Telegram flood errors.
*   **Dashboard-Linked Account Binding:** Allows users to link their dashboard profiles to the Telegram bot via one-click deep links (`/start <uuid>`).

---

### 6. Lead Tracker & Drip Campaign Bot (`join_bot`)
A daemon system (`gap_join_bot.py` / `gapjointarcker.py`) that manages multiple client greeting bots dynamically and processes marketing campaigns.

*   **Dynamic Multi-Bot Synchronization:** Continuously monitors the Supabase `tg_tracker` database and dynamically launches or stops individual bot client instances on the fly using real-time database listener events.
*   **Welcome Greetings & Lead Capturing:** Greets new users joining groups/channels, logging their Telegram credentials (names, usernames, and user IDs) as potential leads in the database.
*   **Automated Drip Follow-ups:** Automatically schedules and sends sequential drip messages (e.g. follow-up offers, helpful tips) to newly joined users based on their join-time.
*   **Task & Broadcast Workers:** Asynchronously picks up broadcast tasks and sends out mass messages to all users who have interacted with the bot.

---

### 7. AI RAG Chatbot (`llm_bot`)
An AI agent manager that connects Telegram bots with Gemini LLMs, providing automated, context-aware customer support.

*   **Dynamic Multi-Bot Daemon:** Spawns and manages individual AI bot sessions dynamically based on settings stored in Supabase.
*   **Retrieval-Augmented Generation (RAG):** Uses Google Gemini API embeddings and vector search to query business Q&A and document uploads from Supabase, enabling bots to answer complex business-specific questions.
*   **Persistent Chat History:** Saves complete chat logs and message sequences in Supabase, keeping conversation history intact across multiple messages for contextual replies.
*   **Interactive Mentions & Chat Controls:** Operates in private messages and detects bot mentions (`@bot`) within group chats to respond appropriately.
*   **Lead Identification & Capture:** Detects and parses telephone numbers, emails, and names from chats, automatically recording them as hot leads.
*   **Anti-Loop Protection:** Implements filters to prevent responding to other bots, eliminating infinite loop risks.

---

## 🚀 Deployment & Run Guide

> [!WARNING]
> Do not run any of the userbots or helper bots manually (e.g. `python llm_bot.py`) and through PM2 at the same time. Doing so will spawn duplicate Telethon client sessions for the same bot token, leading to session file conflicts, database lockouts, and critical update receiving errors.

### Recommended Lifecycle Management via PM2

To safely monitor and manage the bot processes on your server, use PM2:

```bash
# To restart a specific bot process (e.g., ID 5)
pm2 restart 5

# To monitor logs in real-time
pm2 logs 5

# To list all running services
pm2 status
```