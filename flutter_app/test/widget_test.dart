import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
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
    if (command == 'cancel_buffered') {
      status = {...status, 'cancel_requested': true};
    }
    if (command == 'output') status = {...status, 'output': args['output']};
    return status;
  }

  @override
  Future<void> close() async {}
}

Future<void> _showSettings(WidgetTester tester) async {
  await tester.ensureVisible(find.text('Settings'));
  await tester.tap(find.text('Settings'));
  await tester.pumpAndSettle();
}

Future<void> _showCapture(WidgetTester tester) async {
  await tester.ensureVisible(find.text('Capture'));
  await tester.tap(find.text('Capture'));
  await tester.pumpAndSettle();
}

void main() {
  setUp(() => SharedPreferences.setMockInitialValues({}));

  testWidgets('select source and start/stop live capture from Capture', (
    tester,
  ) async {
    final backend = FakeBackend();
    await tester.pumpWidget(LiveWhisperApp(client: backend));
    await tester.pumpAndSettle();
    await _showSettings(tester);
    expect(find.text('Default system audio'), findsOneWidget);
    await _showCapture(tester);
    expect(find.textContaining('Model ready'), findsNothing);
    expect(
      find.text('Automatic output: ~/YYYY-MM-DD_HH-MM (chosen at start)'),
      findsOneWidget,
    );
    await tester.tap(find.byKey(const Key('quick-start-button')));
    await tester.pump();
    expect(backend.commands, contains('start_live'));
    expect(backend.lastLiveArgs?['chunk_seconds'], 4);
    expect(backend.lastLiveArgs?['language'], 'de');
    expect(find.text('Running · live'), findsOneWidget);
    await tester.tap(find.byKey(const Key('quick-stop-button')));
    await tester.pump();
    expect(backend.commands, contains('stop'));
  });

  testWidgets('cancel buffered transcription after the current file chunk', (
    tester,
  ) async {
    final backend = FakeBackend();
    backend.status = {
      ...backend.status,
      'state': 'stopping',
      'mode': 'file',
      'input_source': 'file',
      'backlog_seconds': 1322.1,
      'processing_speed': 10.06,
      'cancel_requested': false,
    };
    await tester.pumpWidget(LiveWhisperApp(client: backend));
    await tester.pumpAndSettle();

    expect(find.text('Finishing buffered audio'), findsOneWidget);
    expect(
      find.text(
        'Cancel finishes the current 30-second chunk before discarding the rest.',
      ),
      findsOneWidget,
    );
    await tester.tap(find.byKey(const Key('cancel-buffered-button')));
    await tester.pump();
    expect(backend.commands, contains('cancel_buffered'));
    expect(find.text('Canceling…'), findsOneWidget);
    expect(
      find.textContaining('letting the current chunk finish'),
      findsOneWidget,
    );

    backend.status = {
      ...backend.status,
      'state': 'cancelled',
      'backlog_seconds': 1322.1,
    };
    await tester.pump(const Duration(seconds: 1));
    await tester.pump();
    expect(find.text('Canceled · buffered audio discarded'), findsOneWidget);
    expect(
      find.textContaining('remaining buffered audio discarded'),
      findsOneWidget,
    );
  });

  testWidgets('cancel is hidden while a YouTube import is still stopping', (
    tester,
  ) async {
    final backend = FakeBackend();
    backend.status = {
      ...backend.status,
      'state': 'stopping',
      'mode': 'file',
      'input_source': 'youtube',
      'transcript_source': '',
      'import_phase': 'downloading_audio',
    };
    await tester.pumpWidget(LiveWhisperApp(client: backend));
    await tester.pumpAndSettle();
    expect(find.text('Stopping YouTube import'), findsOneWidget);
    expect(find.byKey(const Key('cancel-buffered-button')), findsNothing);
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
    await tester.pumpAndSettle();
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
          .widget<FilledButton>(find.byKey(const Key('quick-start-button')))
          .onPressed,
      isNull,
    );
    expect(
      tester
          .widget<OutlinedButton>(find.byKey(const Key('quick-stop-button')))
          .onPressed,
      isNull,
    );

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
    expect(
      tester
          .widget<FilledButton>(find.byKey(const Key('quick-start-button')))
          .onPressed,
      isNotNull,
    );
  });

  testWidgets(
    'automatic output is per-session and a chosen path can be reset',
    (tester) async {
      const fileSelector = MethodChannel('plugins.flutter.io/file_selector');
      tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
        fileSelector,
        (call) async => call.method == 'getSavePath' ? '/tmp/custom.txt' : null,
      );
      addTearDown(
        () => tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
          fileSelector,
          null,
        ),
      );

      final backend = FakeBackend();
      await tester.pumpWidget(LiveWhisperApp(client: backend));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('quick-start-button')));
      await tester.pump();
      expect(backend.lastLiveArgs?['output'], '');
      expect(backend.status['output'], '/home/test/2026-09-23_09-01');
      await tester.tap(find.byKey(const Key('quick-stop-button')));
      await tester.pump();
      await tester.tap(find.byKey(const Key('quick-start-button')));
      await tester.pump();
      expect(backend.lastLiveArgs?['output'], '');
      expect(backend.status['output'], '/home/test/2026-09-23_09-02');

      await tester.tap(find.byKey(const Key('quick-stop-button')));
      await tester.pump();
      await tester.tap(find.byKey(const Key('output-pick-button')));
      await tester.pumpAndSettle();
      expect(find.text('/tmp/custom.txt'), findsOneWidget);
      await tester.tap(find.byKey(const Key('quick-start-button')));
      await tester.pump();
      expect(backend.lastLiveArgs?['output'], '/tmp/custom.txt');

      await tester.tap(find.byKey(const Key('quick-stop-button')));
      await tester.pump();
      await tester.tap(find.byKey(const Key('output-clear-button')));
      await tester.pump();
      expect(
        find.text('Automatic output: ~/YYYY-MM-DD_HH-MM (chosen at start)'),
        findsOneWidget,
      );
      await tester.tap(find.byKey(const Key('quick-start-button')));
      await tester.pump();
      expect(backend.lastLiveArgs?['output'], '');
    },
  );

  testWidgets(
    'model, language and latency persist across app restarts and are used on start',
    (tester) async {
      final backend = FakeBackend();
      await tester.pumpWidget(LiveWhisperApp(client: backend));
      await tester.pumpAndSettle();
      await _showSettings(tester);

      await tester.tap(find.text('base'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('small').last);
      await tester.pumpAndSettle();
      await tester.tap(find.text('Deutsch (de)'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('English (en)').last);
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.text('Low latency · 4s'));
      await tester.tap(find.text('Low latency · 4s'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Higher accuracy · 8s').last);
      await tester.pumpAndSettle();

      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getString('whisper_model'), 'small');
      expect(prefs.getString('transcription_language'), 'en');
      expect(prefs.getInt('live_chunk_seconds'), 8);
      await _showCapture(tester);
      await tester.tap(find.byKey(const Key('quick-start-button')));
      await tester.pump();
      expect(backend.lastLiveArgs?['model'], 'small');
      expect(backend.lastLiveArgs?['language'], 'en');
      expect(backend.lastLiveArgs?['chunk_seconds'], 8);

      await tester.pumpWidget(const SizedBox());
      final restarted = FakeBackend();
      await tester.pumpWidget(LiveWhisperApp(client: restarted));
      await tester.pumpAndSettle();
      await _showSettings(tester);
      expect(find.text('small'), findsOneWidget);
      expect(find.text('English (en)'), findsOneWidget);
      expect(find.text('Higher accuracy · 8s'), findsOneWidget);
      await _showCapture(tester);
      await tester.tap(find.byKey(const Key('quick-start-button')));
      await tester.pump();
      expect(restarted.lastLiveArgs?['model'], 'small');
      expect(restarted.lastLiveArgs?['language'], 'en');
      expect(restarted.lastLiveArgs?['chunk_seconds'], 8);
    },
  );

  testWidgets('higher accuracy preset sends eight-second chunks', (
    tester,
  ) async {
    final backend = FakeBackend();
    await tester.pumpWidget(LiveWhisperApp(client: backend));
    await tester.pumpAndSettle();
    await _showSettings(tester);
    await tester.ensureVisible(find.text('Low latency · 4s'));
    await tester.tap(find.text('Low latency · 4s'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Higher accuracy · 8s').last);
    await tester.pumpAndSettle();
    await _showCapture(tester);
    await tester.tap(find.byKey(const Key('quick-start-button')));
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
