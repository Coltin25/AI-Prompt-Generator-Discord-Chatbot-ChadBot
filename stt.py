import os
import asyncio
from config import bot, chat_with_ai, conversation_history, AZURE_SPEECH_KEY, AZURE_REGION, remove_file_later
from tts import synthesize_speech, tts_queue
from personalities import PERSONALITY
import billing
import azure.cognitiveservices.speech as speechsdk

# Every personality's voice alias (see personalities.py / listener.py) — biasing the
# recognizer toward these short, real words makes wake-word detection noticeably more
# reliable than letting Azure guess.
WAKE_WORD_PHRASES = [
    data["voice_alias"] for data in PERSONALITY.values() if "voice_alias" in data
]


# ─────────────────────────────────────────────────────────────         
# Azure speech to text from discord audio
def speech_to_text(wave_path: str) -> str:
    speech_config = speechsdk.SpeechConfig(subscription=AZURE_SPEECH_KEY, region=AZURE_REGION)

    audio_config = speechsdk.audio.AudioConfig(filename=wave_path)
    recognizer = speechsdk.SpeechRecognizer(speech_config=speech_config, audio_config=audio_config)

    # Bias the recognizer toward our wake words for more reliable detection.
    phrase_list = speechsdk.PhraseListGrammar.from_recognizer(recognizer)
    for phrase in WAKE_WORD_PHRASES:
        phrase_list.addPhrase(phrase)

    result = recognizer.recognize_once_async().get()
    if result.reason == speechsdk.ResultReason.RecognizedSpeech:
        return result.text
    else:
        if result.reason == speechsdk.ResultReason.Canceled:
            print("[STT] Canceled:", result.cancellation_details.error_details)
        return None

@bot.command(name="stt", help="Record you speech, transcribe, and respond via GPT")
async def stt(ctx, seconds: float = 5.0):
    try:
        from discord.ext.voice_recv import VoiceRecvClient, PCMRecorder
    except ImportError:
        return await ctx.send("STT recording isn't supported in this version of the library.")

    if not ctx.author.voice:
        return await ctx.send("Where are you? I need you hear you.")

    gate_msg = billing.stt_gate_message(ctx.guild.id, seconds)
    if gate_msg:
        return await ctx.send(gate_msg)

    vc = ctx.voice_client
    if vc is None:
        vc = await ctx.author.voice.channel.connect(cls=VoiceRecvClient,
            self_deaf=False,
            reconnect=False
        )

    print("VOICE CLIENT CLASS:", vc.__class__.__name__)

    recorder = PCMRecorder(sample_rate=16000, channels=1)
    vc.listen(recorder)

    await ctx.send(f"Im listening for {seconds:.1f} seconds. Speak up!")
    await asyncio.sleep(seconds)

    vc.stop_listening()
    print("Stopped listening, exporting wav...")
    wav_path = recorder.export_wav()
    print(f"WAV path: {wav_path}, exists: {os.path.exists(wav_path)}")
    await ctx.send("Workin my magic")

    transcript = await asyncio.to_thread(speech_to_text, wav_path)
    billing.record_stt_usage(ctx.guild.id, seconds)
    await remove_file_later(wav_path)

    if not transcript:
        return await ctx.send("Couldn't understand you, bro. Speak clearer next time.")

    await ctx.send(f"I heard: '{transcript}'")

    cid = str(ctx.channel.id)
    conversation_history.setdefault(cid, []).append({"role": "user", "content": transcript})
    conversation = conversation_history[cid]

    reply, _ = await chat_with_ai(conversation, max_tokens=150, temperature=0.7)
    await ctx.send(reply)
    conversation.append({"role": "assistant", "content": reply})

    if vc.is_connected():
        gate_msg = billing.tts_gate_message(ctx.guild.id, len(reply))
        if gate_msg:
            await ctx.send(gate_msg)
        else:
            audio = await asyncio.to_thread(synthesize_speech, reply)
            billing.record_tts_usage(ctx.guild.id, len(reply))
            await tts_queue.put((audio, vc))

    MAX = 20
    if len(conversation) > MAX:
        conversation_history[cid] = [conversation[0]] + conversation[-MAX:]