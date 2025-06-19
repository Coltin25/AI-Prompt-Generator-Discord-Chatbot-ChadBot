import os
import uuid
import discord
from openai import OpenAI
import tempfile
from discord.ext import commands
from dotenv import load_dotenv
import azure.cognitiveservices.speech as speechsdk
import discord.opus

# Load environment variables
load_dotenv()

# Validate required environment variables
required_env_vars = {
    "CHATBOT": "Discord bot token",
    "THICC_BOI": "Discord guild or user ID",
    "OPEN_AI_API": "OpenAI API key",
    "AZURE_TTS": "Azure TTS subscription key",
    "AZURE_ENDPOINT": "Azure region"
}

missing_keys = []

for key, description in required_env_vars.items():
    if not os.getenv(key):
        print(f"Missing environment variable: {key} ({description})")
        missing_keys.append(key)

if missing_keys:
    print(f"\n Your .env file is missing {len(missing_keys)} required key(s). Fix and restart.")
    exit(1)
else:
    print("All required environment variables are set.")

CHATBOT = os.getenv("CHATBOT")
THICC_BOI = os.getenv("THICC_BOI")
OPENAI_KEY = os.getenv("OPEN_AI_API")
AZURE_SPEECH_KEY = os.getenv("AZURE_TTS")
AZURE_REGION = os.getenv("AZURE_REGION")
THICCBOI = discord.Object(id=THICC_BOI)

client = OpenAI(api_key=OPENAI_KEY)

# Discord intents
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ─────────────────────────────────────────────────────────────
# Load Opus for voice support
if not discord.opus.is_loaded():
    discord.opus.load_opus(r'C:\ffmpeg\ffmpeg-7.1.1-essentials_build\bin\libopus.dll')
    print('✔️ Opus library loaded')
else:
    print('✔️ Opus already loaded')

# ─────────────────────────────────────────────────────────────
#Azure Speech Logic
def synthesize_speech(text: str) -> str:
    speech_config = speechsdk.SpeechConfig(subscription=AZURE_SPEECH_KEY, region=AZURE_REGION)
    speech_config.speech_synthesis_voice_name = "en-US-Neural"
    speech_config.set_speech_synthesis_output_format(
        speechsdk.SpeechSynthesisOutputFormat.Riff16Khz16BitMonoPcm
    )

    # Avoid locked temp file by writing directly to filename
    temp_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}.wav")
    audio_config = speechsdk.audio.AudioOutputConfig(filename=temp_path)
    synthesizer = speechsdk.SpeechSynthesizer(speech_config=speech_config, audio_config=audio_config)

    result = synthesizer.speak_text_async(text).get()
    if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
        raise Exception("Speech synthesis failed")

    return temp_path

# ─────────────────────────────────────────────────────────────
@bot.command()
async def join(ctx):
    if ctx.author.voice:
        channel = ctx.author.voice.channel
        await channel.connect()
        await ctx.send("Sup fuckers, I'm here.")
    else:
        await ctx.send("Yo wtf, where's the party at? Join a voice chat already.")

@bot.command()
async def leave(ctx):
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("I'm outta here.")
    else:
        await ctx.send("Bruh, I'm not even in the call.")

@bot.command()
async def chad(ctx, *, prompt):
    await ctx.send(f"Here is the deal: `{prompt}`")

    try:
        # ChatGPT response
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150
        )
        reply = response.choices[0].message.content.strip()
        await ctx.send(reply)

        # TTS Audio
        audio_path = synthesize_speech(reply)

        # Play it
        if ctx.voice_client and ctx.voice_client.is_connected():
            ctx.voice_client.play(
                discord.FFmpegPCMAudio(
                    executable=r"C:\ffmpeg\ffmpeg-7.1.1-essentials_build\bin\ffmpeg.exe",
                    source=audio_path
                ),
                after=lambda e: os.remove(audio_path)
            )
        else:
            await ctx.send("Use `!join` first so I can talk.")
    except Exception as e:
        await ctx.send("Something went wrong. Check logs.")
        print("Error in !chad command:", e)

# ─────────────────────────────────────────────────────────────
bot.run(CHATBOT)
