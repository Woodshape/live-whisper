import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backend


class BackendTests(unittest.TestCase):
    def test_commands_and_errors_are_line_delimited_and_correlated(self):
        responses = io.StringIO()
        backend.serve(io.StringIO('not-json\n{"id":2,"command":"status"}\n{"id":3,"command":"wrong"}\n'), responses)
        rows = [json.loads(line) for line in responses.getvalue().splitlines()]
        self.assertEqual([row['id'] for row in rows], [None, 2, 3])
        self.assertEqual([row['ok'] for row in rows], [False, True, False])
        self.assertEqual(rows[1]['data']['state'], 'idle')

    def test_desktop_file_transcription_preserves_original(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'speech.wav'
            source.write_bytes(b'not-valid-audio')
            output = Path(directory) / 'notes.txt'
            with patch.object(backend, 'Transcriber') as mocked:
                engine = mocked.return_value
                backend.handle(engine, {'command': 'start_file', 'source': str(source),
                                                   'output': str(output), 'model': 'tiny'})
                engine.start.assert_called_once_with('file', str(source), str(output), 'tiny')
                self.assertTrue(source.exists())

    def test_preload_model_command(self):
        with patch.object(backend, 'Transcriber') as mocked:
            engine = mocked.return_value
            backend.handle(engine, {'command': 'preload_model', 'model': 'base'})
            engine.preload_model.assert_called_once_with('base')

    def test_desktop_start_allows_automatic_output(self):
        with patch.object(backend, 'Transcriber') as mocked:
            engine = mocked.return_value
            backend.handle(engine, {'command': 'start_live', 'source': 'speaker.monitor', 'model': 'base'})
            engine.start.assert_called_once_with('live', 'speaker.monitor', None,
                                                 'base', live_chunk_seconds=8)

    def test_desktop_language_is_forwarded_for_live_and_file(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(backend, 'Transcriber') as mocked:
            engine = mocked.return_value
            source = Path(directory) / 'recording.wav'
            source.write_bytes(b'audio')
            backend.handle(engine, {'command': 'start_live', 'source': 'speaker.monitor',
                                    'model': 'small', 'language': 'de'})
            engine.start.assert_called_with('live', 'speaker.monitor', None, 'small',
                                           live_chunk_seconds=8, language='de')
            backend.handle(engine, {'command': 'start_file', 'source': str(source),
                                    'model': 'small', 'language': 'en'})
            engine.start.assert_called_with('file', str(source), None, 'small', language='en')

    def test_desktop_live_preset_is_passed_to_worker(self):
        with patch.object(backend, 'Transcriber') as mocked:
            engine = mocked.return_value
            backend.handle(engine, {'command': 'start_live', 'source': 'speaker.monitor',
                                    'output': '/tmp/notes.txt', 'model': 'base', 'chunk_seconds': 4})
            engine.start.assert_called_once_with('live', 'speaker.monitor', '/tmp/notes.txt',
                                                 'base', live_chunk_seconds=4)

    def test_missing_file_does_not_start(self):
        engine = backend.Transcriber()
        with self.assertRaisesRegex(ValueError, 'existing'):
            backend.handle(engine, {'command': 'start_file', 'source': '/no/such/file.wav',
                                    'output': '/tmp/out.txt', 'model': 'tiny'})
        self.assertEqual(engine.status()['state'], 'idle')


if __name__ == '__main__':
    unittest.main()
