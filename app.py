"""Local-only web UI and streaming transcription worker."""

from __future__ import annotations

import argparse
import functools
import json
import os
import platform
import re
import subprocess
import tempfile
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from youtube_import import YouTubeImportCancelled, import_youtube, normalize_youtube_url, transcript_lines

ROOT = Path(__file__).resolve().parent
RATE = 16_000
LIVE_SECONDS = 8  # Browser prototype default; desktop sends an explicit preset.
LIVE_CHUNK_PRESETS = {4, 8}
FILE_SECONDS = 30
MODELS = {"tiny", "base", "small", "medium", "large-v3"}
MAX_UPLOAD = 4 * 1024**3


def default_output_path() -> str:
    # Local wall-clock time when the transcription starts; minute precision by design.
    return str(Path.home() / datetime.now().strftime("%Y-%m-%d_%H-%M"))


@functools.lru_cache(maxsize=1)
def whisper_languages() -> frozenset[str]:
    """Spoken-language codes the bundled faster-whisper build accepts.

    Mirrors faster-whisper's private constant so validation needs no model load;
    ``WhisperModel.supported_languages`` remains the runtime source of truth.
    """
    from faster_whisper.tokenizer import _LANGUAGE_CODES

    return frozenset(_LANGUAGE_CODES)


def resolve_youtube_language(video_language: str | None, selected: str | None) -> str | None:
    """Use the video's own spoken language, falling back to the user's selection.

    The desktop language choice describes live capture; forcing it onto a video in
    another language makes Whisper translate that audio instead of transcribing it.
    """
    if video_language is not None and video_language in whisper_languages():
        return video_language
    return selected


def devices() -> list[dict[str, str]]:
    system = platform.system()
    if system == "Linux":
        result = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True, text=True, check=True)
        sources = [line.split("\t")[1] for line in result.stdout.splitlines() if len(line.split("\t")) > 1]
        try:
            sink = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True, check=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            sink = ""
        preferred = sink + ".monitor"
        sources.sort(key=lambda name: (name != preferred, not name.endswith(".monitor")))
        return [{"id": name, "label": f"{'Default system audio' if name == preferred else 'System audio' if name.endswith('.monitor') else 'Microphone'}: {name}"} for name in sources]
    if system == "Darwin":
        result = subprocess.run(["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""], capture_output=True, text=True)
        audio = result.stderr.split("AVFoundation audio devices:")[-1] if "AVFoundation audio devices:" in result.stderr else ""
        return [{"id": match.group(1), "label": match.group(2)} for match in re.finditer(r"\[(\d+)\] ([^\n\r]+)", audio)]
    raise ValueError("Only Linux and macOS are supported")


def capture_command(mode: str, source: str) -> list[str]:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if mode == "live":
        if platform.system() == "Linux":
            cmd += ["-f", "pulse", "-i", source]
        elif platform.system() == "Darwin":
            cmd += ["-f", "avfoundation", "-i", f":{source}"]
        else:
            raise ValueError("Only Linux and macOS are supported")
    else:
        cmd += ["-i", source]
    return cmd + ["-vn", "-ac", "1", "-ar", str(RATE), "-f", "s16le", "pipe:1"]


def load_model(name: str, progress=None):
    from faster_whisper import WhisperModel
    if progress is None:
        return WhisperModel(name, device="cpu", compute_type="int8")

    # Use the same model repository, cache and file selection as faster-whisper,
    # but expose Hugging Face's aggregate byte progress instead of suppressing it.
    # The returned directory is then loaded locally (no second download).
    from huggingface_hub import snapshot_download
    from tqdm.auto import tqdm

    with open(os.devnull, "w") as quiet:
        class DownloadProgress(tqdm):
            def __init__(self, *args, **kwargs):
                self.track_bytes = kwargs.get("desc") == "Downloading bytes"
                kwargs["file"] = quiet
                kwargs["disable"] = False  # keep byte counters active without terminal output
                super().__init__(*args, **kwargs)

            def update(self, n=1):
                result = super().update(n)
                if self.track_bytes and self.total and self.n > 0:
                    progress("downloading", self.n, self.total)
                return result

        path = snapshot_download(
            f"Systran/faster-whisper-{name}",
            allow_patterns=["config.json", "preprocessor_config.json", "model.bin",
                            "tokenizer.json", "vocabulary.*"],
            tqdm_class=DownloadProgress,
        )
    progress("initializing", 0, 0)
    return WhisperModel(path, device="cpu", compute_type="int8")


class Transcriber:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.state = "idle"
        self.mode = ""
        self.output = ""
        self.model_name = ""
        self.language = None
        self.warm_model = None
        self.warm_model_name = None
        self.live_chunk_seconds = LIVE_SECONDS
        self.lines = 0
        self.recent_lines = deque(maxlen=10)
        self.captured_seconds = 0.0
        self.processed_seconds = 0.0
        self.processing_speed = None
        self.model_phase = None
        self.model_load_started = None
        self.model_download_bytes = 0
        self.model_download_total = 0
        self.model_download_eta_seconds = None
        self.model_download_started = None
        self.recent_chunks = deque(maxlen=5)
        self.audio_detected = False
        self.input_source = ""
        self.source_title = ""
        self.transcript_source = ""
        self.captions_automatic = False
        self.transcript_language = ""
        self.import_phase = None
        self.import_downloaded_bytes = 0
        self.import_total_bytes = 0
        self.error = ""
        self.stop_event = threading.Event()
        self.cancel_event = threading.Event()
        self.proc: subprocess.Popen | None = None

    def status(self) -> dict:
        with self.lock:
            return {"state": self.state, "mode": self.mode, "output": self.output,
                    "model": self.model_name, "language": self.language,
                    "ready_model": self.warm_model_name,
                    "live_chunk_seconds": self.live_chunk_seconds,
                    "lines": self.lines,
                    "recent_lines": list(self.recent_lines), "error": self.error,
                    "captured_seconds": self.captured_seconds, "audio_detected": self.audio_detected,
                    "processed_seconds": self.processed_seconds,
                    "backlog_seconds": round(max(0, self.captured_seconds - self.processed_seconds), 1),
                    "processing_speed": self.processing_speed,
                    "model_phase": self.model_phase,
                    "model_load_elapsed_seconds": round(time.monotonic() - self.model_load_started)
                    if self.model_phase and self.model_load_started is not None else None,
                    "model_download_bytes": self.model_download_bytes,
                    "model_download_total": self.model_download_total,
                    "model_download_eta_seconds": self.model_download_eta_seconds,
                    "input_source": self.input_source, "source_title": self.source_title,
                    "transcript_source": self.transcript_source,
                    "captions_automatic": self.captions_automatic,
                    "transcript_language": self.transcript_language,
                    "import_phase": self.import_phase,
                    "import_downloaded_bytes": self.import_downloaded_bytes,
                    "import_total_bytes": self.import_total_bytes,
                    "cancel_requested": self.cancel_event.is_set()}

    def _model_progress(self, phase: str, done: int, total: int) -> None:
        with self.lock:
            self.model_phase = phase
            if phase != "downloading":
                self.model_download_eta_seconds = None
                return
            now = time.monotonic()
            if self.model_download_started is None:
                self.model_download_started = now
            self.model_download_bytes = done
            self.model_download_total = total
            elapsed = now - self.model_download_started
            # Wait for a usable rate; the estimate can change with the connection.
            self.model_download_eta_seconds = (
                round((total - done) * elapsed / done)
                if elapsed >= 2 and done > 0 and total > done else None
            )

    def preload_model(self, name: str) -> None:
        if name not in MODELS:
            raise ValueError("Unknown model")
        with self.lock:
            if self.state in ("loading", "capturing", "running", "stopping", "uploading", "preloading", "importing"):
                raise ValueError("A transcription or model load is already in progress")
            if self.warm_model_name == name and self.warm_model is not None:
                return
            # Retain only one model in RAM at a time.
            self.warm_model = self.warm_model_name = None
            self.model_name = name
            self.mode = ""
            self.error = ""
            self.model_phase = "checking"
            self.model_load_started = time.monotonic()
            self.model_download_started = None
            self.model_download_bytes = self.model_download_total = 0
            self.model_download_eta_seconds = None
            self.state = "preloading"
            threading.Thread(target=self._preload, args=(name,), daemon=True).start()

    def _preload(self, name: str) -> None:
        try:
            model = load_model(name, progress=self._model_progress)
            with self.lock:
                self.warm_model = model
                self.warm_model_name = name
                self.model_phase = None
                self.state = "idle"
        except Exception as exc:
            with self.lock:
                self.error = str(exc)
                self.state = "error"
        finally:
            with self.lock:
                self.model_phase = None

    def set_output(self, value: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Enter an output file path")
        path = Path(value).expanduser().resolve()
        if not path.parent.is_dir() or path.is_dir():
            raise ValueError("Output directory must already exist; output must be a file")
        # Check writability before a long-running job, without truncating existing content.
        with path.open("a", encoding="utf-8"):
            pass
        with self.lock:
            self.output = str(path)

    def _prepare_session_locked(self, mode: str, output: str | None, model: str,
                                live_chunk_seconds: int, language: str | None,
                                input_source: str, initial_state: str) -> threading.Event:
        if output is None or (isinstance(output, str) and not output.strip()):
            output = default_output_path()
        self.set_output(output)
        self.mode, self.model_name, self.lines, self.error = mode, model, 0, ""
        self.language = language
        self.live_chunk_seconds = live_chunk_seconds
        self.recent_lines.clear()
        self.captured_seconds, self.processed_seconds = 0.0, 0.0
        self.processing_speed = None
        if input_source != "youtube" and self.warm_model_name != model:
            self.warm_model = self.warm_model_name = None
        self.model_phase = None if input_source == "youtube" or self.warm_model is not None else "checking"
        self.model_load_started = time.monotonic() if self.model_phase else None
        self.model_download_started = None
        self.model_download_bytes = self.model_download_total = 0
        self.model_download_eta_seconds = None
        self.recent_chunks.clear()
        self.audio_detected = False
        self.input_source = input_source
        self.source_title = ""
        self.transcript_source = "whisper" if input_source == "file" else ""
        self.captions_automatic = False
        self.transcript_language = ""
        self.import_phase = None
        self.import_downloaded_bytes = self.import_total_bytes = 0
        self.stop_event = threading.Event()
        self.cancel_event = threading.Event()
        self.state = initial_state
        return self.stop_event

    def start(self, mode: str, source: str, output: str | None, model: str,
              remove_source: bool = False, live_chunk_seconds: int | None = None,
              language: str | None = None) -> None:
        # Live capture and file import carry no language metadata, so the user's
        # selection is the only signal; the desktop client offers only these codes.
        if language not in (None, "de", "en"):
            raise ValueError("Language must be de or en")
        self._start(mode, source, output, model, remove_source, live_chunk_seconds, language)

    def _start(self, mode: str, source: str, output: str | None, model: str,
               remove_source: bool = False, live_chunk_seconds: int | None = None,
               language: str | None = None) -> None:
        """Start a session. ``language`` must already be validated by the caller."""
        if live_chunk_seconds is None:
            live_chunk_seconds = LIVE_SECONDS
        if type(live_chunk_seconds) is not int or live_chunk_seconds not in LIVE_CHUNK_PRESETS:
            raise ValueError("Live chunk length must be 4 or 8 seconds")
        if model not in MODELS:
            raise ValueError("Unknown model")
        if mode not in ("live", "file"):
            raise ValueError("Unknown mode")
        if not source:
            raise ValueError("Select an audio source or upload a file")
        if mode == "live" and source not in {item["id"] for item in devices()}:
            raise ValueError("Audio source is no longer available; refresh devices")
        with self.lock:
            if self.state in ("loading", "capturing", "running", "stopping", "uploading", "preloading", "importing"):
                raise ValueError("A transcription or model load is already in progress")
            if output is None or (isinstance(output, str) and not output.strip()):
                output = default_output_path()
            if mode == "file" and isinstance(output, str) and Path(output).expanduser().resolve() == Path(source).resolve():
                raise ValueError("Input and output must be different files")
            stop = self._prepare_session_locked(mode, output, model, live_chunk_seconds, language,
                                                "live" if mode == "live" else "file", "loading")
            threading.Thread(target=self._run, args=(mode, source, model, stop,
                                                     remove_source, live_chunk_seconds, language), daemon=True).start()

    def start_youtube(self, url: str, output: str | None, model: str,
                      language: str | None = None, automatic_captions: bool = True) -> None:
        canonical_url = normalize_youtube_url(url)
        if model not in MODELS:
            raise ValueError("Unknown model")
        if language not in (None, "de", "en"):
            raise ValueError("Language must be de or en")
        if not isinstance(automatic_captions, bool):
            raise ValueError("Automatic captions must be true or false")
        with self.lock:
            if self.state in ("loading", "capturing", "running", "stopping", "uploading", "preloading", "importing"):
                raise ValueError("A transcription or model load is already in progress")
            stop = self._prepare_session_locked("file", output, model, FILE_SECONDS,
                                                language, "youtube", "importing")
            self.import_phase = "checking_transcript"
            output_path = self.output
            threading.Thread(target=self._run_youtube, args=(canonical_url, output_path, model,
                                                             language, automatic_captions,
                                                             stop), daemon=True).start()

    def _youtube_progress(self, phase: str, done: int, total: int) -> None:
        with self.lock:
            self.import_phase = phase
            self.import_downloaded_bytes = done
            self.import_total_bytes = total

    def _run_youtube(self, url: str, output: str, model: str, language: str | None,
                     automatic_captions: bool, stop: threading.Event) -> None:
        imported = None
        handed_off = False
        completed = False
        try:
            imported = import_youtube(url, language, stop, self._youtube_progress,
                                      automatic_captions=automatic_captions)
            with self.lock:
                self.source_title = imported.title
                if stop.is_set():
                    raise YouTubeImportCancelled("YouTube import cancelled")
                if imported.captions:
                    lines = transcript_lines(imported.captions, rolling=imported.caption_automatic)
                    if not lines:
                        raise RuntimeError("The caption track did not contain transcript text")
                    with open(self.output, "a", encoding="utf-8") as transcript:
                        transcript.write("\n".join(lines) + "\n")
                    self.lines = len(lines)
                    self.recent_lines.extend(lines[-10:])
                    self.captured_seconds = self.processed_seconds = round(imported.duration, 1)
                    self.transcript_source = "youtube_captions"
                    self.captions_automatic = imported.caption_automatic
                    self.transcript_language = imported.caption_language or ""
                    self.import_phase = None
                    self.state = "finished"
                    completed = True
                    return
                if imported.audio_path is None or not imported.audio_path.is_file():
                    raise RuntimeError("YouTube import produced neither full captions nor an audio file")
                self.state = "idle"
                # The video's own language beats the live-capture selection, which
                # would otherwise make Whisper translate instead of transcribe it.
                self._start("file", str(imported.audio_path), output, model, remove_source=True,
                            language=resolve_youtube_language(imported.spoken_language, language))
                self.input_source = "youtube"
                self.source_title = imported.title
                self.transcript_source = "whisper"
                self.import_phase = None
                handed_off = True
        except YouTubeImportCancelled:
            pass
        except Exception as exc:
            with self.lock:
                self.error = str(exc)
                self.state = "error"
        finally:
            if imported and imported.audio_path and not handed_off:
                try:
                    imported.audio_path.unlink(missing_ok=True)
                except OSError:
                    pass
            if not handed_off:
                with self.lock:
                    self.import_phase = None
                    if self.state != "error":
                        self.state = "finished" if completed else "stopped" if stop.is_set() else "error"
                        if self.state == "error" and not self.error:
                            self.error = "YouTube import ended without a transcript"

    def stop(self) -> None:
        with self.lock:
            if self.state not in ("loading", "capturing", "running", "uploading", "importing"):
                return
            self.stop_event.set()
            self.state = "stopping"
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()

    def cancel_buffered_audio(self) -> dict:
        # Set the event before waiting for the status lock. The transcription
        # worker holds that lock while writing each finished chunk, so this lets
        # it complete the write and observe cancellation before claiming another.
        cancel_event = self.cancel_event
        if (
            self.state != "stopping"
            or self.mode not in ("live", "file")
            or self.import_phase is not None
            or (self.input_source == "youtube" and self.transcript_source != "whisper")
        ):
            raise ValueError("No buffered transcription is being finished")
        cancel_event.set()
        with self.lock:
            return self.status()

    def _run(self, mode: str, source: str, name: str, stop: threading.Event,
             remove_source: bool, live_chunk_seconds: int, language: str | None) -> None:
        cancel = self.cancel_event
        proc = None
        reader = None
        try:
            with tempfile.TemporaryFile(mode="w+t") as log, tempfile.TemporaryFile(buffering=0) as audio_file:
                if stop.is_set():
                    return
                proc = subprocess.Popen(capture_command(mode, source), stdout=subprocess.PIPE, stderr=log)
                # Capture independently of model download/inference. A disk-backed buffer
                # prevents losing speech while loading or when inference lags behind playback.
                condition = threading.Condition()
                capture = {"written": 0, "done": False, "error": None}
                assert proc.stdout is not None

                def record() -> None:
                    try:
                        import numpy as np
                        while True:
                            chunk = proc.stdout.read(64 * 1024)
                            if not chunk:
                                break
                            audio_file.write(chunk)
                            detected = bool(np.max(np.abs(np.frombuffer(chunk[:len(chunk) // 2 * 2], dtype="<i2").astype("float32"))) > 98)
                            with condition:
                                capture["written"] += len(chunk)
                                condition.notify_all()
                            with self.lock:
                                self.captured_seconds = round(capture["written"] / (RATE * 2), 1)
                                self.audio_detected |= detected
                    except Exception as exc:
                        capture["error"] = exc
                    finally:
                        proc.stdout.close()
                        with condition:
                            capture["done"] = True
                            condition.notify_all()

                reader = threading.Thread(target=record, daemon=True)
                with self.lock:
                    self.proc = proc
                    if stop.is_set():
                        proc.terminate()
                    else:
                        self.state = "capturing"
                reader.start()
                with self.lock:
                    model = self.warm_model if self.warm_model_name == name else None
                if model is None:
                    model = load_model(name, progress=self._model_progress)
                    with self.lock:
                        self.warm_model = model
                        self.warm_model_name = name
                with self.lock:
                    self.model_phase = None
                    if not stop.is_set():
                        self.state = "running"
                offset = 0
                size = (live_chunk_seconds if mode == "live" else FILE_SECONDS) * RATE * 2
                while True:
                    if cancel.is_set():
                        break
                    with condition:
                        condition.wait_for(lambda: capture["written"] - offset >= size or capture["done"])
                        count = min(size, capture["written"] - offset)
                        done = capture["done"]
                    if count < size and not done:
                        continue
                    if not count:
                        break
                    # Cancellation and claiming the next chunk are serialized by
                    # the same lock as transcript writes. A queued cancel therefore
                    # cannot cause a second chunk to start while a line is written.
                    with self.lock:
                        if cancel.is_set():
                            break
                    seconds = offset // (RATE * 2)
                    pcm = os.pread(audio_file.fileno(), count // 2 * 2, offset)
                    offset += len(pcm)
                    if len(pcm) < RATE:  # Ignore fragments shorter than half a second.
                        break
                    import numpy as np
                    audio = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768.0
                    speech = bool(np.max(np.abs(audio)) > 0.003)
                    if speech:
                        started = time.monotonic()
                        options = {"beam_size": 5, "vad_filter": True}
                        if language is not None:
                            options["language"] = language
                        segments, _ = model.transcribe(audio, **options)
                        text = " ".join(" ".join(seg.text.split()) for seg in segments if seg.text.strip()).strip()
                        if text:
                            line = f"[{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}] {text}"
                            with self.lock:
                                with open(self.output, "a", encoding="utf-8") as out:
                                    out.write(line + "\n")
                                self.lines += 1
                                self.recent_lines.append(line)
                        elapsed = max(time.monotonic() - started, 0.001)
                    with self.lock:
                        self.processed_seconds = round(offset / (RATE * 2), 1)
                        if speech:
                            self.recent_chunks.append((len(pcm) / (RATE * 2), elapsed))
                            total_audio = sum(chunk[0] for chunk in self.recent_chunks)
                            total_time = sum(chunk[1] for chunk in self.recent_chunks)
                            self.processing_speed = round(total_audio / total_time, 2)
                reader.join()
                if capture["error"]:
                    raise capture["error"]
                code = proc.wait()
                if code != 0 and not stop.is_set():
                    log.seek(0)
                    raise RuntimeError(log.read().strip()[-1000:] or f"FFmpeg exited with code {code}")
        except Exception as exc:
            with self.lock:
                self.error = str(exc)
                self.state = "error"
        finally:
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            if reader:
                reader.join(timeout=3)
            if remove_source:
                Path(source).unlink(missing_ok=True)
            with self.lock:
                self.proc = None
                self.model_phase = None
                if self.state != "error":
                    self.state = (
                        "cancelled"
                        if cancel.is_set()
                        else "stopped" if stop.is_set() else "finished"
                    )


class Handler(BaseHTTPRequestHandler):
    engine: Transcriber

    def log_message(self, format: str, *args) -> None:
        if self.path != "/api/status":
            super().log_message(format, *args)

    def reply(self, code: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/":
            body = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/status":
            self.reply(200, self.engine.status())
        elif path == "/api/devices":
            try:
                self.reply(200, {"devices": devices()})
            except (OSError, subprocess.CalledProcessError, ValueError) as exc:
                self.reply(400, {"error": f"Cannot list audio devices: {exc}"})
        else:
            self.reply(404, {"error": "Not found"})

    def do_POST(self) -> None:
        try:
            self.check_origin()
            size = int(self.headers.get("Content-Length", "0"))
            if size < 0 or size > 65536:
                raise ValueError("Invalid request size")
            data = json.loads(self.rfile.read(size))
            path = urlsplit(self.path).path
            if path == "/api/start":
                self.engine.start("live", data["source"], data.get("output"), data["model"])
            elif path == "/api/output":
                self.engine.set_output(data["output"])
            elif path == "/api/stop":
                self.engine.stop()
            else:
                return self.reply(404, {"error": "Not found"})
            self.reply(200, self.engine.status())
        except (ValueError, KeyError, TypeError, json.JSONDecodeError, OSError) as exc:
            self.log_error("Request rejected: %s", exc)
            self.reply(400, {"error": str(exc)})

    def do_PUT(self) -> None:
        path = urlsplit(self.path)
        if path.path != "/api/transcribe-file":
            return self.reply(404, {"error": "Not found"})
        tmp = None
        try:
            self.check_origin()
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_UPLOAD:
                raise ValueError("Select a nonempty file of at most 4 GiB")
            query = parse_qs(path.query)
            output, model = query.get("output", [""])[0], query["model"][0]
            with self.engine.lock:
                if self.engine.state in ("loading", "capturing", "running", "stopping", "uploading", "importing"):
                    raise ValueError("A transcription is already in progress")
                if model not in MODELS:
                    raise ValueError("Unknown model")
                if output.strip():
                    self.engine.set_output(output)
                else:
                    self.engine.output = ""  # The default is chosen at start, after upload.
                self.engine.state = "uploading"
                self.engine.stop_event = threading.Event()
                stop = self.engine.stop_event
            with tempfile.NamedTemporaryFile(prefix="live-whisper-", suffix=".media", delete=False) as file:
                tmp = file.name
                while size:
                    if stop.is_set():
                        raise ValueError("Upload stopped")
                    chunk = self.rfile.read(min(size, 1024 * 1024))
                    if not chunk:
                        raise ValueError("Upload interrupted")
                    file.write(chunk)
                    size -= len(chunk)
            with self.engine.lock:
                if stop.is_set():
                    raise ValueError("Upload stopped")
                self.engine.state = "idle"
                self.engine.start("file", tmp, output, model, remove_source=True)
            tmp = None  # Worker owns the file now.
            self.reply(200, self.engine.status())
        except (ValueError, KeyError, IndexError, TypeError, OSError) as exc:
            with self.engine.lock:
                if self.engine.state in ("uploading", "stopping") and self.engine.proc is None:
                    self.engine.state = "stopped"
            self.reply(400, {"error": str(exc)})
        finally:
            if tmp:
                Path(tmp).unlink(missing_ok=True)

    def check_origin(self) -> None:
        # Local-only service: reject cross-site POSTs to a user's loopback server.
        expected = f"http://127.0.0.1:{self.server.server_port}"
        if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}" or self.headers.get("Origin") != expected:
            raise ValueError("Requests must originate from this app")


def main() -> None:
    parser = argparse.ArgumentParser(description="Local live and file transcription")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    Handler.engine = Transcriber()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Open http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        Handler.engine.stop()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
