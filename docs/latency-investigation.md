# Live transcription latency investigation (Linux)

At the time of this investigation, `app.Transcriber` waited for a complete **8-second audio chunk** before sending it to Whisper. `processing_speed` is the duration of that chunk divided by its **inference** time; it does *not* include the initial wait for the chunk. A model running at 1.4× takes ~5.7s to process an 8s chunk, so an utterance at the start can take roughly 8 + 5.7 = 13.7s to appear, even though processing is faster than playback. The Flutter UI polls at one-second intervals, adding at most about one second after the line is written.

## Reproduce

Speech fixture: `tests/fixtures/jfk.wav` (11s; downloaded from [whisper.cpp's sample](https://github.com/ggml-org/whisper.cpp/blob/master/samples/jfk.wav), SHA256 `59dfb9a4acb36fe2a2affc14bacbee2920ff435cb13cc314a08c13f66ba7860e`). The probe feeds the file to FFmpeg with `-re` and uses the **same live worker** as the app, writing its transcript to a temporary file. No audio is uploaded during the test.

```sh
cd live-whisper
uv run --python 3.13 python tests/latency_probe.py --simulated
# Expected FAIL: first line >10s despite a simulated model reporting 1.4× speed.
uv run --python 3.13 python tests/latency_probe.py --simulated --chunk-seconds 4
# Expected PASS: the same model, shorter capture chunks.
uv run --python 3.13 python tests/latency_probe.py --chunk-seconds 8 --max-first-line-seconds 60 --show-transcript
# Repeat with --chunk-seconds 4 to compare the actual local base model.
# The probe also permits 2 for experimental comparisons; it is not a product preset.
```

Results from this Linux CPU with cached `base` model, faster-whisper `int8`; times measured from capture start to first line written (not including Flutter's status polling):

| Live chunk | First line | Reported model speed | Observed transcript quality |
| --- | ---: | ---: | --- |
| 8s | ~9.8s | ~3.9× first chunk | Coherent two-line JFK excerpt |
| 4s | ~5.6s | ~2.0× first chunk | Correct words, sentence split mid-phrase |
| 2s | ~3.6s | ~1.0× first chunk | Several incorrect words and fragments |
| 8s, simulated 1.4× | ~13.47s | 1.4× | Fake transcript; reproduces reported 10–20s delay |
| 4s, simulated 1.4× | ~6.54s | 1.4× | Fake transcript |

These are *one short speech sample*, not general accuracy or throughput benchmarks. With very short chunks Whisper has less context and incurs more overhead; on slower hardware, 4s chunks might push a 1.4× model below 1.0×, causing a growing backlog. FFmpeg's `64 KiB` capture reads correspond to ~2.05s of mono 16kHz PCM and may add jitter for chunk sizes not divisible by this read interval; they do not explain the 13s baseline. The file contains no permanent recordings from the probe.

**Conclusion:** Inference speed and end-to-end caption delay are distinct. The major source of delay is the required chunk wait followed by inference. The Flutter desktop app now defaults to **Low latency (4s)** and offers **Higher accuracy (8s)** as a per-session choice. The browser prototype retains its 8s default. A possible future improvement is tentative short-chunk captions followed by longer-context corrections (more work, but better accuracy).
