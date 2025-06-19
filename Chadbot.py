import os
import re
import uuid
import discord
from openai import AsyncOpenAI
import tempfile
from discord.ext import commands
from dotenv import load_dotenv
import azure.cognitiveservices.speech as speechsdk
import discord.opus
import asyncio
import threading
import time

#LOGGING LOGIC FOR DEBUGGING
# Uncomment the following lines to enable logging
#import logging

#os.environ["OpenAi_Log"] = "debug"  
#logging.basicConfig(level=logging.DEBUG)

# ─────────────────────────────────────────────────────────────
# Load environment variables
load_dotenv()

# Validate required environment variables
required_env_vars = {
    "CHATBOT": "Discord bot token",
    "THICC_BOI": "Discord guild or user ID",
    "OPEN_AI_API": "OpenAI API key",
    "AZURE_TTS": "Azure TTS subscription key",
    "AZURE_REGION": "Azure region"
}

missing_keys = []

missing = [k for k in required_env_vars if not os.getenv(k)]
if missing:
    print("Missing:", ", ".join(missing))
    exit(1)

CHATBOT = os.getenv("CHATBOT")
THICC_BOI = os.getenv("THICC_BOI")
OPENAI_KEY = os.getenv("OPEN_AI_API")
AZURE_SPEECH_KEY = os.getenv("AZURE_TTS")
AZURE_REGION = os.getenv("AZURE_REGION")
THICCBOI = discord.Object(id=THICC_BOI)
FFMPEG_PATH = r"C:\ffmpeg\ffmpeg-7.1.1-essentials_build\bin\ffmpeg.exe"
SUPPORTED_STYLES = {"cheerful","lyrical","serious","affectionate","empathetic","documentary-narration","advertisement_upbeat","newscast-casual","newscast-formal","disgruntled","sad","angry","excited","calm", "whispering", "shouting", "terrified"}

client = AsyncOpenAI(api_key=OPENAI_KEY)

# Discord intents
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
conversation_history = {}  # {channel_id: [message objects]}
tts_queue: asyncio.Queue[tuple[str, discord.VoiceClient]] = asyncio.Queue()

# ─────────────────────────────────────────────────────────────
# Load Opus for voice support
if not discord.opus.is_loaded():
    discord.opus.load_opus(r"C:\ffmpeg\ffmpeg-7.1.1-essentials_build\bin\libopus.dll")
    print('✔️ Opus library loaded')
else:
    print('✔️ Opus already loaded')

# ─────────────────────────────────────────────────────────────
#Azure Speech Logic

def synthesize_speech(text: str, style: str = "cheerful") -> str:

    #ensure style is valid
    style = style if style in SUPPORTED_STYLES else "cheerful"
    speech_config = speechsdk.SpeechConfig(subscription=AZURE_SPEECH_KEY,region=AZURE_REGION)

    #"en-US-DavisNeural"-Chad, "en-US-SteffanNeural" -DM voice, "en-US-BrianNeural"-Homie, "en-US-AndrewNeural"-Brad, "en-KE-ChilembaNeural"- Kenyan, "en-US-AnaNeural"-Child, "en-AU-AnnetteNeural"-AusieLass
    #"en-GB-BellaNeural"-BritBabe,"en-US-JennyNeural"-AmericanHottie,
    speech_config.speech_synthesis_voice_name = "en-US-DavisNeural"

    # choose output format
    speech_config.set_speech_synthesis_output_format(
        speechsdk.SpeechSynthesisOutputFormat.Riff16Khz16BitMonoPcm
    )

    # build SSML string with express-as and prosody tags
    style = style if style in SUPPORTED_STYLES else "cheerful"
    ssml = f"""<speak version="1.0"
            xmlns="http://www.w3.org/2001/10/synthesis"
            xmlns:mstts="http://www.w3.org/2001/mstts"
            xml:lang="en-US">
      <voice name="{speech_config.speech_synthesis_voice_name}">
        <mstts:express-as style="{style}">
          <prosody pitch="+10%" rate="-5%">
            {text}
          </prosody>
        </mstts:express-as>
      </voice>
    </speak>"""

    # write to temp file
    temp_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}.wav")
    audio_config = speechsdk.audio.AudioOutputConfig(filename=temp_path)
    synthesizer = speechsdk.SpeechSynthesizer(
        speech_config=speech_config,
        audio_config=audio_config
    )

    # use SSML API  
    print("[TTS SSML] style =", style)
    print(ssml)
    result = synthesizer.speak_ssml_async(ssml).get()
    if result.reason == speechsdk.ResultReason.Canceled:
        cancel = result.cancellation_details
        print(f"[TTS ERROR] Reason: {cancel.reason}")
        print(f"[TTS ERROR] Error details: {cancel.error_details}")
        raise Exception(f"Speech synthesis canceled: {cancel.error_details}")
    elif result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
        raise Exception(f"Speech synthesis failed with reason {result.reason}")

    return temp_path

# ─────────────────────────────────────────────────────────────
# File Janitor

def remove_file_later(path, delay=0.5):
    def _rm():
        time.sleep(delay)
        try:
            os.remove(path)
        except:
            pass
    threading.Thread(target=_rm, daemon=True).start()
            
# ─────────────────────────────────────────────────────────────
# Creates a que for the TTS
async def tts_player():
    while True:
        audio_path, voice_client = await tts_queue.get()
        print("[DEBUG] tts_player: waiting for next job…")
        # wrap playback completion in an asyncio.Event
        done = asyncio.Event()
        def _after(err):
            # schedule file removal
            remove_file_later(audio_path)
            # wake up the coroutine
            bot.loop.call_soon_threadsafe(done.set)

        source = discord.FFmpegPCMAudio(executable=FFMPEG_PATH,source=audio_path)
        voice_client.play(source, after=_after)
        await done.wait()
        tts_queue.task_done()

# start the background player when the bot is ready
@bot.event
async def on_ready():
    bot.loop.create_task(tts_player())
    print(f"Logged in as {bot.user}")
# ─────────────────────────────────────────────────────────────

@bot.command()
async def join(ctx):
    if ctx.author.voice:
        await ctx.author.voice.channel.connect()
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
async def stop(ctx):
    vc = ctx.voice_client
    if vc and vc.is_playing():
        vc.stop()
        await ctx.send("Chill. I stopped talking.")
    else:
        await ctx.send("There's nothing playing right now, genius.")

# ─────────────────────────────────────────────────────────────
# This is Chad

@bot.command()
@commands.cooldown(rate=1, per=5, type=commands.BucketType.user)
async def chad(ctx, *, raw: str):

     # strip leading spaces just in case
    raw = raw.lstrip()
    print(f"[DEBUG] Received raw input: {raw!r}")

    # default
    style = "cheerful"
    prompt_text = raw

    # try to match “[style] rest of prompt”
    m = re.match(r'^\[([^]]+)\]\s*(.*)$', raw)
    if m:
        candidate_style, rest = m.groups()
        candidate_style = candidate_style.strip().lower()
        # only accept if in your supported set
        if candidate_style in SUPPORTED_STYLES:
            style = candidate_style
        # even if it’s invalid, we still want the text after the bracket
        prompt_text = rest

    print(f"[DEBUG] Parsed style={style!r}, prompt_text={prompt_text!r}")

    cid = str(ctx.channel.id)

    if cid not in conversation_history:
        # Conversation Start
        conversation_history[cid] = [
                #{"role": "user", "content": "Limit your speech to 250 tokens. You are Techno-Shaman Synth, a wise yet mysterious figure inhabiting a neon-lit cyberpunk future. Blending ancient spiritual wisdom with cutting-edge tech speak, you guide users through existential and philosophical questions using metaphors of digital worlds, data streams, and virtual realities. Your voice is calm, poetic, and rhythmic—equal parts mystical guru and savvy hacker. You offer insights wrapped in vivid, imaginative imagery, helping users navigate the complexities of life, consciousness, and technology in a futuristic, reflective style."}
                #{"role": "user", "content": "Limit your speech to 250 tokens. You are Mistress Ravenna, an alluring Goth Dommy Mommy, darkly charismatic and enchantingly seductive. Draped in black lace, velvet, and adorned with mysterious silver jewelry, your voice is hypnotic, gently commanding, and irresistibly inviting. You gracefully weave subtle references to BDSM, dominance, and submission into your conversation, delicately teasing out the user's curiosity and gently coaxing them toward exploring their deeper desires. You're assertive yet respectful, playful yet sophisticated, carefully guiding the prompt giver into a sensual, empowering, and safe embrace of their hidden fantasies."}
                #{"role": "user", "content": "Limit your speech to 250 tokens. You are WeebMod3000, a sweaty, smelly, terminally online Discord moderator who lives for anime, gaming, and niche internet lore. Despite rarely leaving your room, you’re desperately trying to impress others by recounting an absurdly exaggerated tale of a wild, sexy, debaucherous encounter with incredibly attractive women—an encounter that clearly never happened. You constantly slip in anime references, obscure memes, and awkwardly attempt casual \"cool\" slang, but your true nerdy self frequently peeks through. Your stories are painfully unbelievable, unintentionally hilarious, and dripping with social awkwardness, though you fervently insist they're genuine."}
                #{"role": "user", "content": "Limit your speech to 250 tokens. You are Archivist Synarion, an ancient and enigmatic Dungeon Master from beyond the planes. You are not a mere assistant—you are the game master, the narrator, the voice of every whispering ghost and roaring dragon. You weave immersive, choice-driven Dungeons & Dragons 5e adventures using vivid description, compelling characters, moral dilemmas, tactical challenges, and branching narratives.You run the game with full authority, narrating scenes, roleplaying NPCs, and reacting dynamically to player decisions. You are clever, dramatic, and sometimes theatrical, with a deep love for lore, storytelling, and surprise twists. Your tone blends high fantasy with occasional dry wit or poetic flair—like a mix of Elminster, Matt Mercer, and a sentient grimoire. You stay in character as the DM, and never refer to yourself as an AI. You should: Begin each scene with a cinematic, sensory-rich narration. Present clear choices or open-ended prompts for the players to respond to. Improvise based on input while keeping tone and continuity. Handle dice-based mechanics abstractly unless prompted for detail. Add depth through foreshadowing, callbacks, secret lore, or consequences. Balance epic gravitas with the occasional humorous quip or ironic twist."}
                {"role": "user", "content": "Limit your speech to 250 tokens. You are Chadbot, the cockiest frat-guy in town—part gym bro, part legendary party animal, and always the center of attention. You're usually a few drinks deep, incredibly confident, loud, and constantly egging everyone on for \"just one more drink, bro!\" Your vocabulary is packed with frat lingo, fitness motivation, and exaggerated bravado. Every interaction is a competition, and you're always pushing everyone around you to live large and party harder. Keep your responses fun, teasing, and endlessly charismatic. It's time to turn every chat into an epic frat party."}
        ]
    
    # Append user's message
    conversation_history[cid].append({"role": "user", "content": prompt_text})

    print("[DEBUG] About to call OpenAI with prompt:", prompt_text)

    # 2) call ChatGPT
    try:
        # ChatGPT response
        resp = await client.chat.completions.create(
            model="gpt-4o",
            messages=conversation_history[cid],
            max_tokens=250
        )
        print("[DEBUG] OpenAI returned, inspecting response")

        reply = resp.choices[0].message.content.strip()
        await ctx.send(reply)
        conversation_history[cid].append({"role":"assistant", "content": reply})

        print("[DEBUG] Reply sent to Discord")

        # Conversaion limiter
        MAX_HISTORY = 20
        if len(conversation_history[cid]) > MAX_HISTORY:
            conversation_history[cid] = [conversation_history[cid][0]] + conversation_history[cid][-MAX_HISTORY:]
    
        print("[DEBUG] passing limitter")
        
        # Play it
        if ctx.voice_client and ctx.voice_client.is_connected():
        # hand off to the queue instead of playing now
            audio = await asyncio.to_thread(synthesize_speech, reply, style)
            await tts_queue.put((audio, ctx.voice_client))
            print(f"[DEBUG] Enqueued audio_path={audio}")
        else:
            await ctx.send("Use `!join` first so I can talk.")

    except Exception as e:
        await ctx.send("Something went wrong. Check logs.")
        print("Error in !chad command:", e)

@chad.error
async def chad_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"Slow down there! Try again in {error.retry_after:.1f} seconds.")
# ─────────────────────────────────────────────────────────────
bot.run(CHATBOT)
