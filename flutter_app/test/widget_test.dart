import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:live_whisper/backend_client.dart';
import 'package:live_whisper/main.dart';

class FakeBackend implements TranscriptionClient {
  final commands = <String>[];
  Map<String, dynamic>? lastLiveArgs;
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
    if (command == 'start_live') {
      lastLiveArgs = args;
      status = {
        ...status,
        'state': 'running',
        'mode': 'live',
        'output': args['output'],
        'live_chunk_seconds': args['chunk_seconds'],
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
  testWidgets('select source, output and start/stop live capture', (
    tester,
  ) async {
    final backend = FakeBackend();
    await tester.pumpWidget(LiveWhisperApp(client: backend));
    await tester.pump();
    expect(find.text('Default system audio'), findsOneWidget);
    final field = find.byType(TextField);
    await tester.enterText(field, '/tmp/transcript.txt');
    await tester.pump();
    await tester.ensureVisible(find.text('Start live transcription'));
    await tester.pump();
    await tester.tap(find.text('Start live transcription'));
    await tester.pump();
    expect(backend.commands, contains('start_live'));
    expect(backend.lastLiveArgs?['chunk_seconds'], 4);
    expect(find.text('Running · live'), findsOneWidget);
    await tester.ensureVisible(find.text('Stop'));
    await tester.pump();
    await tester.tap(find.text('Stop'));
    await tester.pump();
    expect(backend.commands, contains('stop'));
  });

  testWidgets('higher accuracy preset sends eight-second chunks', (
    tester,
  ) async {
    final backend = FakeBackend();
    await tester.pumpWidget(LiveWhisperApp(client: backend));
    await tester.pump();
    await tester.enterText(find.byType(TextField), '/tmp/transcript.txt');
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
