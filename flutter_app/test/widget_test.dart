import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:live_whisper/backend_client.dart';
import 'package:live_whisper/main.dart';
import 'package:shared_preferences/shared_preferences.dart';

class FakeBackend implements TranscriptionClient {
  final commands = <String>[];
  Map<String, dynamic>? lastLiveArgs;
  Map<String, dynamic>? lastYoutubeArgs;
  int sessions = 0;
  Map<String, dynamic> status = {
    'state': 'idle',
    'mode': '',
    'output': '',
    'lines': 0,
    'captured_seconds': 0,
    'backlog_seconds': 0,
    'processing_speed': null,
    'audio_detected': false,
    'recent_lines': <String>[],
  };

  @override
  Future<Map<String, dynamic>> request(
    String command, [
    Map<String, dynamic> args = const {},
  ]) async {
    commands.add(command);
    if (command == 'devices') {
      return {
        'devices': [
          {'id': 'speaker.monitor', 'label': 'Default system audio'},
        ],
      };
    }
    if (command == 'preload_model') {
      status = {
        ...status,
        'state': 'preloading',
        'model': args['model'],
        'model_phase': 'checking',
      };
    }
    if (command == 'start_live') {
      lastLiveArgs = args;
      sessions++;
      status = {
        ...status,
        'state': 'running',
        'mode': 'live',
        'output': args['output'] == ''
            ? '/home/test/2026-09-23_09-${sessions.toString().padLeft(2, '0')}'
            : args['output'],
        'live_chunk_seconds': args['chunk_seconds'],
      };
    }
    if (command == 'start_youtube') {
      lastYoutubeArgs = args;
      status = {
        ...status,
        'state': 'importing',
        'mode': 'file',
        'input_source': 'youtube',
        'import_phase': 'checking_transcript',
      };
    }
    if (command == 'stop') status = {...status, 'state': 'stopped'};
    if (command == 'output') status = {...status, 'output': args['output']};
    return status;
  }

  @override
  Future<void> close() async {}
}

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  testWidgets('select source, output and start/stop live capture', (
    tester,
  ) async {
    final backend = FakeBackend();
    await tester.pumpWidget(LiveWhisperApp(client: backend));
    await tester.pump();
    expect(find.text('Default system audio'), findsOneWidget);
    expect(find.textContaining('Model ready'), findsNothing);
    expect(
      find.text('Automatic output: ~/YYYY-MM-DD_HH-MM (chosen at start)'),
      findsOneWidget,
    );
    final field = find.byKey(const Key('output-path-field'));
    await tester.enterText(field, '/tmp/transcript.txt');
    await tester.pump();
    await tester.ensureVisible(find.text('Start live transcription'));
    await tester.pump();
    await tester.tap(find.text('Start live transcription'));
    await tester.pump();
    expect(backend.commands, contains('start_live'));
    expect(backend.lastLiveArgs?['chunk_seconds'], 4);
    expect(backend.lastLiveArgs?['language'], 'de');
    expect(find.text('Running · live'), findsOneWidget);
    await tester.ensureVisible(find.text('Stop'));
    await tester.pump();
    await tester.tap(find.text('Stop'));
    await tester.pump();
    expect(backend.commands, contains('stop'));
  });

  testWidgets('imports a YouTube URL and starts captions-first transcription', (
    tester,
  ) async {
    final backend = FakeBackend();
    await tester.pumpWidget(LiveWhisperApp(client: backend));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 20));
    final field = find.byKey(const Key('youtube-url-field'));
    await tester.ensureVisible(field);
    await tester.enterText(field, 'https://youtu.be/mjQlZrteMIY');
    await tester.pump();
    await tester.ensureVisible(find.text('Check captions & transcribe'));
    await tester.tap(find.text('Check captions & transcribe'));
    await tester.pump();
    expect(backend.commands, contains('start_youtube'));
    expect(backend.lastYoutubeArgs?['url'], 'https://youtu.be/mjQlZrteMIY');
    expect(backend.lastYoutubeArgs?['language'], 'de');
    expect(backend.lastYoutubeArgs?['automatic_captions'], true);
    expect(find.text('Importing YouTube video'), findsOneWidget);
  });

  testWidgets('captions checkbox disables the automatic-caption fallback', (
    tester,
  ) async {
    final backend = FakeBackend();
    await tester.pumpWidget(LiveWhisperApp(client: backend));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 20));
    final field = find.byKey(const Key('youtube-url-field'));
    await tester.ensureVisible(field);
    await tester.enterText(field, 'https://youtu.be/mjQlZrteMIY');
    await tester.pump();
    final toggle = find.byKey(const Key('youtube-captions-checkbox'));
    await tester.ensureVisible(toggle);
    await tester.pump();
    await tester.tap(toggle);
    await tester.pump();
    expect(find.text('Check captions & transcribe'), findsOneWidget);
    await tester.tap(find.text('Check captions & transcribe'));
    await tester.pump();
    expect(backend.lastYoutubeArgs?['automatic_captions'], false);
  });

  testWidgets('pre-load button warms selected model before capture', (
    tester,
  ) async {
    final backend = FakeBackend();
    await tester.pumpWidget(LiveWhisperApp(client: backend));
    await tester.pump();
    await tester.ensureVisible(find.text('Pre-load model'));
    await tester.pump();
    await tester.tap(find.text('Pre-load model'));
    await tester.pump();
    expect(backend.commands, contains('preload_model'));
    expect(backend.status['model'], 'base');
    expect(find.text('Pre-loading · base model'), findsOneWidget);
    expect(
      find.textContaining('no audio is being recorded yet'),
      findsOneWidget,
    );
    expect(
      tester
          .widget<FilledButton>(
            find.widgetWithText(FilledButton, 'Start live transcription'),
          )
          .onPressed,
      isNull,
    );
    expect(find.text('Stop'), findsNothing);

    backend.status = {
      ...backend.status,
      'state': 'idle',
      'model_phase': null,
      'ready_model': 'base',
    };
    await tester.pump(const Duration(seconds: 1));
    await tester.pump();
    expect(find.text('Ready · base model warmed'), findsOneWidget);
    expect(find.text('Model ready in memory'), findsOneWidget);
    await tester.ensureVisible(find.text('Start live transcription'));
    await tester.pump();
    expect(
      tester
          .widget<FilledButton>(
            find.widgetWithText(FilledButton, 'Start live transcription'),
          )
          .onPressed,
      isNotNull,
    );
  });

  testWidgets(
    'blank output stays automatic across sessions; explicit path overrides',
    (tester) async {
      final backend = FakeBackend();
      await tester.pumpWidget(LiveWhisperApp(client: backend));
      await tester.pump();
      final output = find.byKey(const Key('output-path-field'));
      await tester.ensureVisible(find.text('Start live transcription'));
      await tester.pump();
      await tester.tap(find.text('Start live transcription'));
      await tester.pump();
      expect(backend.lastLiveArgs?['output'], '');
      expect(backend.status['output'], '/home/test/2026-09-23_09-01');
      await tester.pump(const Duration(seconds: 1));
      expect(tester.widget<TextField>(output).controller!.text, '');
      await tester.ensureVisible(find.text('Stop'));
      await tester.tap(find.text('Stop'));
      await tester.pump();
      await tester.ensureVisible(find.text('Start live transcription'));
      await tester.tap(find.text('Start live transcription'));
      await tester.pump();
      expect(backend.lastLiveArgs?['output'], '');
      expect(backend.status['output'], '/home/test/2026-09-23_09-02');

      await tester.ensureVisible(find.text('Stop'));
      await tester.tap(find.text('Stop'));
      await tester.pump();
      await tester.enterText(output, '/tmp/custom.txt');
      await tester.ensureVisible(find.text('Start live transcription'));
      await tester.tap(find.text('Start live transcription'));
      await tester.pump();
      expect(backend.lastLiveArgs?['output'], '/tmp/custom.txt');

      await tester.ensureVisible(find.text('Stop'));
      await tester.tap(find.text('Stop'));
      await tester.pump();
      await tester.enterText(output, '');
      await tester.ensureVisible(find.text('Start live transcription'));
      await tester.tap(find.text('Start live transcription'));
      await tester.pump();
      expect(backend.lastLiveArgs?['output'], '');
    },
  );

  testWidgets(
    'language selection persists across app restarts and is sent on start',
    (tester) async {
      final backend = FakeBackend();
      await tester.pumpWidget(LiveWhisperApp(client: backend));
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.text('Deutsch (de)'));
      await tester.pump();
      await tester.tap(find.text('Deutsch (de)').first);
      await tester.pumpAndSettle();
      await tester.tap(find.text('English (en)').last);
      await tester.pumpAndSettle();
      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getString('transcription_language'), 'en');
      await tester.ensureVisible(find.text('Start live transcription'));
      await tester.pump();
      await tester.tap(find.text('Start live transcription'));
      await tester.pump();
      expect(backend.lastLiveArgs?['language'], 'en');

      await tester.pumpWidget(const SizedBox());
      final restarted = FakeBackend();
      await tester.pumpWidget(LiveWhisperApp(client: restarted));
      await tester.pumpAndSettle();
      expect(find.text('English (en)'), findsOneWidget);
      await tester.ensureVisible(find.text('Start live transcription'));
      await tester.pump();
      await tester.tap(find.text('Start live transcription'));
      await tester.pump();
      expect(restarted.lastLiveArgs?['language'], 'en');
    },
  );

  testWidgets('higher accuracy preset sends eight-second chunks', (
    tester,
  ) async {
    final backend = FakeBackend();
    await tester.pumpWidget(LiveWhisperApp(client: backend));
    await tester.pump();
    await tester.enterText(
      find.byKey(const Key('output-path-field')),
      '/tmp/transcript.txt',
    );
    await tester.ensureVisible(find.text('Start live transcription'));
    await tester.pump();
    await tester.ensureVisible(find.text('Low latency · 4s'));
    await tester.pump();
    await tester.tap(find.text('Low latency · 4s'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Higher accuracy · 8s').last);
    await tester.pumpAndSettle();
    await tester.tap(find.text('Start live transcription'));
    await tester.pump();
    expect(backend.lastLiveArgs?['chunk_seconds'], 8);
  });

  testWidgets(
    'shows saved transcript lines in reverse order and updates live',
    (tester) async {
      final backend = FakeBackend();
      backend.status = {
        ...backend.status,
        'state': 'running',
        'mode': 'live',
        'recent_lines': <String>[
          '[00:00:00] First line',
          '[00:00:08] Newest line',
        ],
      };
      await tester.pumpWidget(LiveWhisperApp(client: backend));
      await tester.pump();
      expect(find.text('[00:00:08] Newest line'), findsOneWidget);
      expect(find.text('[00:00:00] First line'), findsOneWidget);
      expect(find.text('Transcribed lines will appear here.'), findsNothing);
      backend.status = {
        ...backend.status,
        'recent_lines': <String>[
          '[00:00:00] First line',
          '[00:00:08] Newest line',
          '[00:00:16] Third line',
        ],
      };
      await tester.pump(const Duration(seconds: 1));
      await tester.pump();
      expect(find.text('[00:00:16] Third line'), findsOneWidget);
    },
  );

  testWidgets(
    'distinguishes model download from speech and shows download ETA',
    (tester) async {
      final backend = FakeBackend();
      backend.status = {
        ...backend.status,
        'state': 'capturing',
        'mode': 'live',
        'audio_detected': true,
        'captured_seconds': 47,
        'model_phase': 'downloading',
        'model_download_bytes': 26214400,
        'model_download_total': 104857600,
        'model_download_eta_seconds': 18,
      };
      await tester.pumpWidget(LiveWhisperApp(client: backend));
      await tester.pump();
      expect(find.text('Capturing · downloading model'), findsOneWidget);
      expect(find.textContaining('25%'), findsOneWidget);
      expect(find.textContaining('~18s download remaining'), findsOneWidget);
      expect(
        find.textContaining('initialization follows (time unknown)'),
        findsOneWidget,
      );
      expect(
        find.textContaining('Audio is being buffered from the start'),
        findsOneWidget,
      );
      expect(find.textContaining('waiting for speech or model'), findsNothing);

      backend.status = {...backend.status, 'model_phase': 'initializing'};
      await tester.pump(const Duration(seconds: 1));
      await tester.pump();
      expect(find.textContaining('Initializing model'), findsOneWidget);
      expect(find.textContaining('ETA unknown on first load'), findsOneWidget);

      backend.status = {
        ...backend.status,
        'state': 'running',
        'model_phase': null,
      };
      await tester.pump(const Duration(seconds: 1));
      await tester.pump();
      expect(
        find.textContaining(
          'Model ready · waiting for first processed speech chunk',
        ),
        findsOneWidget,
      );
      expect(
        find.textContaining('Audio is being buffered from the start'),
        findsNothing,
      );
    },
  );

  testWidgets('file backlog shows an estimated remaining time', (tester) async {
    final backend = FakeBackend();
    backend.status = {
      ...backend.status,
      'state': 'running',
      'mode': 'file',
      'processing_speed': 8.35,
      'backlog_seconds': 6955.7,
    };
    await tester.pumpWidget(LiveWhisperApp(client: backend));
    await tester.pump();
    expect(find.textContaining('6955.7s audio waiting'), findsOneWidget);
    expect(find.textContaining('~14 min remaining'), findsOneWidget);

    backend.status = {...backend.status, 'backlog_seconds': 20.0};
    await tester.pump(const Duration(seconds: 1));
    await tester.pump();
    expect(find.textContaining('~3s remaining'), findsOneWidget);

    backend.status = {...backend.status, 'mode': 'live'};
    await tester.pump(const Duration(seconds: 1));
    await tester.pump();
    expect(find.textContaining('remaining'), findsNothing);

    backend.status = {
      ...backend.status,
      'mode': 'file',
      'processing_speed': null,
    };
    await tester.pump(const Duration(seconds: 1));
    await tester.pump();
    expect(find.textContaining('remaining'), findsNothing);

    backend.status = {
      ...backend.status,
      'processing_speed': 8.35,
      'state': 'finished',
    };
    await tester.pump(const Duration(seconds: 1));
    await tester.pump();
    expect(find.textContaining('remaining'), findsNothing);
  });

  testWidgets('shows warning for a model slower than real time', (
    tester,
  ) async {
    final backend = FakeBackend();
    backend.status = {
      ...backend.status,
      'state': 'running',
      'mode': 'live',
      'processing_speed': 0.65,
      'backlog_seconds': 20.0,
      'captured_seconds': 40,
    };
    await tester.pumpWidget(LiveWhisperApp(client: backend));
    await tester.pump();
    expect(find.textContaining('0.65× real time'), findsOneWidget);
    expect(find.textContaining('Falling behind'), findsOneWidget);
  });
}
