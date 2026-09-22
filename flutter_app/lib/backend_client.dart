import 'dart:async';
import 'dart:convert';
import 'dart:io';

abstract class TranscriptionClient {
  Future<Map<String, dynamic>> request(
    String command, [
    Map<String, dynamic> args = const {},
  ]);
  Future<void> close();
}

/// Local, line-oriented IPC; never opens a network listener or a browser.
class DesktopBackend implements TranscriptionClient {
  DesktopBackend._(this.process) {
    process.stdout
        .transform(utf8.decoder)
        .transform(const LineSplitter())
        .listen(
          (line) {
            try {
              final response = jsonDecode(line) as Map<String, dynamic>;
              final id = response['id'] as int?;
              final pending = _pending.remove(id);
              if (pending == null) return;
              if (response['ok'] == true) {
                pending.complete(response['data'] as Map<String, dynamic>);
              } else {
                pending.completeError(StateError('${response['error']}'));
              }
            } catch (error) {
              // The worker should only emit JSON. Fail pending calls if it doesn't.
              for (final call in _pending.values) {
                call.completeError(error);
              }
              _pending.clear();
            }
          },
          onDone: () {
            for (final call in _pending.values) {
              call.completeError(
                StateError('Transcription worker exited. Restart the app.'),
              );
            }
            _pending.clear();
          },
        );
    process.stderr.transform(utf8.decoder).listen((text) {
      stderr.write(text);
    });
  }

  final Process process;
  final Map<int, Completer<Map<String, dynamic>>> _pending = {};
  int _nextId = 0;

  static Future<DesktopBackend> launch() async {
    final configuredRoot = Platform.environment['LIVE_WHISPER_PROJECT_DIR'];
    final current = Directory.current;
    final root = configuredRoot != null
        ? Directory(configuredRoot)
        : File('${current.path}/backend.py').existsSync()
        ? current
        : current.parent;
    if (!File('${root.path}/backend.py').existsSync()) {
      throw StateError(
        'Cannot find backend.py. Launch from live-whisper/flutter_app.',
      );
    }
    final python =
        Platform.environment['LIVE_WHISPER_PYTHON'] ??
        '${root.path}/.venv/bin/python';
    if (!File(python).existsSync()) {
      throw StateError(
        'Python environment missing. Run: cd ${root.path} && uv sync --python 3.13',
      );
    }
    final process = await Process.start(python, [
      '-u',
      '${root.path}/backend.py',
    ], workingDirectory: root.path);
    return DesktopBackend._(process);
  }

  @override
  Future<Map<String, dynamic>> request(
    String command, [
    Map<String, dynamic> args = const {},
  ]) {
    final id = ++_nextId;
    final completer = Completer<Map<String, dynamic>>();
    _pending[id] = completer;
    try {
      process.stdin.writeln(
        jsonEncode({'id': id, 'command': command, ...args}),
      );
    } catch (error) {
      _pending.remove(id);
      completer.completeError(error);
    }
    return completer.future;
  }

  @override
  Future<void> close() async {
    await process.stdin.close(); // EOF stops capture via backend.serve().
  }
}
