import io
from datetime import datetime
import math
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import wave
from pathlib import Path
from unittest.mock import patch

import app
from youtube_import import CaptionCue, YouTubeImport, YouTubeImportCancelled


class FakeModel:
    def transcribe(self, audio, **kwargs):
        return iter([type("Segment", (), {"text": " hello world"})()]), None


def wait_for(engine):
    for _ in range(300):
        if engine.status()["state"] in ("finished", "error", "stopped", "cancelled"):
            return engine.status()
        time.sleep(0.02)
    raise AssertionError(f"Worker did not finish: {engine.status()}")


class TranscriberTests(unittest.TestCase):
    def test_default_path_uses_local_start_time_without_seconds(self):
        with patch.object(app, "datetime") as clock:
            clock.now.return_value = datetime(2026, 9, 23, 9, 7, 55)
            self.assertEqual(app.default_output_path(), str(Path.home() / "2026-09-23_09-07"))

    def test_blank_output_generates_a_fresh_path_for_each_session(self):
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / "input.wav"
            with wave.open(str(src), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(app.RATE)
                wav.writeframes(b"\xff\x3f" * app.RATE)
            first = Path(directory) / "2026-09-23_09-07"
            second = Path(directory) / "2026-09-23_09-08"
            engine = app.Transcriber()
            with patch.object(app, "load_model", return_value=FakeModel()), \
                 patch.object(app, "default_output_path", side_effect=[str(first), str(second)]) as default:
                engine.start("file", str(src), "", "tiny")
                self.assertEqual(wait_for(engine)["output"], str(first))
                engine.start("file", str(src), None, "tiny")
                self.assertEqual(wait_for(engine)["output"], str(second))
                self.assertEqual(default.call_count, 2)
            self.assertIn("hello world", first.read_text())
            self.assertIn("hello world", second.read_text())

    def test_explicit_output_overrides_default(self):
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / "input.wav"
            with wave.open(str(src), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(app.RATE)
                wav.writeframes(b"\xff\x3f" * app.RATE)
            output = Path(directory) / "my-transcript.txt"
            engine = app.Transcriber()
            with patch.object(app, "load_model", return_value=FakeModel()), \
                 patch.object(app, "default_output_path") as default:
                engine.start("file", str(src), str(output), "tiny")
                self.assertEqual(wait_for(engine)["output"], str(output))
                default.assert_not_called()
            self.assertTrue(output.is_file())

    def test_file_transcription_appends_and_preserves_original_media(self):
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / "input.wav"
            dst = Path(directory) / "result.txt"
            dst.write_text("existing\n")
            with wave.open(str(src), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(app.RATE)
                wav.writeframes(b"".join(int(math.sin(i / 20) * 16000).to_bytes(2, 'little', signed=True) for i in range(app.RATE)))
            engine = app.Transcriber()
            with patch.object(app, "load_model", return_value=FakeModel()):
                engine.start("file", str(src), str(dst), "tiny")
                self.assertEqual(wait_for(engine)["state"], "finished")
            self.assertEqual(dst.read_text(), "existing\n[00:00:00] hello world\n")
            self.assertTrue(src.exists())

    def test_youtube_full_captions_are_written_without_loading_whisper(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "captions.txt"
            captions = (
                CaptionCue(0, 3, "Caption one"),
                CaptionCue(35, 40, "Caption two"),
            )
            engine = app.Transcriber()
            imported = YouTubeImport("Example video", 40, captions, "en")
            with (
                patch.object(app, "import_youtube", return_value=imported) as importer,
                patch.object(app, "load_model") as load_model,
            ):
                engine.start_youtube("https://youtu.be/mjQlZrteMIY", str(output), "tiny", "en")
                status = wait_for(engine)
            importer.assert_called_once()
            load_model.assert_not_called()
            self.assertEqual(status["state"], "finished")
            self.assertEqual(status["input_source"], "youtube")
            self.assertEqual(status["transcript_source"], "youtube_captions")
            self.assertEqual(status["transcript_language"], "en")
            self.assertEqual(status["lines"], 2)
            self.assertEqual(
                output.read_text(),
                "[00:00:00] Caption one\n[00:00:30] Caption two\n",
            )

    def test_youtube_automatic_captions_are_merged_when_opted_in(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "captions.txt"
            captions = (
                CaptionCue(0, 2, "I have so much content"),
                CaptionCue(2, 4, "I have so much content on my channel"),
            )
            engine = app.Transcriber()
            imported = YouTubeImport("Example video", 40, captions, "en-orig",
                                     caption_automatic=True)
            with (
                patch.object(app, "import_youtube", return_value=imported) as importer,
                patch.object(app, "load_model") as load_model,
            ):
                engine.start_youtube("https://youtu.be/mjQlZrteMIY", str(output), "tiny", "en",
                                     automatic_captions=True)
                status = wait_for(engine)
            self.assertTrue(importer.call_args.kwargs["automatic_captions"])
            load_model.assert_not_called()
            self.assertEqual(status["state"], "finished")
            self.assertTrue(status["captions_automatic"])
            self.assertEqual(status["transcript_language"], "en-orig")
            self.assertEqual(output.read_text(),
                             "[00:00:00] I have so much content on my channel\n")

    def test_youtube_audio_fallback_uses_whisper_and_removes_download(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "audio.wav"
            with wave.open(str(source), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(app.RATE)
                wav.writeframes(b"\xff\x3f" * app.RATE)
            output = Path(directory) / "youtube.txt"
            engine = app.Transcriber()
            imported = YouTubeImport("Example video", 1, audio_path=source)
            with (
                patch.object(app, "import_youtube", return_value=imported),
                patch.object(app, "load_model", return_value=FakeModel()),
            ):
                engine.start_youtube("https://www.youtube.com/watch?v=mjQlZrteMIY",
                                     str(output), "tiny", "en")
                status = wait_for(engine)
            self.assertEqual(status["state"], "finished")
            self.assertEqual(status["input_source"], "youtube")
            self.assertEqual(status["transcript_source"], "whisper")
            self.assertFalse(source.exists())
            self.assertIn("hello world", output.read_text())

    def test_youtube_audio_fallback_prefers_the_video_language_over_the_user_selection(self):
        options_seen = []
        class RecordingModel:
            def transcribe(self, audio, **kwargs):
                options_seen.append(kwargs.get("language"))
                return iter([type("Segment", (), {"text": " speech"})()]), None

        cases = (("en", "de", "en"), ("es", "de", "es"), (None, "de", "de"), ("xx", "de", "de"))
        with tempfile.TemporaryDirectory() as directory:
            engine = app.Transcriber()
            with patch.object(app, "load_model", return_value=RecordingModel()):
                for spoken_language, selected, expected in cases:
                    source = Path(directory) / f"audio-{spoken_language}.wav"
                    with wave.open(str(source), "wb") as wav:
                        wav.setnchannels(1)
                        wav.setsampwidth(2)
                        wav.setframerate(app.RATE)
                        wav.writeframes(b"\xff\x3f" * app.RATE)
                    imported = YouTubeImport("Example video", 1, audio_path=source,
                                             spoken_language=spoken_language)
                    with patch.object(app, "import_youtube", return_value=imported):
                        engine.start_youtube("https://youtu.be/mjQlZrteMIY",
                                             str(Path(directory) / "out.txt"), "tiny", selected)
                        status = wait_for(engine)
                    with self.subTest(spoken_language=spoken_language, selected=selected):
                        self.assertEqual(status["state"], "finished")
                        self.assertEqual(status["language"], expected)
                        self.assertEqual(options_seen[-1], expected)

    def test_youtube_import_can_skip_automatic_captions_and_use_whisper(self):
        entered, release = threading.Event(), threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "audio.wav"
            with wave.open(str(source), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(app.RATE)
                wav.writeframes(b"\xff\x3f" * app.RATE)
            output = Path(directory) / "youtube.txt"
            engine = app.Transcriber()
            imported = YouTubeImport("Example video", 1, audio_path=source, spoken_language="en")

            def blocked_import(*args, **kwargs):
                entered.set()
                release.wait(3)
                return imported

            with (
                patch.object(app, "import_youtube", side_effect=blocked_import) as importer,
                patch.object(app, "load_model", return_value=FakeModel()),
            ):
                engine.start_youtube("https://youtu.be/mjQlZrteMIY", str(output), "tiny", "de",
                                     automatic_captions=False)
                self.assertTrue(entered.wait(3))
                self.assertEqual(engine.status()["import_phase"], "checking_transcript")
                release.set()
                status = wait_for(engine)
            self.assertFalse(importer.call_args.kwargs["automatic_captions"])
            self.assertEqual(status["state"], "finished")
            self.assertEqual(status["transcript_source"], "whisper")
            self.assertIn("hello world", output.read_text())

    def test_youtube_automatic_captions_toggle_must_be_boolean(self):
        engine = app.Transcriber()
        with self.assertRaisesRegex(ValueError, "Automatic captions"):
            engine.start_youtube("https://youtu.be/mjQlZrteMIY", "/tmp/out.txt", "tiny", "en",
                                 automatic_captions="yes")
        self.assertEqual(engine.status()["state"], "idle")

    def test_stop_during_youtube_import_waits_for_import_worker_and_cancels(self):
        entered, release = threading.Event(), threading.Event()

        def blocked_import(*args, **kwargs):
            entered.set()
            release.wait(3)
            raise YouTubeImportCancelled("YouTube import cancelled")

        engine = app.Transcriber()
        with patch.object(app, "import_youtube", side_effect=blocked_import):
            engine.start_youtube("https://youtu.be/mjQlZrteMIY", "/tmp/youtube-stop.txt", "tiny", "en")
            self.assertTrue(entered.wait(3))
            engine.stop()
            self.assertEqual(engine.status()["state"], "stopping")
            with self.assertRaisesRegex(ValueError, "buffered transcription"):
                engine.cancel_buffered_audio()
            release.set()
            self.assertEqual(wait_for(engine)["state"], "stopped")

    def test_invalid_youtube_url_is_rejected_before_starting_import(self):
        engine = app.Transcriber()
        with self.assertRaisesRegex(ValueError, "YouTube video link"):
            engine.start_youtube("https://not-youtube.example/watch?v=mjQlZrteMIY",
                                 "/tmp/should-not-start.txt", "tiny", "en")
        self.assertEqual(engine.status()["state"], "idle")

    def test_language_is_fixed_per_session_even_when_reusing_a_warm_model(self):
        options_seen = []
        class RecordingModel:
            def transcribe(self, audio, **kwargs):
                options_seen.append(kwargs)
                return iter([type("Segment", (), {"text": " speech"})()]), None

        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / "input.wav"
            with wave.open(str(src), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(app.RATE)
                wav.writeframes(b"\xff\x3f" * app.RATE)
            engine = app.Transcriber()
            output = str(Path(directory) / "out.txt")
            with self.assertRaisesRegex(ValueError, "Language must be de or en"):
                engine.start("file", str(src), output, "tiny", language="fr")
            self.assertFalse(Path(output).exists())
            with patch.object(app, "load_model", return_value=RecordingModel()) as loader:
                for language in ("de", "en", None):
                    engine.start("file", str(src), output, "tiny", language=language)
                    status = wait_for(engine)
                    self.assertEqual(status["state"], "finished")
                    self.assertEqual(status["language"], language)
                loader.assert_called_once()
            self.assertEqual([options.get("language") for options in options_seen], ["de", "en", None])
            self.assertNotIn("language", options_seen[-1])

    def test_speed_and_audio_backlog_update_after_chunk_finishes(self):
        transcribing, release = threading.Event(), threading.Event()
        class SlowModel:
            def transcribe(self, audio, **kwargs):
                transcribing.set()
                release.wait(2)
                return iter([type("Segment", (), {"text": " speech"})()]), None

        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / "input.wav"
            with wave.open(str(src), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(app.RATE)
                wav.writeframes(b"\xff\x3f" * app.RATE)
            engine = app.Transcriber()
            with patch.object(app, "load_model", return_value=SlowModel()):
                engine.start("file", str(src), str(Path(directory) / "out.txt"), "tiny")
                self.assertTrue(transcribing.wait(3))
                during = engine.status()
                self.assertEqual(during["captured_seconds"], 1.0)
                self.assertEqual(during["processed_seconds"], 0.0)
                self.assertEqual(during["backlog_seconds"], 1.0)
                self.assertIsNone(during["processing_speed"])
                time.sleep(0.05)
                release.set()
                done = wait_for(engine)
            self.assertEqual(done["state"], "finished")
            self.assertEqual(done["processed_seconds"], 1.0)
            self.assertEqual(done["backlog_seconds"], 0.0)
            self.assertGreater(done["processing_speed"], 0)
            self.assertLess(done["processing_speed"], 100)

    def test_cancel_finishes_the_current_30_second_chunk_before_discarding_rest(self):
        transcribing = threading.Event()
        release_transcription = threading.Event()
        writing = threading.Event()
        release_write = threading.Event()
        cancel_returned = threading.Event()
        chunk_lengths = []
        cancel_responses = []
        real_open = open

        class SlowModel:
            def transcribe(self, audio, **kwargs):
                chunk_lengths.append(len(audio))
                transcribing.set()
                release_transcription.wait(3)
                return iter([type("Segment", (), {"text": " finished chunk"})()]), None

        class GatedWriter:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                self.stream.__enter__()
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

            def write(self, text):
                writing.set()
                if not release_write.wait(3):
                    raise TimeoutError("test did not release the transcript write")
                return self.stream.write(text)

        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / "recording.wav"
            output = Path(directory) / "partial-transcript.txt"
            with wave.open(str(src), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(app.RATE)
                wav.writeframes(b"\xff\x3f" * (31 * app.RATE))

            def gated_open(path, mode="r", *args, **kwargs):
                stream = real_open(path, mode, *args, **kwargs)
                if Path(path).resolve() == output.resolve() and mode == "a":
                    return GatedWriter(stream)
                return stream

            engine = app.Transcriber()
            cancel_thread = None
            with (
                patch.object(app, "load_model", return_value=SlowModel()),
                patch("builtins.open", side_effect=gated_open),
            ):
                engine.start("file", str(src), str(output), "tiny")
                self.assertTrue(transcribing.wait(3))
                engine.stop()
                self.assertEqual(engine.status()["state"], "stopping")
                release_transcription.set()
                self.assertTrue(writing.wait(3))

                def request_cancel():
                    cancel_responses.append(engine.cancel_buffered_audio())
                    cancel_returned.set()

                cancel_thread = threading.Thread(target=request_cancel)
                cancel_thread.start()
                self.assertTrue(engine.cancel_event.wait(1))
                self.assertFalse(cancel_returned.is_set())
                release_write.set()
                cancel_thread.join(3)
                self.assertTrue(cancel_returned.is_set())
                self.assertTrue(cancel_responses[0]["cancel_requested"])
                done = wait_for(engine)

            self.assertEqual(done["state"], "cancelled")
            self.assertEqual(chunk_lengths, [30 * app.RATE])
            self.assertEqual(done["processed_seconds"], 30.0)
            self.assertEqual(done["backlog_seconds"], 1.0)
            self.assertEqual(output.read_text(), "[00:00:00] finished chunk\n")

    def test_live_cancel_observes_four_and_eight_second_chunk_boundaries(self):
        for chunk_seconds in (4, 8):
            with self.subTest(chunk_seconds=chunk_seconds), tempfile.TemporaryDirectory() as directory:
                entered, release = threading.Event(), threading.Event()
                chunk_lengths = []

                class SlowModel:
                    def transcribe(self, audio, **kwargs):
                        chunk_lengths.append(len(audio))
                        entered.set()
                        release.wait(3)
                        return iter([type("Segment", (), {"text": " live chunk"})()]), None

                src = Path(directory) / "speech.wav"
                output = Path(directory) / "partial-live.txt"
                with wave.open(str(src), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(app.RATE)
                    wav.writeframes(b"\xff\x3f" * (2 * chunk_seconds * app.RATE))
                command = [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(src),
                    "-vn", "-ac", "1", "-ar", str(app.RATE), "-f", "s16le", "pipe:1",
                ]
                engine = app.Transcriber()
                with (
                    patch.object(app, "devices", return_value=[{"id": "monitor", "label": "Monitor"}]),
                    patch.object(app, "capture_command", return_value=command),
                    patch.object(app, "load_model", return_value=SlowModel()),
                ):
                    engine.start(
                        "live", "monitor", str(output), "tiny",
                        live_chunk_seconds=chunk_seconds,
                    )
                    self.assertTrue(entered.wait(3))
                    engine.stop()
                    cancel_status = engine.cancel_buffered_audio()
                    self.assertTrue(cancel_status["cancel_requested"])
                    release.set()
                    done = wait_for(engine)

                self.assertEqual(done["state"], "cancelled")
                self.assertEqual(chunk_lengths, [chunk_seconds * app.RATE])
                self.assertEqual(done["processed_seconds"], float(chunk_seconds))
                self.assertEqual(output.read_text(), "[00:00:00] live chunk\n")

    def test_recent_lines_match_last_ten_written_lines_and_reset_on_start(self):
        class NumberedModel:
            def __init__(self):
                self.number = 0
            def transcribe(self, audio, **kwargs):
                self.number += 1
                segment = type("Segment", (), {"text": f" chunk\n {self.number}"})()
                return iter([segment]), None

        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / "recording.wav"
            output = Path(directory) / "notes.txt"
            engine = app.Transcriber()
            model = NumberedModel()
            with patch.object(app, "FILE_SECONDS", 1), patch.object(app, "load_model", return_value=model):
                with wave.open(str(src), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(app.RATE)
                    wav.writeframes(b"\xff\x3f" * (12 * app.RATE))
                engine.start("file", str(src), str(output), "tiny")
                finished = wait_for(engine)
                self.assertEqual(finished["state"], "finished")
                self.assertEqual(finished["lines"], 12)
                self.assertEqual(finished["recent_lines"], output.read_text().splitlines()[-10:])
                self.assertTrue(finished["recent_lines"][0].startswith("[00:00:02]"))
                finished["recent_lines"].clear()  # A status snapshot cannot mutate the engine.
                self.assertEqual(len(engine.status()["recent_lines"]), 10)
                with wave.open(str(src), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(app.RATE)
                    wav.writeframes(b"\xff\x3f" * app.RATE)
                engine.start("file", str(src), str(output), "tiny")
                second = wait_for(engine)
                self.assertEqual(second["lines"], 1)
                self.assertEqual(second["recent_lines"], output.read_text().splitlines()[-1:])

    def test_live_chunk_preset_is_per_session_and_changes_batching(self):
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / "speech.wav"
            with wave.open(str(src), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(app.RATE)
                wav.writeframes(b"\xff\x3f" * (12 * app.RATE))
            command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(src),
                       "-vn", "-ac", "1", "-ar", str(app.RATE), "-f", "s16le", "pipe:1"]
            engine = app.Transcriber()
            with patch.object(app, "devices", return_value=[{"id": "test", "label": "test"}]), \
                 patch.object(app, "capture_command", return_value=command), \
                 patch.object(app, "load_model", return_value=FakeModel()):
                for chunk_seconds, expected_lines in ((4, 3), (8, 2)):
                    output = str(Path(directory) / f"output-{chunk_seconds}.txt")
                    engine.start("live", "test", output, "tiny", live_chunk_seconds=chunk_seconds)
                    result = wait_for(engine)
                    self.assertEqual(result["state"], "finished")
                    self.assertEqual(result["live_chunk_seconds"], chunk_seconds)
                    self.assertEqual(result["lines"], expected_lines)
                    self.assertEqual(len(Path(output).read_text().splitlines()), expected_lines)
            self.assertTrue(src.exists())

    def test_invalid_chunk_preset_is_rejected_before_creating_output(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = app.Transcriber()
            output = Path(directory) / "out.txt"
            with self.assertRaisesRegex(ValueError, "chunk"):
                engine.start("live", "anything", str(output), "tiny", live_chunk_seconds=2)
            self.assertFalse(output.exists())
            self.assertEqual(engine.status()["state"], "idle")

    def test_invalid_input_does_not_start_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = app.Transcriber()
            with self.assertRaisesRegex(ValueError, "different"):
                engine.start("file", str(Path(directory) / "a"), str(Path(directory) / "a"), "tiny")
            with self.assertRaisesRegex(ValueError, "Unknown model"):
                engine.start("file", "source", "result.txt", "bad")
            self.assertEqual(engine.status()["state"], "idle")

    def test_model_progress_and_download_eta(self):
        engine = app.Transcriber()
        with patch.object(app.time, "monotonic", side_effect=[10, 14]):
            engine._model_progress("downloading", 100, 1000)
            self.assertIsNone(engine.status()["model_download_eta_seconds"])
            engine._model_progress("downloading", 400, 1000)
            self.assertEqual(engine.status()["model_download_eta_seconds"], 6)
        engine._model_progress("initializing", 0, 0)
        self.assertEqual(engine.status()["model_phase"], "initializing")
        self.assertIsNone(engine.status()["model_download_eta_seconds"])

    def test_model_download_uses_progress_and_local_model_path(self):
        from faster_whisper import WhisperModel
        from huggingface_hub import snapshot_download
        events = []
        def fake_download(repo, **kwargs):
            self.assertEqual(repo, "Systran/faster-whisper-base")
            progress = kwargs["tqdm_class"](desc="Downloading bytes", total=1000)
            progress.update(250)
            progress.close()
            return "/cached/model"
        with patch("huggingface_hub.snapshot_download", side_effect=fake_download), \
             patch("faster_whisper.WhisperModel", return_value=FakeModel()) as model:
            app.load_model("base", progress=lambda *values: events.append(values))
        self.assertIn(("downloading", 250, 1000), events)
        self.assertEqual(events[-1], ("initializing", 0, 0))
        model.assert_called_once_with("/cached/model", device="cpu", compute_type="int8")

    def test_preload_warms_model_without_capture_and_reuses_it(self):
        entered, release = threading.Event(), threading.Event()
        model = FakeModel()
        def slow_load(name, progress=None):
            entered.set()
            release.wait(2)
            progress("initializing", 0, 0)
            return model

        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / "input.wav"
            dst = Path(directory) / "out.txt"
            with wave.open(str(src), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(app.RATE)
                wav.writeframes(b"\xff\x3f" * app.RATE)
            engine = app.Transcriber()
            with patch.object(app, "load_model", side_effect=slow_load) as loader:
                engine.preload_model("tiny")
                self.assertTrue(entered.wait(1))
                self.assertEqual(engine.status()["state"], "preloading")
                self.assertEqual(engine.status()["model_phase"], "checking")
                self.assertEqual(engine.status()["captured_seconds"], 0)
                self.assertIsNone(engine.status()["ready_model"])
                with self.assertRaisesRegex(ValueError, "already in progress"):
                    engine.start("file", str(src), str(dst), "tiny")
                release.set()
                for _ in range(100):
                    if engine.status()["ready_model"] == "tiny":
                        break
                    time.sleep(0.02)
                self.assertEqual(engine.status()["state"], "idle")
                self.assertIsNone(engine.status()["model_phase"])
                engine.preload_model("tiny")  # No redundant load.
                engine.start("file", str(src), str(dst), "tiny")
                self.assertEqual(wait_for(engine)["state"], "finished")
                self.assertEqual(loader.call_count, 1)
                self.assertIn("hello world", dst.read_text())

    def test_start_auto_loads_and_reuses_model_until_selection_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / "input.wav"
            with wave.open(str(src), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(app.RATE)
                wav.writeframes(b"\xff\x3f" * app.RATE)
            engine = app.Transcriber()
            with patch.object(app, "load_model", side_effect=lambda name, progress=None: FakeModel()) as loader:
                for name in ("tiny", "tiny", "base"):
                    engine.start("file", str(src), str(Path(directory) / "out.txt"), name)
                    done = wait_for(engine)
                    self.assertEqual(done["state"], "finished")
                    self.assertEqual(done["ready_model"], name)
                self.assertEqual([call.args[0] for call in loader.call_args_list], ["tiny", "base"])

    def test_preload_failure_reports_error_and_allows_retry(self):
        engine = app.Transcriber()
        with self.assertRaisesRegex(ValueError, "Unknown model"):
            engine.preload_model("bad")
        with patch.object(app, "load_model", side_effect=RuntimeError("download failed")):
            engine.preload_model("tiny")
            self.assertEqual(wait_for(engine)["state"], "error")
            self.assertIsNone(engine.status()["ready_model"])
            self.assertIsNone(engine.status()["model_phase"])
        with patch.object(app, "load_model", return_value=FakeModel()):
            engine.preload_model("tiny")
            for _ in range(100):
                if engine.status()["ready_model"] == "tiny":
                    break
                time.sleep(0.02)
        self.assertEqual(engine.status()["state"], "idle")
        self.assertEqual(engine.status()["error"], "")

    def test_stop_during_model_load(self):
        entered, release = threading.Event(), threading.Event()
        def slow_model(name, **kwargs):
            entered.set()
            release.wait(2)
            return FakeModel()
        with tempfile.TemporaryDirectory() as directory:
            src = Path(directory) / "input"
            src.write_bytes(b"file")
            engine = app.Transcriber()
            with patch.object(app, "load_model", side_effect=slow_model):
                engine.start("file", str(src), str(Path(directory) / "out"), "tiny")
                self.assertTrue(entered.wait(1))
                self.assertEqual(engine.status()["model_phase"], "checking")
                engine.stop()
                release.set()
                done = wait_for(engine)
                self.assertEqual(done["state"], "stopped")
                self.assertIsNone(done["model_phase"])
            self.assertTrue(src.exists())

    def test_http_file_upload_transcribes_without_playback(self):
        engine = app.Transcriber()
        class TestHandler(app.Handler):
            pass
        TestHandler.engine = engine
        server = app.ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                src = Path(directory) / "input.wav"
                dst = Path(directory) / "transcript.txt"
                with wave.open(str(src), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(app.RATE)
                    wav.writeframes(b"".join(int(math.sin(i / 20) * 16000).to_bytes(2, 'little', signed=True) for i in range(app.RATE)))
                from urllib.parse import urlencode
                url = f"http://127.0.0.1:{server.server_port}/api/transcribe-file?" + urlencode({"output": str(dst), "model": "tiny"})
                with patch.object(app, "load_model", return_value=FakeModel()):
                    with urllib.request.urlopen(urllib.request.Request(url, data=src.read_bytes(), method="PUT", headers={"Origin": f"http://127.0.0.1:{server.server_port}"})) as response:
                        self.assertEqual(response.status, 200)
                    self.assertEqual(wait_for(engine)["state"], "finished")
                    automatic = Path(directory) / "2026-09-23_09-07"
                    with patch.object(app, "default_output_path", return_value=str(automatic)):
                        default_url = f"http://127.0.0.1:{server.server_port}/api/transcribe-file?model=tiny"
                        with urllib.request.urlopen(urllib.request.Request(default_url, data=src.read_bytes(), method="PUT", headers={"Origin": f"http://127.0.0.1:{server.server_port}"})) as response:
                            self.assertEqual(response.status, 200)
                        self.assertEqual(wait_for(engine)["output"], str(automatic))
                self.assertIn("hello world", automatic.read_text())
                self.assertIn("hello world", dst.read_text())
                self.assertTrue(src.exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_live_audio_played_while_model_loads_is_transcribed(self):
        # The live source only speaks while the model is downloading/loading.
        loading, release, playback = threading.Event(), threading.Event(), threading.Event()
        playback.set()
        samples = b"\xff\x3f" * app.RATE

        class FakeProcess:
            def __init__(self, *args, **kwargs):
                self.stdout = io.BytesIO(samples if playback.is_set() else b"\0\0" * app.RATE)
            def poll(self):
                return 0
            def wait(self, timeout=None):
                return 0
            def terminate(self):
                pass
            def kill(self):
                pass

        def delayed_model(name, **kwargs):
            loading.set()
            release.wait(2)
            return FakeModel()

        with tempfile.TemporaryDirectory() as directory:
            dst = Path(directory) / "transcript.txt"
            engine = app.Transcriber()
            with patch.object(app, "devices", return_value=[{"id": "monitor", "label": "Monitor"}]), \
                 patch.object(app, "capture_command", return_value=["fake-ffmpeg"]), \
                 patch.object(app.subprocess, "Popen", FakeProcess), \
                 patch.object(app, "load_model", side_effect=delayed_model):
                engine.start("live", "monitor", str(dst), "tiny")
                self.assertTrue(loading.wait(1))
                playback.clear()  # Video finished before the model became available.
                engine.stop()  # User stops while the first download is still in progress.
                release.set()
                self.assertEqual(wait_for(engine)["state"], "stopped")
            self.assertIn("hello world", dst.read_text())

    def test_default_output_monitor_is_first_audio_source(self):
        def pactl(args, **kwargs):
            output = ("1\thdmi.monitor\tPipeWire\n"
                      "2\tspeaker.monitor\tPipeWire\n"
                      "3\tmicrophone\tPipeWire\n") if args[-2:] == ["short", "sources"] else "speaker\n"
            return type("Result", (), {"stdout": output})()
        with patch.object(app.platform, "system", return_value="Linux"), patch.object(app.subprocess, "run", side_effect=pactl):
            self.assertEqual(app.devices()[0]["id"], "speaker.monitor")

    def test_http_rejects_cross_site_writes(self):
        engine = app.Transcriber()
        class TestHandler(app.Handler):
            pass
        TestHandler.engine = engine
        server = app.ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/api/stop"
            with self.assertRaises(urllib.error.HTTPError) as context:
                urllib.request.urlopen(urllib.request.Request(url, data=b"{}", headers={"Origin": "https://evil.example"}))
            self.assertEqual(context.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
