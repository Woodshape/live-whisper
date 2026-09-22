"""Replay a speech recording at real-time speed through the actual live worker.

Usage: uv run python tests/latency_probe.py
Fixture: whisper.cpp samples/jfk.wav, SHA256 59dfb9a4acb36fe2a2affc14bacbee2920ff435cb13cc314a08c13f66ba7860e.
Use --simulated to model a 1.4x Whisper without downloading model weights.
"""

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app


class SimulatedModel:
    def transcribe(self, audio, **kwargs):
        time.sleep(len(audio) / app.RATE / 1.4)
        return iter([type("Segment", (), {"text": " Ask not what your country can do for you"})()]), None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--simulated', action='store_true')
    parser.add_argument('--max-first-line-seconds', type=float, default=10)
    parser.add_argument('--chunk-seconds', type=int, default=8)
    parser.add_argument('--show-transcript', action='store_true')
    args = parser.parse_args()
    audio = Path(os.environ.get('LIVE_WHISPER_AUDIO', str(Path(__file__).resolve().parent / 'fixtures/jfk.wav')))
    if not audio.is_file():
        raise SystemExit(f"Download the test audio first: {audio}")
    source = 'test-speech-file'
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-re",
               "-i", str(audio), "-vn", "-ac", "1", "-ar", str(app.RATE), "-f", "s16le", "pipe:1"]
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / 'transcript.txt'
        with patch.object(app, 'LIVE_CHUNK_PRESETS', {2, 4, 8}), \
                patch.object(app, 'devices', return_value=[{'id': source, 'label': 'Speech test'}]), \
                patch.object(app, 'capture_command', return_value=command), \
                patch.object(app, 'load_model', return_value=SimulatedModel()) if args.simulated else patch.object(app, 'load_model', wraps=app.load_model):
            engine = app.Transcriber()
            started = time.monotonic()
            engine.start('live', source, str(output), 'base', live_chunk_seconds=args.chunk_seconds)
            first = None
            while time.monotonic() - started < 90:
                status = engine.status()
                if first is None and output.exists() and output.stat().st_size:
                    first = time.monotonic() - started
                    print(f"first transcript line after {first:.2f}s; speed={status['processing_speed']}x; "
                          f"audio buffered={status['backlog_seconds']}s", flush=True)
                if status['state'] in ('finished', 'stopped', 'error'):
                    break
                time.sleep(0.05)
            status = engine.status()
            print(f"final status: {status['state']}, speed={status['processing_speed']}x, "
                  f"audio captured={status['captured_seconds']}s, lines={status['lines']}, "
                  f"error={status['error']!r}", flush=True)
            if args.show_transcript and output.exists():
                print('Transcript:\n' + output.read_text(), flush=True)
            if first is None or first > args.max_first_line_seconds:
                raise SystemExit(f"FAIL: first line latency {first}s exceeds {args.max_first_line_seconds}s")
            print('PASS: first line latency within limit')


if __name__ == '__main__':
    main()
