import os
import re
import time
import wave
import uuid
import asyncio
import audioop
import tempfile

from discord.ext.voice_recv import AudioSink
from config import bot, chat_with_ai, conversation_history, last_personality
from personalities import PERSONALITY
from tts import synthesize_speech, tts_queue
from stt import speech_to_text
import billing

DEFAULT_PERSONALITY = "chadbot"

DISCORD_SAMPLE_RATE = 48000
AZURE_SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2        # 16-bit PCM
SILENCE_TIMEOUT = 1.5   # seconds of silence before processing the utterance

# Every personality is reachable by its own voice alias (set per-personality via the
# "voice_alias" field in personalities.py, e.g. chadbot's is "chad") — saying one
# switches the listener to that personality for the reply and keeps using it (like
# !chad does) until a different alias is spoken. The personality keys themselves (used
# by !chad and stored in last_personality) are often multi-word or invented
# ("weebmod3000", "sir_edrick") and Azure mangles those the same way it mangled
# "chadbot" -> "chatbot"/"Chadwick", so each alias is a short, plain, real word instead.
VOICE_WAKE_ALIASES = {
    key: data["voice_alias"] for key, data in PERSONALITY.items() if "voice_alias" in data
}

# \w* tolerates Azure padding a short/unusual word into a fuller-sounding one — the same
# drift that turned "chad" into "Chadwick". Longest aliases are checked first so a
# multi-word alias can't get shadowed by a shorter one. Trailing punctuation/whitespace
# is consumed too (e.g. "chad, how's it going" -> prompt is "how's it going").
# Everything after the match becomes the prompt; if nothing follows, the bot asks what
# they need.
_ALIASES_BY_LENGTH = sorted(VOICE_WAKE_ALIASES, key=lambda k: len(VOICE_WAKE_ALIASES[k]), reverse=True)
_PERSONALITY_NAME_RE = {
    key: re.compile(r'\b' + re.escape(VOICE_WAKE_ALIASES[key]).replace(r'\ ', r'\s+') + r'\w*\b', re.IGNORECASE)
    for key in _ALIASES_BY_LENGTH
}

WAKE_WORD_RE = re.compile(
    r'\b(?:' + '|'.join(
        re.escape(VOICE_WAKE_ALIASES[k]).replace(r'\ ', r'\s+') + r'\w*' for k in _ALIASES_BY_LENGTH
    ) + r')\b[\s,.:;!?-]*',
    re.IGNORECASE,
)


def _resolve_personality(transcript: str) -> str:
    """Returns the personality whose voice alias was spoken (always one, since
    WAKE_WORD_RE only matches when an alias is present), or DEFAULT_PERSONALITY."""
    for key, pattern in _PERSONALITY_NAME_RE.items():
        if pattern.search(transcript):
            return key
    return DEFAULT_PERSONALITY


class ChadListener(AudioSink):
    """Listens in a voice channel and responds when 'chad' or a personality's name is spoken."""

    def __init__(self, text_channel, vc):
        self.text_channel = text_channel
        self.vc = vc
        self._buffers = {}          # user_id -> bytearray of resampled mono PCM
        self._last_audio = {}       # user_id -> monotonic timestamp
        self._resample_state = {}   # user_id -> audioop ratecv state
        self._watcher = bot.loop.create_task(self._silence_watcher())

    def wants_opus(self):
        return False

    def write(self, user, data):
        if user is None or user.bot:
            return

        uid = user.id

        # Discord sends 48kHz stereo — convert to 16kHz mono for Azure STT
        mono = audioop.tomono(data.pcm, SAMPLE_WIDTH, 0.5, 0.5)
        state = self._resample_state.get(uid)
        resampled, new_state = audioop.ratecv(
            mono, SAMPLE_WIDTH, 1, DISCORD_SAMPLE_RATE, AZURE_SAMPLE_RATE, state
        )
        self._resample_state[uid] = new_state

        self._buffers.setdefault(uid, bytearray())
        self._buffers[uid] += resampled
        self._last_audio[uid] = time.monotonic()

    async def _silence_watcher(self):
        """Polls every 300ms; fires when a user has been silent long enough."""
        while True:
            await asyncio.sleep(0.3)
            now = time.monotonic()
            for uid in list(self._last_audio):
                if now - self._last_audio[uid] >= SILENCE_TIMEOUT and self._buffers.get(uid):
                    pcm = bytes(self._buffers.pop(uid))
                    self._last_audio.pop(uid)
                    # Note: _resample_state is intentionally left alone — resetting it
                    # here made the resampler start "cold" on the next utterance, which
                    # distorted whatever word was spoken first (often the wake word).
                    bot.loop.create_task(self._process(uid, pcm))

    async def _process(self, user_id, pcm_bytes):
        guild_id = self.text_channel.guild.id
        duration_seconds = len(pcm_bytes) / (AZURE_SAMPLE_RATE * SAMPLE_WIDTH)
        print(f"[Listener] Got {duration_seconds:.2f}s of audio from user {user_id}, transcribing...")

        if billing.stt_gate_message(guild_id, duration_seconds):
            print(f"[Listener] STT quota exceeded for guild {guild_id}, skipping utterance")
            return

        wav_path = await asyncio.to_thread(_write_wav, pcm_bytes)
        transcript = await asyncio.to_thread(speech_to_text, wav_path)
        billing.record_stt_usage(guild_id, duration_seconds)
        if not transcript:
            print(f"[Listener] STT returned nothing for user {user_id}")
            return

        # Only respond if the wake word is present
        match = WAKE_WORD_RE.search(transcript)
        print(f"[Listener] Heard from {user_id}: {transcript!r} (wake word matched: {bool(match)})")
        if not match:
            return

        # Everything after the wake word is the prompt; if nothing, ask what they need
        prompt = transcript[match.end():].strip()
        if not prompt:
            prompt = "What do you need?"

        member = self.text_channel.guild.get_member(user_id)
        display_name = member.display_name if member else str(user_id)
        await self.text_channel.send(f"{display_name}: *{transcript}*")

        cid = str(self.text_channel.id)

        # Whichever alias was spoken switches personality for this reply and onward
        previous_personality = last_personality.get(cid, DEFAULT_PERSONALITY)
        requested_personality = _resolve_personality(transcript)

        if requested_personality != DEFAULT_PERSONALITY and not billing.has_custom_personality_access(guild_id):
            if requested_personality != previous_personality:
                await self.text_channel.send(
                    "Custom personalities are a **Pro** plan perk — sticking with Chadbot for now."
                )
            personality_key = DEFAULT_PERSONALITY
        else:
            personality_key = requested_personality

        last_personality[cid] = personality_key
        personality = PERSONALITY[personality_key]
        system_prompt = {"role": "system", "content": personality["description"]}

        history = conversation_history.setdefault(cid, [])
        if previous_personality != personality_key:
            conversation_history[cid] = [system_prompt]
            history = conversation_history[cid]
        elif not history or history[0].get("role") != "system":
            history.insert(0, system_prompt)
        else:
            history[0] = system_prompt

        history.append({"role": "user", "content": prompt})

        try:
            reply, _ = await chat_with_ai(history, max_tokens=250, temperature=0.7)
        except Exception as e:
            print(f"[Listener] AI error: {e}")
            await self.text_channel.send("My brain broke for a sec, try again.")
            return

        await self.text_channel.send(f"**[{personality_key.capitalize()}]** {reply}")
        history.append({"role": "assistant", "content": reply})

        if len(history) > 30:
            conversation_history[cid] = [history[0]] + history[-30:]

        if self.vc and self.vc.is_connected():
            gate_msg = billing.tts_gate_message(guild_id, len(reply))
            if gate_msg:
                await self.text_channel.send(gate_msg)
            else:
                try:
                    audio_path = await asyncio.to_thread(
                        synthesize_speech, reply, personality["style"], personality["voice"]
                    )
                    billing.record_tts_usage(guild_id, len(reply))
                    await tts_queue.put((audio_path, self.vc))
                except Exception as e:
                    print(f"[Listener] TTS error: {e}")

    def cleanup(self):
        self._watcher.cancel()


def _write_wav(pcm_bytes: bytes) -> str:
    path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}.wav")
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(AZURE_SAMPLE_RATE)
        wf.writeframes(pcm_bytes)
    return path
