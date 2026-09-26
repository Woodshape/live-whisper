import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import yt_dlp

from youtube_import import (
    CaptionCue,
    choose_caption_track,
    import_youtube,
    is_full_transcript,
    normalize_youtube_url,
    parse_captions,
    transcript_lines,
    video_language,
)


class YouTubeImportTests(unittest.TestCase):
    def test_normalizes_supported_video_urls_and_discards_playlist_parameters(self):
        self.assertEqual(
            normalize_youtube_url("https://www.youtube.com/watch?v=mjQlZrteMIY&list=PL123"),
            "https://www.youtube.com/watch?v=mjQlZrteMIY",
        )
        self.assertEqual(
            normalize_youtube_url("https://youtu.be/mjQlZrteMIY?si=tracking"),
            "https://www.youtube.com/watch?v=mjQlZrteMIY",
        )
        self.assertEqual(
            normalize_youtube_url("https://www.youtube.com/shorts/mjQlZrteMIY"),
            "https://www.youtube.com/watch?v=mjQlZrteMIY",
        )

    def test_rejects_non_youtube_hosts_invalid_ids_and_non_https_urls(self):
        for url in (
            "https://youtube.com.evil.example/watch?v=mjQlZrteMIY",
            "https://evil.example/watch?v=mjQlZrteMIY",
            "http://youtube.com/watch?v=mjQlZrteMIY",
            "https://youtube.com/watch?v=short",
            "https://youtube.com/playlist?list=PL123",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_youtube_url(url)

    def test_parses_vtt_markup_entities_and_timestamped_lines(self):
        cues = parse_captions(
            """WEBVTT\n\n00:00:00.000 --> 00:00:02.500 align:start\n<v Speaker>Hello &amp; welcome.</v>\n\n00:00:30.000 --> 00:00:33.000\nSecond line\n"""
        )
        self.assertEqual(cues, (
            CaptionCue(0.0, 2.5, "Hello & welcome."),
            CaptionCue(30.0, 33.0, "Second line"),
        ))
        self.assertEqual(
            transcript_lines(cues),
            ["[00:00:00] Hello & welcome.", "[00:00:30] Second line"],
        )

    def test_normalizes_the_video_spoken_language_to_a_bare_model_code(self):
        self.assertEqual(video_language({"language": "en-US"}), "en")
        self.assertEqual(video_language({"language": "DE"}), "de")
        self.assertEqual(video_language({"language": "zh-Hant"}), "zh")
        self.assertEqual(video_language({"language": "pt_BR"}), "pt")
        self.assertEqual(video_language({"language": "haw"}), "haw")
        for info in ({}, {"language": None}, {"language": ""}, {"language": 7}, {"language": "english"}):
            with self.subTest(info=info):
                self.assertIsNone(video_language(info))

    def test_only_treats_near_full_caption_tracks_as_complete(self):
        complete = (CaptionCue(0, 5, "start"), CaptionCue(95, 100, "end"))
        partial = (CaptionCue(0, 5, "start"), CaptionCue(70, 75, "partial"))
        self.assertTrue(is_full_transcript(complete, 100))
        self.assertFalse(is_full_transcript(partial, 100))
        self.assertFalse(is_full_transcript(complete, 0))

    def test_selects_the_videos_spoken_language_over_a_manual_translation(self):
        info = {
            "language": "en-US",
            "subtitles": {
                "de": [{"ext": "vtt", "url": "https://captions.invalid/de"}],
                "en": [{"ext": "vtt", "url": "https://captions.invalid/en"}],
            },
        }
        track = choose_caption_track(info, "de")
        self.assertEqual(track.language, "en")
        self.assertEqual(track.format["url"], "https://captions.invalid/en")
        self.assertFalse(track.automatic)

    def test_prefers_manual_captions_over_automatic_ones(self):
        info = {
            "language": "en",
            "subtitles": {"en": [{"ext": "vtt", "url": "https://captions.invalid/manual"}]},
            "automatic_captions": {"en": [{"ext": "vtt", "url": "https://captions.invalid/auto"}]},
        }
        track = choose_caption_track(info, "en")
        self.assertEqual(track.format["url"], "https://captions.invalid/manual")
        self.assertFalse(track.automatic)

    def test_automatic_captions_need_an_opt_in_and_no_manual_track(self):
        info = {
            "language": "en-US",
            "subtitles": {},
            "automatic_captions": {"en-orig": [{"ext": "vtt", "url": "https://captions.invalid/en"}]},
        }
        track = choose_caption_track(info, "en")
        self.assertTrue(track.automatic)
        self.assertEqual(track.language, "en-orig")
        self.assertIsNone(choose_caption_track(info, "en", allow_automatic=False))

    def test_merges_rolling_automatic_caption_cues(self):
        cues = (
            CaptionCue(0.0, 2.0, "I have so much content"),
            CaptionCue(2.0, 4.0, "I have so much content on my channel"),
            CaptionCue(4.0, 6.0, "on my channel covering how to build this"),
            CaptionCue(31.0, 33.0, "covering how to build this system"),
        )
        self.assertEqual(
            transcript_lines(cues, rolling=True),
            [
                "[00:00:00] I have so much content on my channel covering how to build this",
                "[00:00:30] system",
            ],
        )
        # Manual tracks are not merged, so repeated phrasing survives intact.
        self.assertEqual(transcript_lines(cues)[0].count("I have so much content"), 2)


class FakeResponse:
    def __init__(self, body: str):
        self.body = body.encode()

    def read(self, size: int = -1):
        body, self.body = self.body, b""
        return body

    def close(self):
        pass


class FakeYoutubeDL:
    """Stands in for yt-dlp so the import flow runs without network access."""

    opened: list[str] = []
    info: dict = {}
    captions_vtt = "WEBVTT\n\n00:00:00.000 --> 00:00:10.000\nHello world\n"

    def __init__(self, options):
        self.params = dict(options)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        return self.info

    def urlopen(self, url):
        type(self).opened.append(url)
        return FakeResponse(self.captions_vtt)

    def process_ie_result(self, result, download=True):
        Path(self.params["outtmpl"]["default"]).write_bytes(b"audio")


class YouTubeImportFlowTests(unittest.TestCase):
    def run_import(self, automatic_captions, *, manual=True, automatic=True):
        phases = []
        FakeYoutubeDL.opened = []
        FakeYoutubeDL.info = {
            "id": "mjQlZrteMIY",
            "duration": 10,
            "title": "Example",
            "language": "en-US",
            "subtitles": {"en": [{"ext": "vtt", "url": "https://captions.invalid/manual"}]}
            if manual else {},
            "automatic_captions": {"en-orig": [{"ext": "vtt", "url": "https://captions.invalid/auto"}]}
            if automatic else {},
        }
        with patch.object(yt_dlp, "YoutubeDL", FakeYoutubeDL):
            imported = import_youtube("https://youtu.be/mjQlZrteMIY", "de", threading.Event(),
                                      lambda phase, done, total: phases.append(phase),
                                      automatic_captions=automatic_captions)
        return imported, phases

    def test_manual_captions_are_used_even_when_automatic_captions_are_disabled(self):
        imported, phases = self.run_import(automatic_captions=False)
        self.assertEqual(FakeYoutubeDL.opened, ["https://captions.invalid/manual"])
        self.assertEqual(phases, ["checking_transcript", "using_captions"])
        self.assertFalse(imported.caption_automatic)
        self.assertEqual(imported.caption_language, "en")
        self.assertIsNone(imported.audio_path)

    def test_automatic_captions_are_used_when_enabled_and_no_manual_track_exists(self):
        imported, phases = self.run_import(automatic_captions=True, manual=False)
        self.assertEqual(FakeYoutubeDL.opened, ["https://captions.invalid/auto"])
        self.assertEqual(phases[-1], "using_captions")
        self.assertTrue(imported.caption_automatic)
        self.assertEqual(imported.caption_language, "en-orig")
        self.assertIsNone(imported.audio_path)

    def test_no_caption_track_available_downloads_audio_for_local_transcription(self):
        imported, phases = self.run_import(automatic_captions=False, manual=False)
        self.assertEqual(FakeYoutubeDL.opened, [])
        self.assertEqual(phases, ["checking_transcript", "downloading_audio", "preparing_audio"])
        self.assertEqual(imported.spoken_language, "en")
        self.assertTrue(imported.audio_path.is_file())
        imported.audio_path.unlink()


if __name__ == "__main__":
    unittest.main()
