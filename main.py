import os
import io
import re
import random
import asyncio
import discord
import aiohttp
from discord.ext import commands
from tts import synthesize_speech, tts_queue
from personalities import PERSONALITY
from config import (CHATBOT, SUPPORTED_STYLES, DEFAULT_VOICE, VOICE_LIST, conversation_history, last_personality, bot, chat_with_ai, openai_client, HOME_GUILD_ID,)
from discord.ext.voice_recv import VoiceRecvClient
from listener import ChadListener, DEFAULT_PERSONALITY
import billing
import voice_patches
import logging
# ─────────────────────────────────────────────────────────────

voice_patches.apply()

# Chadbot - A Discord bot that channels the spirit of a frat bro
# This bot uses OpenAI's GPT-4o for text generation and Azure TTS for voice synthesis.

#LOGGING LOGIC FOR DEBUGGING
# Uncomment the following lines to enable logging

_log_level = os.getenv("LOG_LEVEL", "WARNING").upper()
logging.basicConfig(level=getattr(logging, _log_level, logging.WARNING))

async def safe_connect(channel: discord.VoiceChannel, cls=None) -> discord.VoiceClient:
    """Try to connect up to 3 times, handling invalidated sessions and timeouts."""
    kwargs = {"timeout": 20, "reconnect": True}
    if cls is not None:
        kwargs["cls"] = cls

    for attempt in range(3):
        try:
            await asyncio.sleep(2 ** attempt)
            return await channel.connect(**kwargs)
        except asyncio.TimeoutError:
            print(f"[safe_connect] Voice connect timed out (attempt {attempt + 1}/3), retrying...")
            await asyncio.sleep(2)
            continue
        except discord.errors.ConnectionClosed as e:
            if e.code == 4006:
                print("[safe_connect] Invalid session (4006), retrying...")
                await asyncio.sleep(2)
                continue
            raise

    raise RuntimeError("Failed to join voice after retries")

@bot.command()
async def join(ctx):
    if not ctx.author.voice:
        return await ctx.send("Join a voice channel first, bro.")

    gate_msg = billing.voice_access_message(ctx.guild.id)
    if gate_msg:
        return await ctx.send(gate_msg)

    try:
        # Disconnect cleanly if already in a channel
        if ctx.voice_client:
            await ctx.voice_client.disconnect()

        vc = await safe_connect(ctx.author.voice.channel, cls=VoiceRecvClient)
        vc.listen(ChadListener(ctx.channel, vc))
        await ctx.send("Sup fuckers, I'm here. Say **chad** and I'll answer.")
    except Exception as e:
        print("Join error:", repr(e))
        try:
            await ctx.send(f"Couldn't join VC right now: {type(e).__name__}")
        except:
            pass

# ─────────────────────────────────────────────────────────────
# Uses ChatGPT to generate a response in bot style
@bot.command()
async def chat(ctx, *, raw: str):
    raw = raw.lstrip()
    # Look for [personality] at the start
    m = re.match(r'^\[([^\]]+)\]\s*(.*)$', raw)
    if m:
        candidate = m.group(1).strip().lower()
        prompt_text = m.group(2)
        if candidate in PERSONALITY:
            personality_key = candidate
        else:
            personality_key = random.choice(list(PERSONALITY.keys()))
    else:
        personality_key = random.choice(list(PERSONALITY.keys()))
        prompt_text = raw

    system_prompt = {
    "role": "system",
    "content": PERSONALITY[personality_key]["description"]
    }
    
    cid = str(ctx.channel.id)
    if cid not in conversation_history:
        conversation_history[cid] = []
    
    # Insert/replace the first message if it's not already a system one
    if not conversation_history[cid] or conversation_history[cid][0]["role"] != "system":
        conversation_history[cid].insert(0, system_prompt)

    conversation_history[cid].append({"role": "user", "content": prompt_text})

    try:
        reply, provider = await chat_with_ai(
            conversation_history[cid],
            max_tokens=250,
            temperature=0.7,
        )
        print(f"{provider}")
        await ctx.send(f"**[{personality_key.capitalize()}]** {reply}")
        conversation_history[cid].append({"role": "assistant", "content": reply})

        # Conversation limiter
        MAX_HISTORY = 30
        if len(conversation_history[cid]) > MAX_HISTORY:
            conversation_history[cid] = [conversation_history[cid][0]] + conversation_history[cid][-MAX_HISTORY:]

    except Exception as e:
        await ctx.send("Something went wrong. Check logs.")
        print("Error:", e)

# ─────────────────────────────────────────────────────────────
# Uses ChatGPT to generate an Image
@bot.command(name='image', help='Generate an image based on a prompt')
async def image(ctx, *, prompt: str):

    # Generate an image using OpenAI's DALL-E
    # await ctx.trigger_typing()  # Show typing indicator

    if not prompt:
        await ctx.send("Please provide a prompt for the image.")
        return
    if len(prompt) > 1000:
        await ctx.send("Prompt is too long! Please keep it under 1000 characters.")
        return
    
    try:
        # Image generation
        await ctx.send("I'm workin on it, lemme grab my beer.")
        
        img_response = await openai_client.images.generate(
            model="dall-e-3",
            prompt=prompt,
            n=1,
            size="1024x1024"
        )
        image_url = img_response.data[0].url

        async with aiohttp.ClientSession() as session:
            async with session.get(image_url) as resp:
                if resp.status != 200:
                    return await ctx.send("Failed to download image.")
                data = await resp.read()

        file = discord.File(io.BytesIO(data), filename="generated.png")
        await ctx.send(file=file)

    except Exception as e:
    # Try to extract a more specific OpenAI error message, if any
        err_msg = getattr(e, "error", None)
        if hasattr(err_msg, "message") and err_msg.message:
            detail = err_msg.message
        else:
            detail = str(e) or "Unknown error"

        print("Error generating image:", detail)
        await ctx.send(f"Sorry man Im too much of a bitch to paint something like that.")



# ─────────────────────────────────────────────────────────────
# Uses Azure TTS to say the text in the channel
@bot.command()
async def say(ctx, *, raw: str):
    """Speak the given text via Azure TTS in VC (auto-join if needed)."""
    raw = raw.lstrip()

    # ——— 0) Defaults: pick a random personality so style/voice are always set ———
    base_key = random.choice(list(PERSONALITY.keys()))
    base = PERSONALITY[base_key]
    style = base["style"]
    voice = base["voice"]
    prompt = raw

    # ——— 1) Look for a leading [token] ———
    m = re.match(r'^\[([^]]+)\]\s*(.*)$', raw)
    if m:
        token, rest = m.groups()
        prompt = rest.lstrip()
        key = token.strip().lower()

        if key in PERSONALITY:
            # User explicitly asked for that personality
            p = PERSONALITY[key]
            style = p["style"]
            voice = p["voice"]

        else:
            # Treat as a style or style:voice override
            voice_candidate = None
            parts = token.split(":", 1)
            if len(parts) == 2:
                style_candidate, voice_candidate = parts
                style = style_candidate.lower()
            else:
                # could be either style or voice
                candidate = parts[0].lower()
                if candidate in SUPPORTED_STYLES:
                    style = candidate
                else:
                    voice_candidate = parts[0]

            if voice_candidate and voice_candidate in VOICE_LIST:
                voice = voice_candidate
            elif voice_candidate:
                await ctx.send(
                    f"Voice `{voice_candidate}` not supported — using `{voice}`."
                )


    # ——— 2) Gate the TTS call ———
    gate_msg = billing.tts_gate_message(ctx.guild.id, len(prompt))
    if gate_msg:
        return await ctx.send(gate_msg)

    # ——— 3) Ensure we’re connected to VC ———
    vc = ctx.voice_client
    if not vc or not vc.is_connected():
        if not ctx.author.voice:
            return await ctx.send("Join a voice channel first, bro.")
        try:
            vc = await safe_connect(ctx.author.voice.channel)
        except Exception as e:
            return await ctx.send(f"Can't join VC: {e}")

    # ——— 4) Synthesize & enqueue (with retry on unsupported-voice) ———
    try:
        audio = await asyncio.to_thread(
            synthesize_speech, prompt, style, voice
        )
    except Exception as e:
        msg = str(e)
        if "Unsupported voice" in msg:
            logging.warning(f"Voice `{voice}` unsupported; retrying default.")
            voice = DEFAULT_VOICE
            audio = await asyncio.to_thread(
                synthesize_speech, prompt, style, voice
            )
            await ctx.send(
                f"Retrying with default voice `{voice}`."
            )
        else:
            # bubble up unexpected errors
            raise

    billing.record_tts_usage(ctx.guild.id, len(prompt))
    await tts_queue.put((audio, vc))
    await ctx.send(
        f"Speaking in style `{style}` with voice `{voice}`…"
    )


# ─────────────────────────────────────────────────────────────
@bot.command()
async def pingchad(ctx):
    # Check if the bot is alive
    await ctx.send("Pong! I'm alive and kicking!")



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
    raw = raw.lstrip()

    # ——— 0) Defaults: random personality so style/voice are always defined ———
    custom_personality_ok = billing.has_custom_personality_access(ctx.guild.id)
    base_key = random.choice(list(PERSONALITY.keys())) if custom_personality_ok else DEFAULT_PERSONALITY
    p = PERSONALITY[base_key]
    default_style = p["style"]
    default_voice = p["voice"]
    style = None
    voice = None
    prompt_text = raw
    personality_key = base_key

    # ——— 1) Look for leading [token] ———
    m = re.match(r'^\[([^]]+)\]\s*(.*)$', raw)
    if m:
        token, rest = m.groups()
        prompt_text = rest.lstrip()
        key = token.strip().lower()

        if key in PERSONALITY:
            if key != DEFAULT_PERSONALITY and not custom_personality_ok:
                await ctx.send("Custom personalities are a **Pro** plan perk — sticking with Chadbot for now. Check `!plan` or `!upgrade`.")
            else:
                # explicit personality chosen
                personality_key = key
                p = PERSONALITY[personality_key]
                default_style = p["style"]
                default_voice = p["voice"]
        else:
            # style/voice override
            parts = token.split(":", 1)
            if len(parts) == 2:
                # [style:voice]
                style_candidate = parts[0].lower()
                voice_candidate = parts[1]
                if style_candidate in SUPPORTED_STYLES:
                    style = style_candidate
                if voice_candidate in VOICE_LIST:
                    voice = voice_candidate
                else:
                    await ctx.send(f"Voice `{voice_candidate}` not supported—using default.")
            else:
                candidate = parts[0].lower()
                if candidate in SUPPORTED_STYLES:
                    style = candidate
                elif candidate in VOICE_LIST:
                    voice = candidate
                else:
                    # neither a style nor a voice, ignore
                    pass

    # ——— 2) Finalize style/voice (use defaults if no override) ———
    style = style or default_style
    voice = voice or default_voice

    # ——— 2.5) Remember last personality for !chad_go_on ———
    cid = str(ctx.channel.id)
    previous_personality = last_personality.get(cid)
    last_personality[cid] = personality_key

    # ——— 3) Prepare the system prompt and conversation history ———
    system_prompt = {
        "role": "system",
        "content": PERSONALITY[personality_key]["description"]
    }
    if cid not in conversation_history:
        conversation_history[cid] = []

    # When personality changes, clear old history so prior messages don't bleed into the new character
    if previous_personality != personality_key:
        conversation_history[cid] = [system_prompt]
    elif not conversation_history[cid] or conversation_history[cid][0].get("role") != "system":
        conversation_history[cid] = [system_prompt] + conversation_history[cid]
    else:
        conversation_history[cid][0] = system_prompt

    conversation_history[cid].append({"role": "user", "content": prompt_text})
    
    # ——— 4) Call the OpenAI API ———
    try:
        reply, provider = await chat_with_ai(
            conversation_history[cid],
            max_tokens=250,
            temperature=0.7,
        )
        print(f"{provider}")
        await ctx.send(f"**[{personality_key.capitalize()}]** {reply}")
        conversation_history[cid].append({"role": "assistant", "content": reply})

        # prune history
        if len(conversation_history[cid]) > 30:
            conversation_history[cid] = [conversation_history[cid][0]] + conversation_history[cid][-30:]

        # ——— 5) TTS if connected ———
        vc = ctx.voice_client
        if vc and vc.is_connected():
            gate_msg = billing.tts_gate_message(ctx.guild.id, len(reply))
            if gate_msg:
                await ctx.send(gate_msg)
            else:
                # same retry‐on‐unsupported logic as !say
                try:
                    audio = await asyncio.to_thread(synthesize_speech, reply, style, voice)
                except Exception as e:
                    if "Unsupported voice" in str(e):
                        voice = DEFAULT_VOICE
                        audio = await asyncio.to_thread(synthesize_speech, reply, style, voice)
                        await ctx.send(f"Falling back to default voice `{voice}`.")
                    else:
                        raise
                billing.record_tts_usage(ctx.guild.id, len(reply))
                await tts_queue.put((audio, vc))
        else:
            await ctx.send("Use `!join` first so I can talk.")

    except Exception:
        await ctx.send("Something went wrong—check the logs.")


@chad.error
async def chad_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"Slow down there! Try again in {error.retry_after:.1f} seconds.")
# ─────────────────────────────────────────────────────────────
@bot.command()
async def personalities(ctx):
    keys = ', '.join(PERSONALITY.keys())
    await ctx.send(f"Available personalities: {keys}")
# ─────────────────────────────────────────────────────────────
# Continue a conversation with Chadbot
@bot.command(name="chad_go_on")
@commands.cooldown(rate=1, per=5, type=commands.BucketType.user)
async def chad_go_on(ctx):
    cid = str(ctx.channel.id)

    # If we never ran a !chad in this channel, bail.
    if cid not in last_personality or cid not in conversation_history or len(conversation_history[cid]) < 2:
        return await ctx.send(
            "No previous conversation to continue, bro. Start with `!chad [personality] your prompt`."
        )

    # 1) Grab the exact last personality key
    personality_key = last_personality[cid]
    personality = PERSONALITY[personality_key]
    style = personality["style"]
    voice = personality["voice"]

    # 2) Ensure system prompt is correct
    system_prompt = {
        "role": "system",
        "content": PERSONALITY[personality_key]["description"]
    }
    history = conversation_history[cid]

    if not history or history[0].get("role") != "system":
        history.insert(0, system_prompt)
    else:
        history[0] = system_prompt

    history.append({"role": "user", "content": "Go on with the conversation."})

    try:
        # 3) Continue the chat
        reply, provider = await chat_with_ai(
            conversation_history[cid],
            max_tokens=250,
            temperature=0.7,
        )
        print(f"{provider}")

        # 4) Send text reply
        await ctx.send(f"**[{personality_key.capitalize()}]** {reply}")

        # 5) Append to history
        history.append({"role": "assistant", "content": reply})
        
        if len(history) > 30:
            conversation_history[cid] = [history[0]] + history[-30:]

        # 6) TTS if possible
        vc = ctx.voice_client
        if vc and vc.is_connected():
            gate_msg = billing.tts_gate_message(ctx.guild.id, len(reply))
            if gate_msg:
                await ctx.send(gate_msg)
            else:
                try:
                    audio = await asyncio.to_thread(synthesize_speech, reply, style, voice)
                except Exception as e:
                    if "Unsupported voice" in str(e):
                        voice = DEFAULT_VOICE
                        audio = await asyncio.to_thread(synthesize_speech, reply, style, voice)
                        await ctx.send(f"Falling back to default voice `{voice}`.")
                    else:
                        raise
                billing.record_tts_usage(ctx.guild.id, len(reply))
                await tts_queue.put((audio, vc))
        else:
            await ctx.send("Use `!join` first so I can talk.")

    except Exception as e:
        await ctx.send("Something went wrong—check the logs.")
        print("Error:", e)

# ─────────────────────────────────────────────────────────────
# Subscription tier + usage
@bot.command(name="plan", help="Show this server's subscription tier and usage")
async def plan(ctx):
    await ctx.send(billing.format_plan(ctx.guild.id))

@bot.command(name="upgrade", help="Upgrade this server's plan")
async def upgrade(ctx):
    await ctx.send(
        "Upgrades aren't live yet, bro — Stripe checkout link coming soon. "
        "Hit up the dev if you can't wait."
    )

@bot.command(name="settier", help="Owner only: manually set a server's subscription tier")
@commands.is_owner()
async def settier(ctx, tier: str, guild_id: int = None):
    if not ctx.guild or ctx.guild.id != HOME_GUILD_ID:
        return await ctx.send("This command only works from the home server.")

    tier = tier.lower()
    if tier not in billing.TIER_LIMITS:
        return await ctx.send(f"Unknown tier `{tier}`. Pick from: {', '.join(billing.TIER_LIMITS)}")

    target_guild_id = guild_id or ctx.guild.id
    billing.set_tier(target_guild_id, tier)
    await ctx.send(f"Set guild `{target_guild_id}` to **{tier.capitalize()}**.")

@settier.error
async def settier_error(ctx, error):
    if isinstance(error, commands.NotOwner):
        await ctx.send("Nice try. This one's for the dev only.")

bot.run(CHATBOT)