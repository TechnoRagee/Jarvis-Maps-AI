"""
voice_input.py
--------------
Voice input module for Jarvis Maps Automation.
Uses SpeechRecognition + sounddevice (no PyAudio needed).
Supports English + Hindi, press-to-talk + always listening (wake word: "Jarvis").
"""

import io
import time
import threading
import numpy as np
import sounddevice as sd
import speech_recognition as sr
from scipy.io.wavfile import write as wav_write


# ─────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────

SAMPLE_RATE   = 16000   # Hz — optimal for speech recognition
CHANNELS      = 1       # Mono
RECORD_SECS   = 5       # Max seconds for press-to-talk
WAKE_WORD     = "jarvis"
LANGUAGES     = ["en-IN", "hi-IN"]   # English (India) + Hindi


# ─────────────────────────────────────────────
#  CORE: RECORD AUDIO VIA SOUNDDEVICE
# ─────────────────────────────────────────────

def record_audio(duration: int = RECORD_SECS) -> sr.AudioData:
    """
    Records audio from microphone using sounddevice.
    Returns SpeechRecognition AudioData object.
    """
    print(f"   🎤 Recording for {duration} seconds...")
    recording = sd.rec(
        int(duration * SAMPLE_RATE),
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="int16"
    )
    sd.wait()  # Wait until recording is done

    # Convert numpy array → WAV bytes → AudioData
    wav_buffer = io.BytesIO()
    wav_write(wav_buffer, SAMPLE_RATE, recording)
    wav_buffer.seek(0)

    recognizer = sr.Recognizer()
    recognizer.energy_threshold = 300
    recognizer.dynamic_energy_threshold = True
    with sr.AudioFile(wav_buffer) as source:
        recognizer.adjust_for_ambient_noise(source, duration=0.3)
        audio = recognizer.record(source)

    return audio


# ─────────────────────────────────────────────
#  CORE: SPEECH TO TEXT
# ─────────────────────────────────────────────

def recognize_speech(audio: sr.AudioData) -> str | None:
    """
    Converts audio to text using Google Speech API.
    Tries Hindi first, then English — returns best result.
    """
    recognizer = sr.Recognizer()
    recognizer.energy_threshold = 300   # Lower = more sensitive to quiet speech
    recognizer.dynamic_energy_threshold = True

    for lang in LANGUAGES:
        try:
            text = recognizer.recognize_google(audio, language=lang)
            if text:
                print(f"   ✅ Recognized ({lang}): {text}")
                return text
        except sr.UnknownValueError:
            continue  # Try next language
        except sr.RequestError as e:
            print(f"   ❌ API error: {e}")
            return None

    print("   ⚠️  Could not understand audio")
    return None


# ─────────────────────────────────────────────
#  PRESS TO TALK
# ─────────────────────────────────────────────

def press_to_talk(callback, duration: int = RECORD_SECS):
    """
    Records audio once and calls callback(text) with the result.
    Run in a background thread so UI stays responsive.

    Args:
        callback: function(text: str) called with recognized text
        duration: how many seconds to record
    """
    def _run():
        try:
            audio = record_audio(duration)
            text  = recognize_speech(audio)
            if text:
                callback(text)
            else:
                callback(None)
        except Exception as e:
            print(f"   ❌ Voice error: {e}")
            callback(None)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()


# ─────────────────────────────────────────────
#  ALWAYS LISTENING (Wake Word: "Jarvis")
# ─────────────────────────────────────────────

class WakeWordListener:
    """
    Continuously listens in background.
    When wake word "Jarvis" is detected → records the actual query
    → calls callback(text) with the location name.

    Usage:
        listener = WakeWordListener(callback=my_function)
        listener.start()
        listener.stop()
    """

    def __init__(self, callback, status_callback=None):
        self.callback        = callback
        self.status_callback = status_callback  # optional UI status update
        self._running        = False
        self._thread         = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread  = threading.Thread(target=self._listen_loop, daemon=True)
        self._thread.start()
        self._set_status("👂 Listening for 'Jarvis'...")
        print("   👂 Wake word listener started — say 'Jarvis' to activate")

    def stop(self):
        self._running = False
        self._set_status("⬤  Ready")
        print("   🔇 Wake word listener stopped")

    def _listen_loop(self):
        while self._running:
            try:
                # 3-second listen to detect wake word
                audio = record_audio(duration=3)
                text  = recognize_speech(audio)

                if text and self._is_wake_word(text):
                    self._set_status("🎤 Wake word detected! Speak your location...")
                    print(f"   🟢 Wake word detected! Listening for query...")
                    time.sleep(0.3)

                    # Now record the actual query (longer duration)
                    query_audio = record_audio(duration=6)
                    query_text  = recognize_speech(query_audio)

                    if query_text:
                        # Remove wake word if included in query
                        clean = query_text.lower().replace(WAKE_WORD, "").strip()
                        self.callback(clean if clean else query_text)
                        self._set_status("👂 Listening for 'Jarvis'...")
                    else:
                        self._set_status("⚠️  Didn't catch that. Say 'Jarvis' again...")
                        time.sleep(1)
                        self._set_status("👂 Listening for 'Jarvis'...")

            except Exception as e:
                if self._running:
                    print(f"   ⚠️  Listener error: {e}")
                    time.sleep(1)

    def _is_wake_word(self, text: str) -> bool:
        """
        Flexible wake word detection.
        Accepts: jarvis, jar, jarvi, jarvis, hey jarvis, ok jarvis
        """
        text = text.lower().strip()
        wake_variants = [
            "jarvis", "jar", "jarvi", "jarvis", "hey jarvis",
            "ok jarvis", "okay jarvis", "javis", "jarvas", "jervis"
        ]
        return any(variant in text for variant in wake_variants)

    def _set_status(self, text):
        if self.status_callback:
            self.status_callback(text)