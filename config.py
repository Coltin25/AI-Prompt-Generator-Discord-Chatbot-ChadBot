import os
import sys
import discord
from dotenv import load_dotenv
from discord.ext import commands
import asyncio
import safety
from persistent_state import PersistentDict, PersistentListDict
from openai import (
    AsyncOpenAI,
    APIConnectionError,
    APITimeoutError,
    RateLimitError,
    APIStatusError,
)
# ─────────────────────────────────────────────────────────────
# Windows compatibility for asyncio
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Load environment variables
load_dotenv()

# Validate required environment variables
required_env_vars = {
    "CHATBOT": "Discord bot token",
    "OPEN_AI_API": "OpenAI API key",
    "AZURE_TTS": "Azure TTS subscription key",
    "AZURE_REGION": "Azure region",
    "HOME_GUILD_ID": "Your home/admin Discord server ID",
}

missing_keys = []

missing = [k for k in required_env_vars if not os.getenv(k)]
if missing:
    print("Missing:", ", ".join(missing))
    exit(1)

CHATBOT = os.getenv("CHATBOT")
OPENAI_KEY = os.getenv("OPEN_AI_API")
GROK_API_KEY = os.getenv("GROK_API_KEY")
AZURE_SPEECH_KEY = os.getenv("AZURE_TTS")
HOME_GUILD_ID = int(os.getenv("HOME_GUILD_ID"))
AZURE_REGION = os.getenv("AZURE_REGION")

DEFAULT_VOICE = "en-US-DavisNeural"

GROK_MODEL = os.getenv("GROK_MODEL", "grok-4-0709")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o")

FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")
LIBOPUS_PATH = os.getenv("OPUS_LIBRARY_PATH", None)

SUPPORTED_STYLES = {"cheerful","lyrical","serious","affectionate","empathetic","documentary-narration","advertisement_upbeat","newscast-casual","newscast-formal","disgruntled","sad","angry","excited","calm", "whispering", "shouting", "terrified"}
VOICE_LIST = [
    "en-US-DavisNeural",  # Default voice
    "en-US-JennyNeural",
    "en-US-AshleyNeural",
    "en-US-ChristopherNeural",
    "en-US-EmmaNeural",
    "en-US-GuyNeural",
    "en-US-JasonNeural",
    "en-US-KateNeural",
    "en-US-LiamNeural",
    "en-US-MiaNeural",
    "en-US-OliviaNeural",
    "en-US-NoahNeural",
    "en-US-SophiaNeural",
    "en-US-WilliamNeural",
    "en-US-AriaNeural",
    "en-US-BenjaminNeural",
    "en-US-CharlotteNeural",
    "en-US-EthanNeural",
    "en-US-IsabellaNeural", 
    "en-US-JacobNeural",
    "en-US-LilyNeural",
]
# ─────────────────────────────────────────────────────────────
grok_client = AsyncOpenAI(
    api_key=GROK_API_KEY,
    base_url="https://api.x.ai/v1",
) if GROK_API_KEY else None

openai_client = AsyncOpenAI(
    api_key=OPENAI_KEY
) if OPENAI_KEY else None

def should_fallback_to_openai(error: Exception) -> bool:
    """
    Fallback only for temporary/provider/account-availability issues.
    Do NOT fallback for bad request errors like 400 or 422.
    """
    if isinstance(error, (APIConnectionError, APITimeoutError, RateLimitError)):
        return True

    if isinstance(error, APIStatusError):
        status = error.status_code

        # Grok/xAI down, overloaded, gateway issues, rate limits, etc.
        if status in {408, 409, 429, 500, 502, 503, 504}:
            return True

        # Optional: fallback if Grok account/billing/permission has an issue.
        # If this hides too much, remove 401/402/403.
        if status in {401, 402, 403}:
            return True

        # Important: 400/422 usually means your code/request is wrong.
        return False

    return False

async def chat_with_ai(messages, max_tokens=150, temperature=0.7):
    """
    Try Grok first. If Grok fails for a fallback-safe reason,
    retry with OpenAI.
    """
    # Sexual content is blocked at the door — don't even spend an API call on it.
    if messages and messages[-1].get("role") == "user" and safety.is_sexual(messages[-1]["content"]):
        return safety.SAFE_REFUSAL, "filtered"

    if grok_client:
        try:
            resp = await grok_client.chat.completions.create(
                model=GROK_MODEL,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            reply = resp.choices[0].message.content.strip()
            if safety.is_sexual(reply):
                return safety.SAFE_REFUSAL, "filtered"
            return reply, "grok"

        except Exception as e:
            print("[AI] Grok failed:", repr(e))

            if hasattr(e, "response") and e.response is not None:
                print("[AI] Grok response:", e.response.text)

            if not should_fallback_to_openai(e):
                raise

            print("[AI] Falling back to OpenAI...")

    if not openai_client:
        raise RuntimeError("No OpenAI fallback client configured.")

    resp = await openai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    reply = resp.choices[0].message.content.strip()
    if safety.is_sexual(reply):
        return safety.SAFE_REFUSAL, "filtered"
    return reply, "openai"

conversation_history = PersistentListDict("conversation_history")  # {channel_id: [message objects]}
last_personality = PersistentDict("last_personality")              # {channel_id: personality_key}

# ─────────────────────────────────────────────────────────────
# Discord intents
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(
  command_prefix="!",
  intents=intents,)
#bot.voice_client_class=voice_recv.VoiceRecvClient


# ─────────────────────────────────────────────────────────────
# Load Opus for voice support
if not discord.opus.is_loaded():
    opus_path = os.getenv("OPUS_LIBRARY_PATH") or None
    for path in filter(None, [opus_path, None]):  # try env path first, then auto-detect
        try:
            discord.opus.load_opus(path)
            print(f"✔️ Opus library loaded ({path or 'auto-detected'})")
            break
        except Exception as e:
            print(f"⚠️ Opus load failed ({path or 'auto-detect'}): {e}")


# ─────────────────────────────────────────────────────────────
# File Janitor
async def remove_file_later(path, delay=0.5):
    await asyncio.sleep(delay)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
