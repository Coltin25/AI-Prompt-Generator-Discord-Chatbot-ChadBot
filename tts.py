# synthesizeSpeech.py
import os
import uuid
import html
import tempfile
import asyncio
import discord
import azure.cognitiveservices.speech as speechsdk
##from google.cloud import texttospeech as google_tts

from config import (
    AZURE_SPEECH_KEY,
    AZURE_REGION,
    SUPPORTED_STYLES,
    FFMPEG_PATH,
    remove_file_later,
    bot,
)

# ─────────────────────────────────────────────────────────────
# Create a queue for TTS audio files
tts_queue: asyncio.Queue = asyncio.Queue()

async def tts_player():
    """Background task that drains tts_queue and plays audio via Discord voice."""
    while True:
        audio_path, vc = await tts_queue.get()
        print(f"[TTS] Got file: {audio_path}")

        if not vc or not vc.is_connected():
            print("[TTS] VoiceClient disconnected, skipping")
            tts_queue.task_done()
            continue

        done = asyncio.Event()

        def _after_playing(err):
            if err:
                print(f"[TTS] Playback error: {err}")
            asyncio.create_task(remove_file_later(audio_path))
            done.set()

        source = discord.FFmpegPCMAudio(
            executable=FFMPEG_PATH,
            source=audio_path
        )
        vc.play(source, after=lambda e: bot.loop.call_soon_threadsafe(_after_playing, e))
        print("[TTS] Playback started")
        await done.wait()
        print("[TTS] Playback finished")
        tts_queue.task_done()

@bot.event
async def on_ready():
    bot.loop.create_task(tts_player())
    print(f"Logged in as {bot.user}")

def synthesize_speech(
    text: str,
    style: str = "calm",
    voice: str = "en-US-DavisNeural"
) -> str:
    """Generate an SSML‐driven WAV file and return its filepath."""
    style = style if style in SUPPORTED_STYLES else "calm"
    speech_config = speechsdk.SpeechConfig(
        subscription=AZURE_SPEECH_KEY,
        region=AZURE_REGION
    )
    speech_config.speech_synthesis_voice_name = voice
    speech_config.set_speech_synthesis_output_format(
        speechsdk.SpeechSynthesisOutputFormat.Riff16Khz16BitMonoPcm
    )

    escaped = html.escape(text)
    ssml = f"""<speak version="1.0"
        xmlns="http://www.w3.org/2001/10/synthesis"
        xmlns:mstts="http://www.w3.org/2001/mstts"
        xml:lang="en-US">
      <voice name="{speech_config.speech_synthesis_voice_name}">
        <mstts:express-as style="{style}">
          <prosody pitch="+10%" rate="-5%">
            {escaped}
          </prosody>
        </mstts:express-as>
      </voice>
    </speak>"""

    print(f"[TTS SSML] style={style}\n{ssml}")

    temp_path = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}.wav")
    audio_config = speechsdk.audio.AudioOutputConfig(filename=temp_path)
    synthesizer = speechsdk.SpeechSynthesizer(
        speech_config=speech_config,
        audio_config=audio_config
    )

    result = synthesizer.speak_ssml_async(ssml).get()
    if result.reason == speechsdk.ResultReason.Canceled:
        details = result.cancellation_details.error_details
        raise Exception(f"Speech synthesis canceled: {details}")
    if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
        raise Exception(f"Speech synthesis failed: {result.reason}")

    return temp_path
