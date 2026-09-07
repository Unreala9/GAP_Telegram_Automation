import os
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from supabase import create_client, Client
from telethon import TelegramClient, events, functions, types
import openai
import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold
import urllib.request
import urllib.parse
import urllib.error
import json
import random

# ---- 1. Logging Setup ----
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger("LLMBotManager")
logger.info("PROCESS PID=%s", os.getpid())

# Load environment variables
load_dotenv()

SUPABASE_URL = os.environ.get("VITE_SUPABASE_URL") or os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("VITE_SUPABASE_ANON_KEY") or os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Supabase URL and Key must be defined in environment variables")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Dictionary to store running clients and their tasks
active_clients = {}
running_tasks = {}

# Dictionary to cache the bot configurations from the database
GLOBAL_BOT_CONFIGS = {} 

# Global set of bot IDs that are currently active join bots (have channel mappings)
GLOBAL_JOIN_BOT_IDS = set()

def configure_bot_allowed_updates(token: str):
    """Ensure Bot API webhook is deleted and allowed_updates is set for MTProto (Requirement 4)"""
    logger.info(f"BEFORE deleteWebhook request for token {token[:10]}...")
    try:
        allowed_updates = ["message", "business_connection", "business_message", "edited_business_message", "deleted_business_messages"]
        url = f"https://api.telegram.org/bot{token}/deleteWebhook"
        data = urllib.parse.urlencode({
            "drop_pending_updates": "true",
            "allowed_updates": json.dumps(allowed_updates)
        }).encode("utf-8")
        
        req = urllib.request.Request(url, data=data)
        logger.info(f"Triggering urllib request to {url}")
        with urllib.request.urlopen(req, timeout=10) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            logger.info(f"AFTER deleteWebhook request. Response: {res_data}")
            if res_data.get("ok"):
                logger.info(f"Successfully configured allowed_updates for bot token {token[:10]}...")
            else:
                logger.warning(f"Failed to configure allowed_updates: {res_data.get('description')}")
    except (TimeoutError, urllib.error.URLError) as net_err:
        logger.warning(f"deleteWebhook net error (TimeoutError/URLError) but continuing bot startup: {repr(net_err)}")
    except Exception as e:
        logger.exception("deleteWebhook failed but continuing bot startup")

API_ID = int(os.environ.get("TELEGRAM_API_ID", "12345678"))
API_HASH = os.environ.get("TELEGRAM_API_HASH", "dummyhash")

# ---- 2. Supabase Optimization (Thread Pool) ----
supabase_executor = ThreadPoolExecutor(max_workers=20)

async def run_supabase_query(query):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(supabase_executor, query.execute)

from collections import defaultdict
import re

# Per-conversation processing lock: bot_id:telegram_user_id
CONVERSATION_LOCKS = defaultdict(asyncio.Lock)

def mask_sensitive(text: str) -> str:
    """Mask phone numbers and secrets in logs."""
    if not text:
        return ""
    return re.sub(r'(\d{2})\d{6}(\d{2})', r'\1******\2', str(text))

async def get_user_memory(bot_id: str, telegram_user_id: int) -> dict:
    """Retrieve persistent user memory from telegram_user_memory."""
    if not telegram_user_id:
        return {}
    try:
        query = supabase.table('tg_user_memory')\
            .select('*')\
            .eq('bot_id', bot_id)\
            .eq('telegram_user_id', telegram_user_id)\
            .maybe_single()
        res = await run_supabase_query(query)
        if res and res.data:
            logger.info(f"[MEMORY] Loaded persistent memory for user {telegram_user_id}: Name={res.data.get('name')}, Stage={res.data.get('lead_stage')}")
            return res.data
        return {}
    except Exception as e:
        logger.error(f"[MEMORY] Failed to load persistent memory for user {telegram_user_id}: {e}")
        return {}

async def get_conversation_state(bot_id: str, telegram_user_id: int) -> dict:
    """Retrieve rolling conversation state and summary from telegram_conversation_state."""
    if not telegram_user_id:
        return {}
    try:
        query = supabase.table('tg_conversation_state')\
            .select('*')\
            .eq('bot_id', bot_id)\
            .eq('telegram_user_id', telegram_user_id)\
            .maybe_single()
        res = await run_supabase_query(query)
        if res and res.data:
            logger.info(f"[SUMMARY] Loaded conversation state for user {telegram_user_id}: Topic={res.data.get('current_topic')}")
            return res.data
        return {}
    except Exception as e:
        logger.error(f"[SUMMARY] Failed to load conversation state for user {telegram_user_id}: {e}")
        return {}

async def is_telegram_message_processed(bot_id: str, telegram_user_id: int, telegram_message_id: int) -> bool:
    """Idempotency check: check if telegram_message_id has already been processed for this bot and user."""
    if not telegram_message_id or not telegram_user_id:
        return False
    try:
        query = supabase.table('tg_chat_messages')\
            .select('id')\
            .eq('bot_id', bot_id)\
            .eq('telegram_user_id', telegram_user_id)\
            .eq('telegram_message_id', telegram_message_id)\
            .limit(1)
        res = await run_supabase_query(query)
        return bool(res and res.data and len(res.data) > 0)
    except Exception as e:
        logger.error(f"[DEDUP] Error checking duplicate telegram_message_id {telegram_message_id}: {e}")
        return False

async def extract_and_update_memory_and_summary(
    bot_id: str,
    telegram_user_id: int,
    user_message: str,
    assistant_response: str,
    current_memory: dict,
    current_state: dict,
    provider: str,
    api_key: str
):
    """Background async extractor that parses structured facts and updates telegram_user_memory & telegram_conversation_state."""
    if not telegram_user_id or not api_key:
        return
    try:
        # Build prompt for structured fact extraction & rolling summary
        extraction_prompt = f"""You are an expert AI Data Extractor and Conversation State Analyst.
Analyze the latest exchange between a user and an AI assistant, along with existing known memory.
Extract updated facts and summarize the ongoing conversation state.

EXISTING KNOWN MEMORY:
- Name: {current_memory.get('name') or 'Unknown'}
- Phone/WhatsApp: {current_memory.get('phone') or 'Unknown'}
- Email: {current_memory.get('email') or 'Unknown'}
- Location: {current_memory.get('location') or 'Unknown'}
- Language: {current_memory.get('language') or 'Unknown'}
- Budget: {current_memory.get('budget') or 'Unknown'}
- Requirements: {current_memory.get('requirements') or 'Unknown'}
- Services: {json.dumps(current_memory.get('services_interested') or [])}
- Lead Stage: {current_memory.get('lead_stage', 'new')}
- Important Facts: {json.dumps(current_memory.get('important_facts') or {})}
- Previous Summary: {current_state.get('summary') or 'None'}

LATEST EXCHANGE:
User: "{user_message}"
Assistant: "{assistant_response}"

INSTRUCTIONS:
1. Extract any new or updated user facts. If the user changed an existing fact (e.g. budget 10k -> 15k, or location Indore), replace with the new fact.
2. If a fact was not mentioned or changed, keep the existing value.
3. Lead Stage progression (never regress): new -> discovering_requirement -> qualified -> contact_captured -> proposal_discussion -> follow_up -> converted -> closed.
4. Generate a compact rolling summary (2-4 sentences max) capturing user intent, key decisions, stated requirements, and current next step.
5. Return ONLY a valid JSON object with the exact keys below:

{{
  "name": string or null,
  "phone": string or null,
  "email": string or null,
  "location": string or null,
  "language": string or null,
  "budget": string or null,
  "requirements": string or null,
  "services_interested": array of strings,
  "lead_stage": string,
  "last_topic": string,
  "important_facts": object,
  "rolling_summary": string
}}"""

        extracted_json = None
        if "openai" in provider:
            client = openai.AsyncOpenAI(api_key=api_key)
            ext_res = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You extract structured customer memory and conversation state in pure JSON format."},
                    {"role": "user", "content": extraction_prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.2,
                max_tokens=500
            )
            extracted_json = json.loads(ext_res.choices[0].message.content)
        elif "gemini" in provider:
            genai.configure(api_key=api_key)
            ext_model = genai.GenerativeModel('gemini-1.5-flash', generation_config={"response_mime_type": "application/json"})
            def _extract():
                res = ext_model.generate_content(extraction_prompt)
                return json.loads(res.text)
            loop = asyncio.get_running_loop()
            extracted_json = await loop.run_in_executor(supabase_executor, _extract)

        if not extracted_json:
            return

        # Prepare updated memory payload
        updated_memory = {
            'bot_id': bot_id,
            'telegram_user_id': telegram_user_id,
            'name': extracted_json.get('name') or current_memory.get('name'),
            'phone': extracted_json.get('phone') or current_memory.get('phone'),
            'email': extracted_json.get('email') or current_memory.get('email'),
            'location': extracted_json.get('location') or current_memory.get('location'),
            'language': extracted_json.get('language') or current_memory.get('language'),
            'budget': extracted_json.get('budget') or current_memory.get('budget'),
            'requirements': extracted_json.get('requirements') or current_memory.get('requirements'),
            'services_interested': extracted_json.get('services_interested') or current_memory.get('services_interested') or [],
            'lead_stage': extracted_json.get('lead_stage') or current_memory.get('lead_stage') or 'new',
            'last_topic': extracted_json.get('last_topic') or current_memory.get('last_topic'),
            'preferences': {**(current_memory.get('preferences') or {}), **(extracted_json.get('preferences') or {})},
            'important_facts': {**(current_memory.get('important_facts') or {}), **(extracted_json.get('important_facts') or {})},
            'updated_at': 'now()'
        }

        # Upsert telegram_user_memory
        mem_upsert = supabase.table('tg_user_memory')\
            .upsert(updated_memory, on_conflict='bot_id,telegram_user_id')
        await run_supabase_query(mem_upsert)
        logger.info(f"[MEMORY] Updated persistent memory for user {telegram_user_id} (Name: {updated_memory.get('name')}, Stage: {updated_memory.get('lead_stage')})")

        # Prepare updated conversation state payload
        updated_state = {
            'bot_id': bot_id,
            'telegram_user_id': telegram_user_id,
            'summary': extracted_json.get('rolling_summary') or current_state.get('summary'),
            'current_topic': extracted_json.get('last_topic') or current_state.get('current_topic'),
            'conversation_stage': extracted_json.get('lead_stage') or current_state.get('conversation_stage') or 'new',
            'last_message_at': 'now()',
            'updated_at': 'now()'
        }

        # Upsert telegram_conversation_state
        state_upsert = supabase.table('tg_conversation_state')\
            .upsert(updated_state, on_conflict='bot_id,telegram_user_id')
        await run_supabase_query(state_upsert)
        logger.info(f"[SUMMARY] Updated conversation state for user {telegram_user_id}")

    except Exception as e:
        logger.error(f"[MEMORY/SUMMARY] Background extraction error for user {telegram_user_id}: {e}")

async def generate_llm_response(bot_id: str, user_message: str, telegram_user_id: int = None) -> tuple:
    """Generate a response using 3-layer memory architecture: Persistent Memory + Rolling Summary + Recent History."""
    config = GLOBAL_BOT_CONFIGS.get(bot_id)
    if not config:
        return "Sorry, my configuration is currently unavailable.", None, {}, {}, "", ""
    
    if hasattr(config, "get"):
        provider = config.get("provider", "").lower()
        api_key = config.get("api_key")
        business_info = config.get("business_info", "") or ""
        support_name = config.get("support_name", "AI Assistant")
        knowledge_base_text = config.get("knowledge_base_text") or ""
        system_prompt_custom = config.get("system_prompt") or ""
    else:
        provider = getattr(config, "provider", "").lower()
        api_key = getattr(config, "api_key", None)
        business_info = getattr(config, "business_info", "") or ""
        support_name = getattr(config, "support_name", "AI Assistant")
        knowledge_base_text = getattr(config, "knowledge_base_text", "") or ""
        system_prompt_custom = getattr(config, "system_prompt", "") or ""

    if not api_key:
        return "⚠️ Setup Error: The API key for this bot has not been configured.", None, {}, {}, "", ""

    logger.info(f"generate_llm_response called: bot_id={bot_id}, telegram_user_id={telegram_user_id}, user_message={user_message[:50]}")

    # 1. Fetch Persistent User Memory (Layer 3) & Rolling Summary (Layer 2)
    current_memory = {}
    current_state = {}
    if telegram_user_id:
        try:
            current_memory = await get_user_memory(bot_id, telegram_user_id)
            current_state = await get_conversation_state(bot_id, telegram_user_id)
        except Exception as fetch_err:
            logger.error(f"Error fetching memory/state for user {telegram_user_id}: {fetch_err}")

    # 2. Generate Query Embedding and Match Chunks / Media (RAG)
    query_embedding = None
    retrieved_context = ""
    matched_image_url = None
    matched_image_caption = None

    try:
        if "openai" in provider:
            client = openai.AsyncOpenAI(api_key=api_key)
            res = await client.embeddings.create(
                input=user_message,
                model="text-embedding-3-small"
            )
            query_embedding = res.data[0].embedding
        elif "gemini" in provider:
            genai.configure(api_key=api_key)
            def _gemini_embed():
                res = genai.embed_content(
                    model="models/text-embedding-004",
                    content=user_message
                )
                return res['embedding']
            loop = asyncio.get_running_loop()
            gemini_vector = await loop.run_in_executor(supabase_executor, _gemini_embed)
            query_embedding = gemini_vector + [0.0] * (1536 - len(gemini_vector))

        if query_embedding:
            # Query match_bot_chunks RPC via Supabase Client
            chunks_query = supabase.rpc("match_bot_chunks", {
                "query_embedding": query_embedding,
                "match_threshold": 0.3,
                "match_count": 5,
                "p_bot_id": bot_id
            })
            chunks_res = await run_supabase_query(chunks_query)
            if chunks_res and chunks_res.data:
                retrieved_context = "\n\n".join([chunk["content"] for chunk in chunks_res.data])
                logger.info(f"RAG: Found {len(chunks_res.data)} matching text chunks.")

            # Query match_bot_media RPC via Supabase Client
            media_query = supabase.rpc("match_bot_media", {
                "query_embedding": query_embedding,
                "match_threshold": 0.35,
                "match_count": 1,
                "p_bot_id": bot_id
            })
            media_res = await run_supabase_query(media_query)
            if media_res and media_res.data and len(media_res.data) > 0:
                matched_image_url = media_res.data[0]["image_url"]
                matched_image_caption = media_res.data[0]["caption"]
                logger.info(f"RAG Media: Found matching image: {matched_image_url} (Caption: {matched_image_caption})")
    except Exception as rag_err:
        logger.error(f"Error during RAG or Embedding generation: {rag_err}")

    # 3. Construct 6-Tier Context
    # Tier 1: Base System Prompt
    if system_prompt_custom and system_prompt_custom.strip():
        system_prompt = system_prompt_custom.strip()
    elif business_info and len(business_info) > 200:
        system_prompt = f"Your name is {support_name}.\n\n{business_info}"
    else:
        system_prompt = f"Your name is {support_name}. You are a professional AI consultant and assistant."

    # Tier 2: Knowledge Base & RAG Context
    kb_content = (knowledge_base_text or business_info or "").strip()
    if kb_content and kb_content not in system_prompt:
        system_prompt += f"\n\n=== BUSINESS KNOWLEDGE ===\n{kb_content}\n=== END BUSINESS KNOWLEDGE ==="

    if retrieved_context.strip():
        system_prompt += f"\n\n=== RELEVANT CONTEXT (RAG) ===\n{retrieved_context}\n=== END RELEVANT CONTEXT ==="

    if matched_image_url:
        system_prompt += f"\n\n[NOTICE: A relevant image is also being sent to the user: '{matched_image_caption}'. Acknowledge/mention this image naturally in your reply (e.g. 'You can see the details in the photo below:').]"

    # Tier 3: Persistent User Memory (Layer 3)
    memory_lines = []
    if current_memory.get('name'):
        memory_lines.append(f"Name: {current_memory['name']}")
    if current_memory.get('phone'):
        memory_lines.append(f"Phone/WhatsApp: {current_memory['phone']}")
    if current_memory.get('email'):
        memory_lines.append(f"Email: {current_memory['email']}")
    if current_memory.get('location'):
        memory_lines.append(f"Location: {current_memory['location']}")
    if current_memory.get('budget'):
        memory_lines.append(f"Budget: {current_memory['budget']}")
    if current_memory.get('requirements'):
        memory_lines.append(f"Requirements: {current_memory['requirements']}")
    if current_memory.get('services_interested'):
        memory_lines.append(f"Services Interested: {', '.join(current_memory['services_interested'])}")
    if current_memory.get('lead_stage'):
        memory_lines.append(f"Lead Stage: {current_memory['lead_stage']}")
    if current_memory.get('important_facts'):
        memory_lines.append(f"Important Facts: {json.dumps(current_memory['important_facts'])}")

    if memory_lines:
        system_prompt += f"\n\n=== KNOWN USER MEMORY (PERSISTENT FACTS) ===\n" + "\n".join(memory_lines) + "\n=== END KNOWN USER MEMORY ==="

    # Tier 4: Rolling Conversation Summary (Layer 2)
    if current_state.get('summary'):
        system_prompt += f"\n\n=== ROLLING CONVERSATION SUMMARY ===\n{current_state['summary']}\n=== END ROLLING CONVERSATION SUMMARY ==="

    # Tier 5: Mandatory Continuity & Universal Memory Rules
    system_prompt += """

=== UNIVERSAL CONVERSATION MEMORY & CONTINUITY RULES (APPLIES TO ALL CHATS) ===
1. DEEP CONTEXT & STATE AWARENESS:
   - You MUST thoroughly review the ongoing conversation history, Persistent User Memory, and Rolling Summary before crafting every reply.
   - Maintain continuous memory of all user details: Name, Phone/WhatsApp, Location, Specific Needs, Budget, and Lead Stage.
   - Persistent User Memory contains confirmed facts. NEVER contradict them or ask for information already known unless the user updates it.

2. NEVER RESET ON GREETINGS OR PAUSES:
   - If the user sends a greeting (e.g. "hi", "hello", "hey", "namaste", "good morning") or a short phrase mid-conversation or after a delay:
     * NEVER reply with a blank, generic initial welcome (e.g. NEVER say "Hello! How can I assist you today?").
     * Address the user by Name (if known) and warmly connect back to the exact last topic or inquiry discussed.
     * Example: "Hey [Name]! We were talking about [last discussed item/topic]. How would you like to proceed?"

3. INTELLIGENT RECALL ON REFERENCES ("You already know", "Maine bataya tha", "What about X?"):
   - When the user refers to earlier statements (e.g. "tumhe pata hai", "maine bataya tha", "you know this", "jaise maine kaha", "what about my request?"):
     * Directly retrieve the exact details from Persistent Memory or Recent History.
     * Explicitly confirm and acknowledge those details to reassure the user that you have remembered everything.
     * Example: "Yes [Name], you mentioned earlier that you're looking for [specific requirement/preference]. Here is the update on that..."

4. NO REPETITIVE QUALIFICATION OR UNPROMPTED CATALOG DUMPING:
   - Once a user has already provided specific details (Name, Contact, Requirement, Budget):
     * NEVER ask them the same question again.
     * NEVER dump large generic pricing or service menus unless the user specifically asks to see all options.
     * Keep the interaction moving forward toward the next logical step.

5. NATURAL, ADAPTIVE HUMAN TONE:
   - Speak like an attentive, knowledgeable human assistant who actively listens and remembers.
   - Keep answers crisp and focused (2-3 concise sentences per turn).
   - Reply naturally in the exact language style the user is speaking in (English, Hinglish, Roman Hindi, etc.).
"""

    # Tier 6: Recent Conversation History (Layer 1 - Last 25 messages chronologically)
    history = []
    if telegram_user_id:
        try:
            history_query = supabase.table('tg_chat_messages')\
                .select('role, content')\
                .eq('bot_id', bot_id)\
                .eq('telegram_user_id', telegram_user_id)\
                .order('created_at', desc=True)\
                .limit(25)
            res = await run_supabase_query(history_query)
            if res and res.data:
                history = list(reversed(res.data))
                logger.info(f"[HISTORY] Loaded {len(history)} messages for user {telegram_user_id}.")
            else:
                logger.info(f"[HISTORY] No prior history found for user {telegram_user_id}.")
        except Exception as e:
            logger.error(f"[HISTORY] Failed to fetch chat history for user {telegram_user_id}: {e}")
        
    try:
        # ---- OpenAI Handler ----
        if "openai" in provider:
            client = openai.AsyncOpenAI(api_key=api_key)
            
            messages = [{"role": "system", "content": system_prompt}]
            
            # Format and append previous history
            for msg in history:
                role = msg.get("role")
                content = (msg.get("content") or "").strip()
                if content and role in ("user", "assistant"):
                    messages.append({"role": role, "content": content})
            
            # Append the current incoming user message exactly once
            messages.append({"role": "user", "content": user_message})

            logger.info(f"Sending {len(messages)} messages to OpenAI for bot {bot_id} (user {telegram_user_id})")

            response = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                max_tokens=600,
                temperature=0.7
            )
            return response.choices[0].message.content, matched_image_url, current_memory, current_state, provider, api_key

        # ---- Gemini Handler ----
        elif "gemini" in provider:
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel('gemini-1.5-flash',
                                        system_instruction=system_prompt)
            safety_settings = {
                HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
                HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
            }
            
            gemini_contents = []
            for msg in history:
                role = "model" if msg.get("role") == "assistant" else "user"
                content = (msg.get("content") or "").strip()
                if not content:
                    continue
                if not gemini_contents and role == "model":
                    continue
                if gemini_contents and gemini_contents[-1]["role"] == role:
                    gemini_contents[-1]["parts"][0] += f"\n{content}"
                else:
                    gemini_contents.append({"role": role, "parts": [content]})
            
            if gemini_contents and gemini_contents[-1]["role"] == "user":
                gemini_contents[-1]["parts"][0] += f"\n{user_message}"
            else:
                gemini_contents.append({"role": "user", "parts": [user_message]})

            def _generate():
                try:
                    res = model.generate_content(gemini_contents, safety_settings=safety_settings)
                    return res.text
                except Exception as ex:
                    logger.error(f"Gemini error: {ex}")
                    raise
                
            loop = asyncio.get_running_loop()
            result_text = await loop.run_in_executor(supabase_executor, _generate)
            return result_text, matched_image_url, current_memory, current_state, provider, api_key

        else:
            return f"⚠️ Unsupported AI provider: {provider}", None, current_memory, current_state, provider, api_key
            
    except Exception as e:
        error_msg = str(e).lower()
        if "api key" in error_msg or "unauthorized" in error_msg or "authentication" in error_msg or "invalid_api_key" in error_msg:
            return "⚠️ Setup Error: The provided LLM API key is invalid or has expired. Please update it in the dashboard.", None, current_memory, current_state, provider, api_key
        elif "quota" in error_msg or "rate limit" in error_msg:
            return "⚠️ Service Error: The LLM provider quota has been exceeded or rate-limited.", None, current_memory, current_state, provider, api_key
        else:
            logger.error(f"LLM Error for bot {bot_id}: {e}")
            return "⚠️ An error occurred while generating a response. Please try again later.", None, current_memory, current_state, provider, api_key

async def get_or_create_telegram_session(bot_id: str, telegram_user_id: int, user_name: str) -> str:
    """Find or create a chatbot session for a Telegram user chatting with a specific bot."""
    try:
        logger.info(f"get_or_create_telegram_session called for bot_id={bot_id}, telegram_user_id={telegram_user_id}, user_name={user_name}")
        # 1. Try to find existing session mapping
        query_select = supabase.table('tg_bot_sessions')\
            .select('id')\
            .eq('bot_id', bot_id)\
            .eq('telegram_user_id', telegram_user_id)
        
        res = await run_supabase_query(query_select)
        if res.data and len(res.data) > 0:
            session_id = res.data[0]['id']
            logger.info(f"Found existing telegram bot session: {session_id}")
            return session_id
            
        # 2. If not found, create new session in chatbot_sessions
        logger.info("No existing session found. Creating a new session in chatbot_sessions...")
        query_insert_session = supabase.table('tg_chatbot_sessions')\
            .insert({'status': 'active'})
            
        session_res = await run_supabase_query(query_insert_session)
        if not session_res.data or len(session_res.data) == 0:
            raise ValueError("Failed to create chatbot session")
            
        session_id = session_res.data[0]['id']
        logger.info(f"Successfully created new chatbot session: {session_id}")
        
        # 3. Create mapping in telegram_bot_sessions
        logger.info(f"Creating session mapping in telegram_bot_sessions with id={session_id}...")
        query_insert_mapping = supabase.table('tg_bot_sessions')\
            .insert({
                'id': session_id,
                'bot_id': bot_id,
                'telegram_user_id': telegram_user_id,
                'user_name': user_name
            })
            
        await run_supabase_query(query_insert_mapping)
        logger.info(f"Successfully mapped session: {session_id} to bot: {bot_id} for user: {telegram_user_id}")
        return session_id
        
    except Exception as e:
        logger.error(f"Error in get_or_create_telegram_session: {e}", exc_info=True)
        raise

async def start_bot(config: dict):
    bot_id = config['bot_id']
    token = config['bot_token']
    logger.info(f"Preparing to start bot config: bot_id={bot_id}, token_prefix={token[:10]}")
    logger.info(f"Starting LLM bot: {bot_id}")
    
    client = None
    try:
        logger.info(f"ENTERING start_bot bot_id={bot_id}")

        # Configure allowed updates for bot (Requirement 4)
        logger.info("Skipping deleteWebhook for Telethon MTProto startup")
        
        # Load from the same sessions directory as bot.py
        client = TelegramClient(f"sessions/llm_bot_{bot_id}", API_ID, API_HASH)
        
        logger.info(f"BEFORE client.start for bot_id={bot_id}")
        import telethon
        logger.info(f"TELETHON VERSION: {telethon.__version__}")
        
        await client.start(bot_token=token)
        logger.info("client.start completed")
        
        logger.info(f"BEFORE client.get_me for bot_id={bot_id}")
        me = await client.get_me()
        logger.info(f"AFTER client.get_me for bot_id={bot_id}")
        
        logger.info(f"BOT VERIFIED username=@{me.username}, id={me.id}, bot_id={bot_id}")
        logger.info("Business Mode must be ON in BotFather and bot must be connected in Telegram Business > Chatbots")
        
        @client.on(events.NewMessage)
        async def handler(event):
            # Ignore outgoing messages sent by the bot itself
            if getattr(event, 'out', False):
                return

            # Only respond to private messages
            if not event.is_private:
                return

            # Exclude service messages or empty messages
            if event.message.action or not event.message.text:
                return

            user_message = event.message.text
            msg_id = getattr(event.message, 'id', None)
            user_id = event.sender_id

            # Ignore /start commands ONLY IF this bot is also configured as a Join Bot (has active channel mappings)
            if user_message.strip().startswith('/start'):
                if bot_id in GLOBAL_JOIN_BOT_IDS:
                    logger.info(f"LLM Bot {bot_id}: Ignoring /start because it has active channel mappings (handled by Join Bot).")
                    return
                # Otherwise, let the LLM handle /start to welcome the user (pure support bot)

            # Deduplication / Idempotency Check
            if msg_id and await is_telegram_message_processed(bot_id, user_id, msg_id):
                logger.info(f"[DEDUP] Duplicate Telegram message {msg_id} ignored for bot {bot_id}, user {user_id}")
                return

            logger.info(f"NORMAL MESSAGE RECEIVED bot_id={bot_id}, chat_id={event.chat_id}, sender_id={user_id}, msg_id={msg_id}, text={user_message[:60]}")
            
            # Acquire per-conversation lock to prevent rapid-message race conditions
            conv_lock_key = f"{bot_id}:{user_id}"
            async with CONVERSATION_LOCKS[conv_lock_key]:
                # Fetch sender details to construct user name
                user_name = f"User {user_id}"
                try:
                    sender = await event.get_sender()
                    if sender:
                        first_name = getattr(sender, 'first_name', '') or ''
                        last_name = getattr(sender, 'last_name', '') or ''
                        username = getattr(sender, 'username', '') or ''
                        
                        full_name = f"{first_name} {last_name}".strip()
                        if full_name:
                            user_name = full_name
                        elif username:
                            user_name = username
                except Exception as e:
                    logger.error(f"Failed to fetch sender profile: {e}")

                # Get or create the mapped Supabase session ID (legacy)
                session_id = None
                try:
                    session_id = await get_or_create_telegram_session(bot_id, user_id, user_name)
                except Exception as e:
                    logger.error(f"Failed to resolve session ID for Telegram chat: {e}")

                # Show "typing..." status and get response using 3-layer memory
                async with client.action(event.chat_id, 'typing'):
                    response_text, matched_image_url, current_memory, current_state, provider, api_key = await generate_llm_response(bot_id, user_message, user_id)
                    
                    if matched_image_url:
                        try:
                            await client.send_file(event.chat_id, file=matched_image_url, caption=response_text)
                        except Exception as send_file_err:
                            logger.error(f"Failed to send image file via Telethon, falling back to text: {send_file_err}")
                            await event.respond(response_text)
                    else:
                        await event.respond(response_text)
                    
                    # 1. Save user message and bot response to legacy chatbot_messages
                    if session_id:
                        try:
                            await run_supabase_query(supabase.table('tg_chatbot_messages').insert([
                                {'session_id': session_id, 'role': 'user', 'content': user_message},
                                {'session_id': session_id, 'role': 'assistant', 'content': response_text}
                            ]))
                        except Exception as e:
                            logger.error(f"Failed to save to legacy chatbot_messages: {e}")

                    # 2. Save user message and bot response to dedicated telegram_chat_messages with telegram_message_id
                    try:
                        await run_supabase_query(supabase.table('tg_chat_messages').insert([
                            {'bot_id': bot_id, 'telegram_user_id': user_id, 'user_name': user_name, 'role': 'user', 'content': user_message, 'telegram_message_id': msg_id},
                            {'bot_id': bot_id, 'telegram_user_id': user_id, 'user_name': user_name, 'role': 'assistant', 'content': response_text}
                        ]))
                        logger.info(f"[DB] Conversation persisted for bot {bot_id}, user {user_id}")
                    except Exception as e:
                        logger.error(f"Failed to save to telegram_chat_messages: {e}")

                    # 3. Asynchronously extract structured memory and update conversation state (non-blocking)
                    if api_key:
                        asyncio.create_task(extract_and_update_memory_and_summary(
                            bot_id, user_id, user_message, response_text, current_memory, current_state, provider, api_key
                        ))


        @client.on(events.Raw)
        async def raw_handler(event):
            try:
                # Inspect Updates containers if matching
                update = event
                if not update:
                    return

                updates_to_process = []
                if isinstance(update, (types.Updates, types.UpdatesCombined)):
                    for inner_update in update.updates:
                        updates_to_process.append(inner_update)
                else:
                    updates_to_process.append(update)

                for u in updates_to_process:
                    if isinstance(u, types.UpdateBotBusinessConnect):
                        connection = u.connection
                        logger.info(f"UpdateBotBusinessConnect connection_id={connection.connection_id}, user_id={connection.user_id}, disabled={connection.disabled}")
                        continue

                    elif isinstance(u, types.UpdateBotEditBusinessMessage):
                        continue

                    elif isinstance(u, types.UpdateBotDeleteBusinessMessage):
                        continue

                    elif isinstance(u, types.UpdateBotNewBusinessMessage):
                        connection_id = u.connection_id
                        msg = u.message
                        if not msg:
                            continue

                        # CRITICAL: Ignore outgoing messages (sent by the business or bot itself)
                        if getattr(msg, 'out', False):
                            logger.info("Skipping business message because msg.out=True (outgoing message)")
                            continue

                        text = getattr(msg, "message", None)
                        if not text:
                            continue

                        peer = msg.peer_id
                        msg_id = getattr(msg, 'id', None)

                        # Extract chat_id for Supabase mapping and sender info
                        if hasattr(peer, 'user_id'):
                            chat_id = peer.user_id
                        elif hasattr(peer, 'channel_id'):
                            chat_id = peer.channel_id
                        elif hasattr(peer, 'chat_id'):
                            chat_id = peer.chat_id
                        else:
                            chat_id = getattr(msg, 'chat_id', None)

                        user_message = text

                        # Extract user_id/sender_id
                        user_id = getattr(msg, 'sender_id', None)
                        if not user_id:
                            if hasattr(msg, 'from_id') and hasattr(msg.from_id, 'user_id'):
                                user_id = msg.from_id.user_id
                            else:
                                user_id = chat_id or getattr(msg, 'chat_id', None)

                        if not user_id:
                            logger.error("Could not determine sender/user_id for business message")
                            continue

                        # Deduplication / Idempotency Check
                        if msg_id and await is_telegram_message_processed(bot_id, user_id, msg_id):
                            logger.info(f"[DEDUP] Duplicate business message {msg_id} ignored for bot {bot_id}, user {user_id}")
                            continue

                        logger.info(f"BUSINESS MESSAGE RECEIVED bot_id={bot_id}, connection_id={connection_id}, user_id={user_id}, msg_id={msg_id}, text={text[:60]}")

                        # Acquire per-conversation lock
                        conv_lock_key = f"{bot_id}:{user_id}"
                        async with CONVERSATION_LOCKS[conv_lock_key]:
                            # Fetch sender details to construct user name
                            user_name = f"User {user_id}"
                            try:
                                sender = await client.get_entity(user_id)
                                if sender:
                                    first_name = getattr(sender, 'first_name', '') or ''
                                    last_name = getattr(sender, 'last_name', '') or ''
                                    username = getattr(sender, 'username', '') or ''
                                    
                                    full_name = f"{first_name} {last_name}".strip()
                                    if full_name:
                                        user_name = full_name
                                    elif username:
                                        user_name = username
                            except Exception as e:
                                logger.error(f"Failed to fetch sender profile: {e}")

                            # Get or create the mapped Supabase session ID (legacy)
                            session_id = None
                            try:
                                session_id = await get_or_create_telegram_session(bot_id, user_id, user_name)
                            except Exception as e:
                                logger.error(f"Failed to resolve session ID for Telegram chat: {e}")

                            # Generate response using 3-layer memory
                            response_text, matched_image_url, current_memory, current_state, provider, api_key = await generate_llm_response(bot_id, user_message, user_id)
                            
                            # Send reply using Telegram business connection
                            try:
                                import httpx

                                bot_token = token
                                tgt_chat_id = user_id or chat_id

                                if matched_image_url:
                                    payload = {
                                        "chat_id": tgt_chat_id,
                                        "photo": matched_image_url,
                                        "caption": response_text,
                                        "business_connection_id": connection_id
                                    }
                                    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
                                else:
                                    payload = {
                                        "chat_id": tgt_chat_id,
                                        "text": response_text,
                                        "business_connection_id": connection_id
                                    }
                                    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

                                async with httpx.AsyncClient(timeout=15) as http:
                                    r = await http.post(url, json=payload)
                                    logger.info(f"BUSINESS SEND RESULT: status={r.status_code}")
                                    r.raise_for_status()

                            except Exception:
                                logger.exception("Business send failed with exception")
                                continue

                            # 1. Save user message and bot response to legacy chatbot_messages
                            if session_id:
                                try:
                                    await run_supabase_query(supabase.table('tg_chatbot_messages').insert([
                                        {'session_id': session_id, 'role': 'user', 'content': user_message},
                                        {'session_id': session_id, 'role': 'assistant', 'content': response_text}
                                    ]))
                                except Exception as e:
                                    logger.error(f"Failed to save to legacy chatbot_messages: {e}")

                            # 2. Save user message and bot response to dedicated telegram_chat_messages
                            try:
                                await run_supabase_query(supabase.table('tg_chat_messages').insert([
                                    {'bot_id': bot_id, 'telegram_user_id': user_id, 'user_name': user_name, 'role': 'user', 'content': user_message, 'telegram_message_id': msg_id},
                                    {'bot_id': bot_id, 'telegram_user_id': user_id, 'user_name': user_name, 'role': 'assistant', 'content': response_text}
                                ]))
                                logger.info(f"[DB] Conversation persisted for business bot {bot_id}, user {user_id}")
                            except Exception as e:
                                logger.error(f"Failed to save to telegram_chat_messages: {e}")

                            # 3. Asynchronously extract structured memory and update conversation state (non-blocking)
                            if api_key:
                                asyncio.create_task(extract_and_update_memory_and_summary(
                                    bot_id, user_id, user_message, response_text, current_memory, current_state, provider, api_key
                                ))

                    else:
                        pass
                        continue
            except Exception as raw_err:
                logger.exception(f"Error inside raw_handler for bot_id={bot_id}: {raw_err}")

        active_clients[bot_id] = client
        await client.run_until_disconnected()

    except asyncio.CancelledError:
        logger.info(f"LLM Bot {bot_id} start_bot task was cancelled. Shutting down client...")
        if client:
            try:
                await client.disconnect()
            except Exception as disc_err:
                logger.error(f"Error disconnecting cancelled bot client {bot_id}: {disc_err}")
        active_clients.pop(bot_id, None)
        running_tasks.pop(bot_id, None)
        raise
    except Exception as e:
        logger.exception(
            f"START_BOT FAILED bot_id={bot_id}: {repr(e)}"
        )
        if client:
            try:
                await client.disconnect()
            except Exception as disc_err:
                logger.error(f"Error disconnecting failed bot client {bot_id}: {disc_err}")
        active_clients.pop(bot_id, None)
        running_tasks.pop(bot_id, None)
        raise


async def bot_runner():
    logger.info("LLM Bot Manager Started. Polling Supabase every 15 seconds for active chatbot configs...")
    while True:
        try:
            # 1. Fetch active channel mappings to identify join bots
            try:
                mappings_query = supabase.table('tg_bot_channel_mappings').select('bot_id').eq('status', 'Active')
                mappings_res = await run_supabase_query(mappings_query)
                GLOBAL_JOIN_BOT_IDS.clear()
                if mappings_res.data:
                    for m in mappings_res.data:
                        if m.get('bot_id'):
                            GLOBAL_JOIN_BOT_IDS.add(m['bot_id'])
                logger.info(f"Active Join Bot IDs in database: {list(GLOBAL_JOIN_BOT_IDS)}")
            except Exception as map_err:
                logger.error(f"Error fetching channel mappings for LLM Bot Manager: {map_err}")

            # 2. Join chatbot_configs with telegram_tracker to get the bot_token
            # Also fetch knowledge_base_text (n8n-generated full system prompt)
            query = supabase.table('tg_chatbot_configs')\
                .select('*, tg_tracker(bot_token)')\
                .eq('status', 'active')
            
            response = await run_supabase_query(query)
            configs = response.data
            
            if configs is None:
                configs = []
            elif hasattr(configs, "data"):
                # Handle cases where response.data contains another layer of data
                configs = configs.data

            current_bot_ids = set()
            for config in configs:
                bot_id = config['bot_id']
                tracker_data = config.get('tg_tracker')
                
                # Skip if we couldn't fetch the token
                if not tracker_data or not tracker_data.get('bot_token'):
                    continue
                    
                bot_token = tracker_data['bot_token']
                
                # Merge into a single flat dict
                full_config = {**config, 'bot_token': bot_token}
                
                # Update global cache
                GLOBAL_BOT_CONFIGS[bot_id] = full_config
                current_bot_ids.add(bot_id)
                
                # Check if bot is already active and connected (Requirement 3)
                if bot_id in active_clients:
                    try:
                        is_connected = active_clients[bot_id].is_connected()
                    except Exception:
                        is_connected = False
                    if is_connected:
                        if bot_id in running_tasks and not running_tasks[bot_id].done():
                            logger.info(f"LLM Bot {bot_id} is already connected and task is running. Skipping start.")
                            continue
                        else:
                            logger.warning(f"LLM Bot {bot_id} client is connected but task is missing or done. Resetting client...")
                            active_clients.pop(bot_id, None)

                if bot_id not in running_tasks or running_tasks[bot_id].done():
                    if bot_id in running_tasks and running_tasks[bot_id].done():
                        try:
                            if running_tasks[bot_id].cancelled():
                                logger.info(f"LLM Bot {bot_id} task was cancelled.")
                            else:
                                exc = running_tasks[bot_id].exception()
                                if exc:
                                    logger.error(f"LLM Bot {bot_id} task failed with exception: {exc}", exc_info=exc)
                        except Exception as check_exc:
                            logger.error(f"Error checking completed task exception for bot {bot_id}: {check_exc}")
                    
                    logger.info(f"Creating task for start_bot: bot_id={bot_id}")
                    try:
                        task = asyncio.create_task(start_bot(full_config))
                        running_tasks[bot_id] = task
                        logger.info(f"Task created successfully for bot_id={bot_id}")
                    except Exception as task_err:
                        logger.exception(f"Failed to create task for bot_id={bot_id}: {task_err}")
                    
            # Check for deleted/paused bots
            for bot_id in list(running_tasks.keys()):
                if bot_id not in current_bot_ids:
                    logger.info(f"LLM Bot {bot_id} is no longer active. Stopping...")
                    running_tasks[bot_id].cancel()
                    
                    if bot_id in GLOBAL_BOT_CONFIGS:
                        GLOBAL_BOT_CONFIGS.pop(bot_id, None)
                        
                    if bot_id in active_clients:
                        try:
                            fut = active_clients[bot_id].disconnect()
                            if asyncio.iscoroutine(fut) or asyncio.isfuture(fut):
                                await fut
                        except Exception as e:
                            logger.error(f"Failed to disconnect bot {bot_id}: {e}")
                        active_clients.pop(bot_id, None)
                        running_tasks.pop(bot_id, None)

        except Exception as e:
            logger.error(f"Error in LLM bot manager loop: {e}")
            
        await asyncio.sleep(15)

if __name__ == "__main__":
    if not os.environ.get("TELEGRAM_API_ID"):
        logger.warning("TELEGRAM_API_ID and TELEGRAM_API_HASH are not set in .env.")
    try:
        asyncio.run(bot_runner())
    except KeyboardInterrupt:
        logger.info("LLM Bot Manager manually stopped.")
