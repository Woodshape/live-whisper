import io
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


class FakeModel:
    def transcribe(self, audio, **kwargs):
        return iter([type("Segment", (), {"text": " hello world"})()]), None


def wait_for(engine):
    for _ in range(300):
        if engine.status()["state"] in ("finished", "error", "stopped"):
            return engine.status()
        time.sleep(0.02)
    raise AssertionError(f"Worker did not finish: {engine.status()}")


class TranscriberTests(unittest.TestCase):
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
