"""Best-effort YouTube captions-first import for local transcription."""

from __future__ import annotations

import html
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple
from urllib.parse import parse_qs, urlsplit

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
LANGUAGE_CODE = re.compile(r"^[a-z]{2,3}$")
TIMESTAMP = re.compile(r"^(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{1,3})$")
MAX_VIDEO_SECONDS = 4 * 60 * 60
MAX_AUDIO_BYTES = 2 * 1024**3
MAX_CAPTION_BYTES = 32 * 1024**2


@dataclass(frozen=True)
class CaptionCue:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class YouTubeImport:
    title: str
    duration: float
    captions: tuple[CaptionCue, ...] = ()
    caption_language: str | None = None
    spoken_language: str | None = None
    caption_automatic: bool = False
    audio_path: Path | None = None


class CaptionTrack(NamedTuple):
    language: str
    format: dict
    automatic: bool


class YouTubeImportCancelled(Exception):
    """Raised when the user cancels an in-progress import."""


def normalize_youtube_url(value: str) -> str:
    """Validate a supported YouTube video URL and discard non-video parameters."""
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise ValueError("Enter a valid YouTube video URL")
    try:
        parsed = urlsplit(value.strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Enter a valid YouTube video URL") from exc
    if (parsed.scheme.lower() != "https" or parsed.username is not None
            or parsed.password is not None or port not in (None, 443)):
        raise ValueError("Use an HTTPS YouTube video link")

    video_id = None
    if host in {"youtu.be", "www.youtu.be"}:
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) == 1:
            video_id = parts[0]
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        parts = [part for part in parsed.path.split("/") if part]
        if parsed.path.rstrip("/") == "/watch":
            values = parse_qs(parsed.query).get("v", [])
            if len(values) == 1:
                video_id = values[0]
        elif len(parts) == 2 and parts[0] in {"shorts", "live", "embed", "v"}:
            video_id = parts[1]
    if not video_id or not VIDEO_ID.fullmatch(video_id):
        raise ValueError("Enter a YouTube video link (playlists and other sites are not supported)")
    return f"https://www.youtube.com/watch?v={video_id}"


def _seconds(value: str) -> float | None:
    match = TIMESTAMP.fullmatch(value.strip())
    if not match:
        return None
    hours, minutes, seconds, millis = match.groups()
    fraction = int(millis.ljust(3, "0")) / 1000
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + fraction


def video_language(info: dict) -> str | None:
    """Return the video's spoken language as a bare code (``en`` from ``en-US``)."""
    value = info.get("language")
    if not isinstance(value, str):
        return None
    code = value.strip().lower().replace("_", "-").split("-", 1)[0]
    return code if LANGUAGE_CODE.fullmatch(code) else None


def parse_captions(content: str, extension: str = "vtt") -> tuple[CaptionCue, ...]:
    """Parse WebVTT/SRT cues, stripping markup and normalizing cue text."""
    content = content.lstrip("\ufeff")
    cues: list[CaptionCue] = []
    for block in re.split(r"\r?\n[ \t]*\r?\n", content.strip()):
        lines = block.splitlines()
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        timings = lines[timing_index].split("-->", 1)
        start = _seconds(timings[0].strip().split()[0])
        end = _seconds(timings[1].strip().split()[0])
        if start is None or end is None or end < start:
            continue
        text = " ".join(lines[timing_index + 1 :])
        text = re.sub(r"<[^>]*>", "", text)
        text = html.unescape(text).replace("\u200b", "").replace("\u200e", "")
        text = " ".join(text.split())
        if text:
            cues.append(CaptionCue(start=start, end=end, text=text))
    return tuple(cues)


def is_full_transcript(cues: tuple[CaptionCue, ...], duration: float) -> bool:
    """Reject empty/partial caption tracks so Whisper can cover the whole video."""
    if not cues or duration <= 0:
        return False
    first = min(cue.start for cue in cues)
    last = max(cue.end for cue in cues)
    return first <= min(30.0, duration * 0.05) and last >= duration - min(60.0, duration * 0.05)


def transcript_lines(cues: tuple[CaptionCue, ...], group_seconds: int = 30,
                     rolling: bool = False) -> list[str]:
    """Group timed caption cues into the app's timestamped transcript format.

    ``rolling`` merges YouTube's automatic captions, where every cue repeats the
    previous cue and appends a few words, so the transcript is not triplicated.
    """
    groups: dict[int, list[str]] = {}
    accumulated: list[str] = []
    for cue in cues:
        timestamp = int(cue.start // group_seconds) * group_seconds
        words = cue.text.split()
        if rolling:
            overlap = 0
            for size in range(min(len(accumulated), len(words)), 0, -1):
                if accumulated[-size:] == words[:size]:
                    overlap = size
                    break
            words = words[overlap:]
            accumulated.extend(words)
        text = " ".join(words)
        if text and (not groups.get(timestamp) or groups[timestamp][-1] != text):
            groups.setdefault(timestamp, []).append(text)
    lines = []
    for timestamp, parts in sorted(groups.items()):
        text = " ".join(parts)
        lines.append(f"[{timestamp // 3600:02}:{timestamp // 60 % 60:02}:{timestamp % 60:02}] {text}")
    return lines


def _language_base(language: str) -> str:
    return language.lower().split("-", 1)[0]


def choose_caption_track(info: dict, preferred_language: str | None,
                         allow_automatic: bool = True) -> CaptionTrack | None:
    """Pick a caption track, preferring creator-written captions over automatic ones.

    Automatic captions are only considered when ``allow_automatic`` is set and no
    manual track exists; their cues repeat each previous cue, so callers must merge
    them with :func:`transcript_lines` (``rolling=True``).
    """
    original = info.get("language")
    original_base = _language_base(original) if isinstance(original, str) and original else None
    preferred_base = _language_base(preferred_language) if preferred_language else None
    sources = [("subtitles", False)] + ([] if not allow_automatic else [("automatic_captions", True)])
    candidates: list[tuple[tuple[int, int, int, int], str, dict, bool]] = []
    for source_rank, (source, automatic) in enumerate(sources):
        tracks = info.get(source) or {}
        if not isinstance(tracks, dict):
            continue
        for language, formats in tracks.items():
            if not isinstance(language, str) or not isinstance(formats, list):
                continue
            formats = [item for item in formats if isinstance(item, dict)]
            base = _language_base(language)
            if original_base and base == original_base:
                language_rank = 0
            elif preferred_base and base == preferred_base:
                language_rank = 1 if original_base else 0
            elif base == "en":
                language_rank = 2
            else:
                language_rank = 3
            format_entry = next((item for item in formats if item.get("ext") == "vtt"), None)
            if format_entry is None:
                format_entry = next((item for item in formats if item.get("ext") == "srt"), None)
            if format_entry is None or not format_entry.get("url"):
                continue
            rank = (source_rank, language_rank,
                    0 if language.lower().endswith("-orig") else 1,
                    0 if format_entry.get("ext") == "vtt" else 1)
            candidates.append((rank, language, format_entry, automatic))
    if not candidates:
        return None
    _, language, entry, automatic = min(candidates, key=lambda candidate: candidate[0])
    return CaptionTrack(language, entry, automatic)


def import_youtube(
    url: str,
    language: str | None,
    stop_event: threading.Event,
    progress,
    automatic_captions: bool = True,
) -> YouTubeImport:
    """Use a creator-written caption track when one exists, otherwise fetch the audio.

    Creator-written captions always win. ``automatic_captions=False`` skips YouTube's
    automatic captions as well, so those videos are transcribed locally.
    """
    canonical_url = normalize_youtube_url(url)
    video_id = canonical_url.rsplit("=", 1)[-1]
    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError
    except ImportError as exc:
        raise RuntimeError("YouTube support requires yt-dlp; install project dependencies and retry") from exc

    def cancelled() -> None:
        if stop_event.is_set():
            raise YouTubeImportCancelled("YouTube import cancelled")

    def download_progress(event: dict) -> None:
        cancelled()
        downloaded = int(event.get("downloaded_bytes") or 0)
        total = int(event.get("total_bytes") or event.get("total_bytes_estimate") or 0)
        if downloaded > MAX_AUDIO_BYTES or total > MAX_AUDIO_BYTES:
            raise DownloadError("YouTube audio exceeds the 2 GiB download limit")
        if event.get("status") in {"downloading", "finished"}:
            progress("downloading_audio", downloaded, total)

    options = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 20,
        "retries": 2,
        "fragment_retries": 2,
        "format": "bestaudio",
        "max_filesize": MAX_AUDIO_BYTES,
    }
    temp_base: str | None = None
    keep_audio = False
    try:
        with YoutubeDL(options) as ydl:
            progress("checking_transcript", 0, 0)
            info = ydl.extract_info(canonical_url, download=False)
            cancelled()
            if not isinstance(info, dict) or str(info.get("id")) != video_id:
                raise ValueError("Could not resolve that YouTube video")
            if info.get("is_live") or info.get("live_status") in {"is_live", "is_upcoming"}:
                raise ValueError("Live and upcoming YouTube videos are not supported")
            duration = float(info.get("duration") or 0)
            if duration <= 0:
                raise ValueError("Could not determine YouTube video duration")
            if duration > MAX_VIDEO_SECONDS:
                raise ValueError("YouTube videos longer than four hours are not supported")
            title = str(info.get("title") or video_id)
            spoken_language = video_language(info)

            track = choose_caption_track(info, language, allow_automatic=automatic_captions)
            if track:
                caption_language, caption_format = track.language, track.format
                try:
                    response = ydl.urlopen(caption_format["url"])
                    data = bytearray()
                    try:
                        while True:
                            cancelled()
                            chunk = response.read(min(64 * 1024, MAX_CAPTION_BYTES + 1 - len(data)))
                            if not chunk:
                                break
                            data.extend(chunk)
                            if len(data) > MAX_CAPTION_BYTES:
                                raise ValueError("Caption track exceeds the 32 MiB limit")
                    finally:
                        response.close()
                    captions = parse_captions(data.decode("utf-8-sig", errors="replace"), caption_format.get("ext", "vtt"))
                    if is_full_transcript(captions, duration):
                        progress("using_captions", 0, 0)
                        return YouTubeImport(title, duration, captions, caption_language,
                                             spoken_language=spoken_language,
                                             caption_automatic=track.automatic)
                except YouTubeImportCancelled:
                    raise
                except Exception:
                    # Captions may be advertised but unavailable (for example, rate-limited).
                    # In that case try the audio path rather than failing the import.
                    pass

            cancelled()
            progress("downloading_audio", 0, 0)
            fd, temp_base = tempfile.mkstemp(prefix="live-whisper-youtube-", suffix=".media")
            os.close(fd)
            Path(temp_base).unlink(missing_ok=True)
            ydl.params.update({
                "skip_download": False,
                "outtmpl": {"default": f"{temp_base}.%(ext)s"},
                "progress_hooks": [download_progress],
            })
            ydl.process_ie_result(info, download=True)
            cancelled()
            candidates = [
                path for path in Path(temp_base).parent.glob(Path(temp_base).name + ".*")
                if path.is_file() and not path.name.endswith((".part", ".ytdl"))
            ]
            if not candidates:
                raise RuntimeError("yt-dlp did not produce an audio file")
            audio_path = max(candidates, key=lambda path: path.stat().st_size)
            if audio_path.stat().st_size > MAX_AUDIO_BYTES:
                raise ValueError("YouTube audio exceeds the 2 GiB download limit")
            progress("preparing_audio", audio_path.stat().st_size, audio_path.stat().st_size)
            keep_audio = True
            return YouTubeImport(title, duration, audio_path=audio_path,
                                 spoken_language=spoken_language)
    except YouTubeImportCancelled:
        raise
    except Exception:
        if stop_event.is_set():
            raise YouTubeImportCancelled("YouTube import cancelled")
        raise
    finally:
        # Keep only a successfully returned audio file; the transcription worker owns it.
        if temp_base and not keep_audio:
            for path in Path(temp_base).parent.glob(Path(temp_base).name + ".*"):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
