# Live Whisper

Local transcription of live computer audio or an existing audio/video file. The Flutter desktop MVP has no browser or HTTP server: it starts a local Python worker over private stdin/stdout pipes. FFmpeg decodes audio and [faster-whisper](https://github.com/SYSTRAN/faster-whisper) transcribes it on your CPU. Audio stays on your computer (the model downloads on first use).

## Flutter desktop MVP (Linux and macOS)

Install [Flutter](https://docs.flutter.dev/get-started/install) and the FFmpeg / Python / uv prerequisites below. Ensure `flutter` is on your PATH; on this Linux machine the SDK is at `~/.local/opt/flutter/bin`, so first run `export PATH="$HOME/.local/opt/flutter/bin:$PATH"`. Then, from this repository:

```sh
cd live-whisper
uv sync --python 3.13
cd flutter_app
flutter pub get
flutter run -d linux      # Linux
# OR: flutter run -d macos # On a Mac with Xcode
```

The native window offers live capture, file selection without copying/uploading the original, output file selection (or enter a path), model choice with an optional **Pre-load model** button, a **Low latency (4s)** / **Higher accuracy (8s)** live preset, Start/Stop, live status, speed/backlog indicators, and a scrollable preview of the last ten saved transcript lines (newest first). Low latency is the desktop default; the longer preset gives Whisper more context and may help a slower computer sustain real-time processing. The selection takes effect when you start a new live session; file transcription stays at 30-second chunks. Pre-load downloads and initializes the selected model without capturing audio; wait for **Model ready in memory** before starting a session to skip the model-loading wait. Only one model is held in memory; switching models replaces it, and closing the app releases it. If you skip Pre-load, Start loads the model automatically while buffering captured audio. Even with a warm model, the first live transcript line waits for the first 4- or 8-second chunk and inference. During file transcription, the audio-waiting indicator also shows an approximate ETA once recent processing speed is available (queued decoded audio ÷ speed). It may vary with the recording; live capture has no completion ETA. The preview resets when a new session starts and persists when the output file changes during a session. For an existing file choose **Choose audio / video**, set an output path, then **Transcribe file**. File transcription reads the original recording without deleting it. The Stop button stops capture and processes any audio already buffered. The output file is append-only; changing it during a session sends future lines to the new path. Keep the app open until processing finishes.

**MVP packaging boundary:** `flutter build linux` and `flutter build macos` compile the UI, but the resulting bundle **is not yet self-contained**. It expects this source tree, `live-whisper/.venv`, and FFmpeg installed; run it from `flutter_app/`. Mac builds and audio capture still need testing on a Mac; when running sandboxed, use native file pickers so macOS can grant access to selected paths. Shipping a portable Linux package or signed/notarized Mac `.app` will require bundling the backend, Python dependencies and FFmpeg (or migrating to whisper.cpp), and handling OS permissions. Neither Flutter nor the native window bypasses BlackHole for macOS system audio.

## Browser prototype (optional)

Requirements: Linux or macOS, [FFmpeg](https://ffmpeg.org/download.html), [uv](https://docs.astral.sh/uv/getting-started/installation/), and Python 3.10–3.13 (uv can install Python 3.13 for you). On Linux, also install `pactl` and enable PulseAudio or PipeWire's PulseAudio compatibility service. FFmpeg must support the `pulse` input on Linux or `avfoundation` on macOS.

```sh
cd live-whisper
uv run --python 3.13 python app.py
```

Open **http://127.0.0.1:8765/** in your browser. Set an output path such as `~/transcript.txt`, choose a model (`base` is a good starting point), then either select a device and start live transcription or select a recording and click **Upload & transcribe**. Stop with the Stop button or Ctrl+C in the terminal. To switch transcript files during a session, enter a new path and click **Set / change output**. Existing files are appended to, not overwritten. The output directory must exist.

To change the port, pass `--port 9000`. The server only listens on `127.0.0.1`; it is not exposed to your network. Uploads are copied to a temporary file while they process and removed when the job ends. Closing the browser tab after upload does not interrupt transcription, but closing the terminal does.

### Capturing system audio

- **Linux:** Choose **Default system audio**, which is placed first in the device list. Other **System audio** monitors may belong to inactive HDMI outputs and record silence. This works with PipeWire via `pipewire-pulse` or PulseAudio. Select a **Microphone** source instead for your own voice; a monitor alone may not capture your microphone in a meeting.
- **macOS:** macOS does not offer direct system-audio capture. Install a virtual loopback device such as [BlackHole](https://existential.audio/blackhole/). In Audio MIDI Setup, create a Multi-Output Device containing BlackHole and your speakers/headphones; select it as your Mac's audio output. Refresh the device list and select BlackHole in this app. If prompted, allow your terminal microphone/audio input permission. A loopback alone may not capture your microphone in a meeting; route/mix both inputs if you need both sides.

**First run:** Model downloads can take several minutes. The desktop UI distinguishes checking the cache, downloading (bytes, percent, and an approximate download ETA once transfer speed is measurable), and initializing the model. The download ETA is **not** a guaranteed time to the first transcript line: model initialization and processing buffered audio take additional, unpredictable time. On a cached model, there is no download ETA. Audio is buffered to a temporary disk file from the start of capture and transcribed after loading. If you stop during download, leave the application running until it finishes processing the captured audio. The UI shows seconds captured and whether any non-silent audio was detected. Once speech is processed, it also shows transcription speed (the last five speech chunks, where 1.0× means processing a chunk took as long as the chunk's audio duration) and audio still waiting to be processed. Below 1.0× the backlog will grow during continuous speech; switch to a smaller model on the next session. Audio is buffered rather than discarded, so a slow model delays the transcript but should not lose it. A backlog of up to about four or eight seconds is normal, depending on the live preset, because audio is processed in chunks. If it reports no audio after several seconds, check the selected device and your system's active output. A `400` response to Start means the job did not start; read the error shown in the UI.

**Note:** The desktop app writes an approximate timestamped line per ~4 or ~8 seconds of live audio, after model inference (the browser prototype continues to default to 8 seconds). File transcription uses ~30-second chunks. These are best-effort chunk timestamps, not word-level synchronized captions. Speech at chunk boundaries can be lost or repeated; there is no speaker identification. Larger models improve accuracy but increase latency, especially on CPU. Uploaded files may be up to 4 GiB. Model downloads require internet once; transcription thereafter runs locally.

## Tests

```sh
uv run --python 3.13 python -m unittest discover -s tests -v
cd flutter_app
flutter analyze
flutter test
flutter build linux --release # On Linux; build macos --release on a Mac
```
