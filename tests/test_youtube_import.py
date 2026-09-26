import unittest

from youtube_import import (
    CaptionCue,
    choose_caption_track,
    is_full_transcript,
    normalize_youtube_url,
    parse_captions,
    transcript_lines,
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

    def test_only_treats_near_full_caption_tracks_as_complete(self):
        complete = (CaptionCue(0, 5, "start"), CaptionCue(95, 100, "end"))
        partial = (CaptionCue(0, 5, "start"), CaptionCue(70, 75, "partial"))
        self.assertTrue(is_full_transcript(complete, 100))
        self.assertFalse(is_full_transcript(partial, 100))
        self.assertFalse(is_full_transcript(complete, 0))

    def test_selects_original_spoken_language_caption_over_translation(self):
        info = {
            "language": "en-US",
            "subtitles": {},
            "automatic_captions": {
                "de": [{"ext": "vtt", "url": "https://captions.invalid/de"}],
                "en-orig": [{"ext": "vtt", "url": "https://captions.invalid/en"}],
            },
        }
        language, track = choose_caption_track(info, "de")
        self.assertEqual(language, "en-orig")
        self.assertEqual(track["url"], "https://captions.invalid/en")

    def test_prefers_manual_captions_when_the_language_matches(self):
        info = {
            "language": "en",
            "subtitles": {"en": [{"ext": "vtt", "url": "https://captions.invalid/manual"}]},
            "automatic_captions": {"en": [{"ext": "vtt", "url": "https://captions.invalid/auto"}]},
        }
        language, track = choose_caption_track(info, "en")
        self.assertEqual(language, "en")
        self.assertEqual(track["url"], "https://captions.invalid/manual")


if __name__ == "__main__":
    unittest.main()
