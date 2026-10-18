"""Check the voice agent end to end, without the phone: a fake phone joins a fresh LiveKit room with a camera and a
mic, a fake backend ("vision") sends the tracker's messages (a mug in the middle of the view, its quick label and
its Look's prices), the voice agent is dispatched, and the phone asks its question (spoken by Gemini's text to
speech, or a 16-bit mono WAV). Prints what comes back: the agent's transcript and how much it spoke. Its side
is in the worker's log.

    python scripts/voice_check.py [question.wav]    # from backend/; needs a running worker (voice/agent.py), and
                                                    # GOOGLE_API_KEY without a WAV
"""
import asyncio
import base64
import io
import json
import os
import sys
import time
import uuid
import wave
from pathlib import Path

import httpx
import numpy as np
from livekit import api, rtc

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storefront import config  # noqa: E402
from storefront.rtc import tokens  # noqa: E402

FPS = 10
W, H = 640, 480
REPLY_WAIT_S = 30.0
QUESTION = "What is that thing in front of me, and how much does it cost?"
TTS_MODEL = "gemini-3.8-flash-tts"

MUG = {"uid": "1.1", "prompt": "mug", "score": 0.9, "box": [0.40, 0.35, 0.60, 0.65], "in_view": True,
       "last_seen_ts": 0}
PRODUCT = {"title": "Stoneware Mug 16oz", "store": "Target", "price": 11.99, "currency": "USD",
           "url": "https://example.com", "image_url": None, "source": "shopping", "tier": "new", "source_rank": 0,
           "match_score": 88, "savings_pct": None, "match_reason": None}
MESSAGES = [
    {"type": "status", "tracking": "running", "prompts": ["mug"]},
    {"type": "objects", "session": 1, "age_ms": 300, "objects": [MUG]},
    {"type": "label", "uid": "1.1", "box": MUG["box"], "name": "white ceramic mug", "price_low": 8, "price_high": 15},
    {"type": "look", "uid": "1.1", "look_id": "l_1.1", "stage": "results", "label": "mug", "box": MUG["box"],
     "selected": False, "crop_url": None, "message": None,
     "item": {"category": "coffee mug", "description": "Glossy white stoneware."},
     "search": {"capture_id": "l_1", "spoken_summary": "Target has it for about 12 dollars.", "best_match": PRODUCT,
                "dupes": [], "used": [], "price_insight": {"verdict_text": "That's a fair price."},
                "timing_ms": {"total": 2400}, "item": None}},
]


def log(msg: str):
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


async def camera(source: rtc.VideoSource, stop: asyncio.Event):
    """A grey frame with a white square (the "mug") in the middle, FPS times a second."""
    frame = np.full((H, W, 4), 90, dtype=np.uint8)
    frame[H // 3:2 * H // 3, 2 * W // 5:3 * W // 5] = 255
    data = frame.tobytes()
    while not stop.is_set():
        source.capture_frame(rtc.VideoFrame(W, H, rtc.VideoBufferType.RGBA, data))
        await asyncio.sleep(1 / FPS)


async def mic(source: rtc.AudioSource, speech: np.ndarray, speak: asyncio.Event, stop: asyncio.Event):
    """Silence, the speech once speak is set, then silence again, in 10 ms frames."""
    n = source.sample_rate // 100
    pending = None
    while not stop.is_set():
        if speak.is_set() and pending is None:
            pending = speech
            log("phone: speaking")
        if pending is not None and len(pending):
            chunk, pending = pending[:n], pending[n:]
            chunk = np.pad(chunk, (0, n - len(chunk)))
        else:
            chunk = np.zeros(n, dtype=np.int16)
        await source.capture_frame(rtc.AudioFrame(chunk.tobytes(), source.sample_rate, 1, n))


def read_wav(f) -> tuple[int, np.ndarray]:
    with wave.open(f) as w:
        assert w.getsampwidth() == 2 and w.getnchannels() == 1, "the WAV must be 16-bit mono"
        return w.getframerate(), np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def say(text: str) -> tuple[int, np.ndarray]:
    """text, spoken by Gemini's text to speech: a WAV, or raw 24 kHz PCM."""
    r = httpx.post(f"https://generativelanguage.googleapis.com/v1beta/models/{TTS_MODEL}:generateContent",
                   headers={"x-goog-api-key": os.environ["GOOGLE_API_KEY"]}, timeout=60,
                   json={"contents": [{"parts": [{"text": text}]}],
                         "generationConfig": {"responseModalities": ["AUDIO"]}})
    r.raise_for_status()
    audio = r.json()["candidates"][0]["content"]["parts"][0]["inlineData"]
    data = base64.b64decode(audio["data"])
    if audio["mimeType"] == "audio/wav":
        return read_wav(io.BytesIO(data))
    return 24000, np.frombuffer(data, dtype=np.int16)


async def main(wav_path: str | None) -> int:
    rate, speech = read_wav(wav_path) if wav_path else say(QUESTION)

    room_name = f"voice-check-{uuid.uuid4().hex[:6]}"
    stop, speak = asyncio.Event(), asyncio.Event()
    heard = {"agent_audio_s": 0.0, "transcript": []}
    agent_joined = asyncio.Event()
    tasks: set[asyncio.Task] = set()

    vision, phone = rtc.Room(), rtc.Room()
    await vision.connect(config.LIVEKIT_URL, tokens.vision(room_name))

    async def agent_audio(track: rtc.Track):
        async for e in rtc.AudioStream(track):
            f = e.frame
            if np.abs(np.frombuffer(f.data, dtype=np.int16)).max(initial=0) > 500:
                heard["agent_audio_s"] += f.samples_per_channel / f.sample_rate

    @phone.on("participant_connected")
    def on_join(p: rtc.RemoteParticipant):
        log(f"room: {p.identity} joined")
        if p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
            agent_joined.set()

    @phone.on("track_subscribed")
    def on_track(track: rtc.Track, pub, p: rtc.RemoteParticipant):
        if track.kind == rtc.TrackKind.KIND_AUDIO and p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
            log(f"phone: hearing {p.identity}")
            tasks.add(asyncio.create_task(agent_audio(track)))

    async def read_transcript(reader: rtc.TextStreamReader, identity: str):
        text = await reader.read_all()
        if text.strip():
            heard["transcript"].append((identity, text))
            log(f"transcript ({identity}): {text}")

    phone.register_text_stream_handler(
        "lk.transcription", lambda r, who: tasks.add(asyncio.create_task(read_transcript(r, who))))

    await phone.connect(config.LIVEKIT_URL, tokens.phone(room_name, "voicecheck"))
    video, audio = rtc.VideoSource(W, H), rtc.AudioSource(rate, 1)
    await phone.local_participant.publish_track(rtc.LocalVideoTrack.create_video_track("camera", video),
                                                rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_CAMERA))
    await phone.local_participant.publish_track(rtc.LocalAudioTrack.create_audio_track("mic", audio),
                                                rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
    tasks |= {asyncio.create_task(camera(video, stop)), asyncio.create_task(mic(audio, speech, speak, stop))}
    log(f"room {room_name}: phone publishing camera {W}x{H}@{FPS} and mic ({len(speech) / rate:.1f}s of speech)")

    async def tracker():
        while not stop.is_set():
            for m in MESSAGES:
                await vision.local_participant.publish_data(json.dumps(m), topic=tokens.UPDATES)
            await asyncio.sleep(0.5)
    tasks.add(asyncio.create_task(tracker()))

    lk = api.LiveKitAPI(config.LIVEKIT_URL, config.LIVEKIT_API_KEY, config.LIVEKIT_API_SECRET)
    dispatch = await lk.agent_dispatch.create_dispatch(
        api.CreateAgentDispatchRequest(agent_name=config.VOICE_AGENT, room=room_name))
    log(f"dispatched {config.VOICE_AGENT} ({dispatch.id})")
    try:
        await asyncio.wait_for(agent_joined.wait(), 20)
        await asyncio.sleep(4)  # its session starts once it sees the phone
        speak.set()
        await asyncio.sleep(len(speech) / rate + REPLY_WAIT_S)
    except TimeoutError:
        log("FAIL: the agent never joined (is the worker running?)")
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await lk.agent_dispatch.delete_dispatch(dispatch.id, room_name)
        await lk.aclose()
        await phone.disconnect()
        await vision.disconnect()

    agent_said = [t for who, t in heard["transcript"] if not who.startswith(tokens.PHONE)]
    log(f"agent spoke {heard['agent_audio_s']:.1f}s; transcript: {agent_said or 'none'}")
    ok = agent_joined.is_set() and heard["agent_audio_s"] > 0.5
    log("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None)))
