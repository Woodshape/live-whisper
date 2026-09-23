import 'dart:async';

import 'package:file_selector/file_selector.dart';
import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'backend_client.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  try {
    final backend = await DesktopBackend.launch();
    runApp(LiveWhisperApp(client: backend));
  } catch (error) {
    runApp(
      MaterialApp(
        home: Scaffold(
          body: Center(
            child: SelectableText('Cannot start Live Whisper\n\n$error'),
          ),
        ),
      ),
    );
  }
}

class LiveWhisperApp extends StatelessWidget {
  const LiveWhisperApp({super.key, required this.client});
  final TranscriptionClient client;

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Live Whisper',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        useMaterial3: true,
        colorScheme: ColorScheme.fromSeed(
          seedColor: const Color(0xff8c9bff),
          brightness: Brightness.dark,
        ),
        scaffoldBackgroundColor: const Color(0xff11141c),
        cardTheme: CardThemeData(
          color: const Color(0xff1d2330),
          margin: EdgeInsets.zero,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(20),
          ),
        ),
        inputDecorationTheme: const InputDecorationTheme(
          border: OutlineInputBorder(),
        ),
      ),
      home: TranscriptionScreen(client: client),
    );
  }
}

class TranscriptionScreen extends StatefulWidget {
  const TranscriptionScreen({super.key, required this.client});
  final TranscriptionClient client;

  @override
  State<TranscriptionScreen> createState() => _TranscriptionScreenState();
}

class _TranscriptionScreenState extends State<TranscriptionScreen> {
  final _output = TextEditingController();
  Timer? _poller;
  Map<String, dynamic> _status = const {
    'state': 'idle',
    'lines': 0,
    'captured_seconds': 0,
  };
  List<Map<String, dynamic>> _devices = [];
  String? _source;
  String? _file;
  String _model = 'base';
  String _language = 'de';
  bool _languageReady = false;
  int _liveChunkSeconds = 4;
  String? _error;
  bool _pending = false;
  bool _polling = false;

  @override
  void initState() {
    super.initState();
    unawaited(_refreshDevices());
    unawaited(_refreshStatus());
    unawaited(_restoreLanguage());
    _poller = Timer.periodic(
      const Duration(seconds: 1),
      (_) => unawaited(_refreshStatus()),
    );
  }

  @override
  void dispose() {
    _poller?.cancel();
    _output.dispose();
    unawaited(widget.client.close());
    super.dispose();
  }

  Future<void> _restoreLanguage() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      if (!mounted) return;
      setState(() {
        _language = prefs.getString('transcription_language') == 'en'
            ? 'en'
            : 'de';
        _languageReady = true;
      });
    } catch (error) {
      if (mounted) {
        setState(() {
          _languageReady = true;
          _error = 'Cannot load language preference: $error';
        });
      }
    }
  }

  Future<void> _changeLanguage(String language) async {
    setState(() => _language = language);
    try {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setString('transcription_language', language);
    } catch (error) {
      if (mounted) {
        setState(() => _error = 'Cannot save language preference: $error');
      }
    }
  }

  bool get _busy => [
    'uploading',
    'preloading',
    'loading',
    'capturing',
    'running',
    'stopping',
  ].contains(_status['state']);

  Future<void> _refreshStatus() async {
    if (_polling) return;
    _polling = true;
    try {
      final status = await widget.client.request('status');
      if (!mounted) return;
      setState(() {
        _status = status;
        if (status['error'] is String &&
            (status['error'] as String).isNotEmpty) {
          _error = status['error'] as String;
        }
      });
    } catch (error) {
      if (mounted) setState(() => _error = '$error');
    } finally {
      _polling = false;
    }
  }

  Future<void> _refreshDevices() async {
    try {
      final response = await widget.client.request('devices');
      final items = (response['devices'] as List).cast<Map<String, dynamic>>();
      if (!mounted) return;
      setState(() {
        _devices = items;
        if (!items.any((device) => device['id'] == _source)) {
          _source = items.isEmpty ? null : items.first['id'] as String;
        }
      });
    } catch (error) {
      if (mounted) setState(() => _error = '$error');
    }
  }

  Future<void> _command(
    String name, [
    Map<String, dynamic> args = const {},
  ]) async {
    if (_pending) return;
    setState(() {
      _pending = true;
      _error = null;
    });
    try {
      final status = await widget.client.request(name, args);
      if (!mounted) return;
      setState(() {
        _status = status;
        if (name == 'output') _output.text = status['output'] as String;
      });
    } catch (error) {
      if (mounted) setState(() => _error = '$error');
    } finally {
      if (mounted) setState(() => _pending = false);
    }
  }

  Future<void> _pickOutput() async {
    try {
      final location = await getSaveLocation(suggestedName: 'transcript.txt');
      if (mounted && location != null) {
        setState(() => _output.text = location.path);
      }
    } catch (error) {
      if (mounted) setState(() => _error = '$error');
    }
  }

  Future<void> _pickFile() async {
    try {
      final selected = await openFile();
      if (mounted && selected != null) setState(() => _file = selected.path);
    } catch (error) {
      if (mounted) setState(() => _error = '$error');
    }
  }

  @override
  Widget build(BuildContext context) {
    final state = _status['state'] as String? ?? 'idle';
    final captured = (_status['captured_seconds'] as num? ?? 0).floor();
    final lines = _status['lines'] as num? ?? 0;
    final speed = _status['processing_speed'] as num?;
    final backlog = _status['backlog_seconds'] as num? ?? 0;
    final modelPhase = _status['model_phase'] as String?;
    final modelReady = _status['ready_model'] == _model;
    final downloadDone = _status['model_download_bytes'] as num? ?? 0;
    final downloadTotal = _status['model_download_total'] as num? ?? 0;
    final downloadEta = _status['model_download_eta_seconds'] as num?;
    final loadElapsed = _status['model_load_elapsed_seconds'] as num? ?? 0;
    final downloadText = downloadTotal > 0
        ? 'Model download: ${(100 * downloadDone / downloadTotal).clamp(0, 100).toStringAsFixed(0)}% · '
              '${(downloadDone / 1048576).toStringAsFixed(0)} / ${(downloadTotal / 1048576).toStringAsFixed(0)} MiB'
              '${downloadEta == null ? ' · calculating download ETA' : ' · ~${downloadEta.ceil()}s download remaining'}'
              ' · initialization follows (time unknown)'
        : 'Checking model cache · ${loadElapsed}s elapsed; download ETA not available yet';
    // For files the decoder can fill the buffer immediately; divide queued audio
    // by recent inference speed to estimate how long processing it will take.
    // Live capture has no fixed endpoint, so it cannot have a completion ETA.
    final int? etaSeconds =
        _status['mode'] == 'file' &&
            (state == 'running' || state == 'stopping') &&
            speed != null &&
            speed.isFinite &&
            speed > 0 &&
            backlog > 0
        ? (backlog / speed).ceil()
        : null;
    final etaText = etaSeconds == null
        ? ''
        : etaSeconds < 60
        ? '   ·   ~${etaSeconds}s remaining'
        : '   ·   ~${(etaSeconds / 60).ceil()} min remaining';
    final speedLabel = switch (modelPhase) {
      'downloading' => downloadText,
      'initializing' =>
        'Initializing model · ${loadElapsed}s elapsed · ETA unknown on first load',
      'checking' =>
        'Checking model cache · ${loadElapsed}s elapsed; download ETA not available yet',
      _
          when speed == null &&
              (state == 'running' || state == 'stopping') &&
              _status['audio_detected'] == true =>
        'Model ready · waiting for first processed speech chunk',
      _ when speed == null && (state == 'running' || state == 'stopping') =>
        'Model ready · waiting for speech',
      _ when speed == null =>
        'Transcription speed: available after speech is processed',
      _ =>
        '${speed.toStringAsFixed(2)}× real time   ·   ${backlog.toStringAsFixed(1)}s audio waiting$etaText',
    };
    final recentLines = (_status['recent_lines'] as List? ?? const [])
        .cast<String>()
        .reversed
        .take(10)
        .toList();
    final fallingBehind =
        state == 'running' &&
        _status['mode'] == 'live' &&
        speed != null &&
        speed < 1;
    final color = fallingBehind
        ? const Color(0xffffb4ac)
        : const Color(0xff9fe4c2);
    final statusLabel = switch (state) {
      'capturing' =>
        modelPhase == null
            ? 'Capturing · model ready'
            : 'Capturing · ${modelPhase == 'downloading' ? 'downloading model' : 'loading model'}',
      'running' => 'Running · ${_status['mode']}',
      'stopping' => 'Finishing buffered audio',
      'loading' => 'Starting',
      'preloading' => 'Pre-loading · ${_status['model']} model',
      'idle' when modelReady => 'Ready · $_model model warmed',
      'error' => 'Error',
      _ => '${state[0].toUpperCase()}${state.substring(1)}',
    };
    final content = ConstrainedBox(
      constraints: const BoxConstraints(maxWidth: 840),
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            const Text(
              'LIVE WHISPER',
              style: TextStyle(
                letterSpacing: 3,
                color: Color(0xffa8b5ff),
                fontWeight: FontWeight.w700,
              ),
            ),
            const SizedBox(height: 8),
            Text(
              'Your words, captured locally.',
              style: Theme.of(context).textTheme.headlineMedium
                  ?.copyWith(fontWeight: FontWeight.w700),
            ),
            const SizedBox(height: 24),
            Card(
              child: Padding(
                padding: const EdgeInsets.all(24),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        Icon(Icons.circle, color: color, size: 13),
                        const SizedBox(width: 10),
                        Expanded(
                          child: Text(
                            statusLabel,
                            style: Theme.of(context).textTheme.titleLarge
                                ?.copyWith(
                                  color: color,
                                  fontWeight: FontWeight.w700,
                                ),
                          ),
                        ),
                        const SizedBox(width: 8),
                        if (_busy && state != 'preloading')
                          OutlinedButton.icon(
                            onPressed: state == 'stopping' || _pending
                                ? null
                                : () => _command('stop'),
                            icon: const Icon(Icons.stop_circle_outlined),
                            label: const Text('Stop'),
                          ),
                      ],
                    ),
                    const SizedBox(height: 16),
                    Text(
                      state == 'preloading'
                          ? 'Preparing the model before capture; no audio is being recorded yet.'
                          : '$lines transcript lines  ·  ${captured}s captured  ·  ${_status['audio_detected'] == true ? 'Audio detected' : 'Waiting for audio'}',
                      style: Theme.of(context).textTheme.bodyLarge,
                    ),
                    const SizedBox(height: 10),
                    SelectableText(
                      (_status['output'] as String?)?.isNotEmpty == true
                          ? _status['output'] as String
                          : 'Automatic output: ~/YYYY-MM-DD_HH-MM (chosen at start)',
                      style: const TextStyle(color: Color(0xffadb8ca)),
                    ),
                    const Divider(height: 30),
                    Text(
                      speedLabel,
                      style: Theme.of(context).textTheme.titleMedium?.copyWith(
                        color: fallingBehind
                            ? const Color(0xffffb4ac)
                            : const Color(0xffc4cdff),
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    if (modelPhase != null &&
                        state != 'preloading' &&
                        _status['mode'] == 'live')
                      const Padding(
                        padding: EdgeInsets.only(top: 6),
                        child: Text(
                          'Audio is being buffered from the start; transcription will catch up when the model is ready.',
                        ),
                      ),
                    if (_status['mode'] == 'live')
                      Padding(
                        padding: const EdgeInsets.only(top: 6),
                        child: Text(
                          'Active live preset: ${_status['live_chunk_seconds'] == 4 ? 'Low latency · 4s' : 'Higher accuracy · 8s'}',
                        ),
                      ),
                    if (fallingBehind)
                      const Padding(
                        padding: EdgeInsets.only(top: 6),
                        child: Text(
                          'Falling behind. Audio is buffered; try a smaller model next session.',
                        ),
                      ),
                    if (_error != null)
                      Padding(
                        padding: const EdgeInsets.only(top: 12),
                        child: SelectableText(
                          _error!,
                          style: const TextStyle(color: Color(0xffffb4ac)),
                        ),
                      ),
                    const Divider(height: 36),
                    Row(
                      children: [
                        const Icon(
                          Icons.notes_outlined,
                          size: 20,
                          color: Color(0xffa8b5ff),
                        ),
                        const SizedBox(width: 9),
                        Text(
                          'Recent transcript',
                          style: Theme.of(context).textTheme.titleMedium,
                        ),
                        const Spacer(),
                        const Text(
                          'Newest first',
                          style: TextStyle(color: Color(0xffadb8ca)),
                        ),
                      ],
                    ),
                    const SizedBox(height: 10),
                    if (recentLines.isEmpty)
                      const Text(
                        'Transcribed lines will appear here.',
                        style: TextStyle(color: Color(0xffadb8ca)),
                      )
                    else
                      Container(
                        width: double.infinity,
                        constraints: const BoxConstraints(maxHeight: 260),
                        decoration: BoxDecoration(
                          color: const Color(0xff141b27),
                          borderRadius: BorderRadius.circular(12),
                        ),
                        child: SingleChildScrollView(
                          padding: const EdgeInsets.symmetric(
                            horizontal: 16,
                            vertical: 8,
                          ),
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              for (final line in recentLines)
                                Padding(
                                  padding: const EdgeInsets.symmetric(
                                    vertical: 7,
                                  ),
                                  child: SelectableText(line),
                                ),
                            ],
                          ),
                        ),
                      ),
                  ],
                ),
              ),
            ),
            const SizedBox(height: 18),
            Card(
              child: Padding(
                padding: const EdgeInsets.all(24),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      'Destination & model',
                      style: Theme.of(context).textTheme.titleLarge,
                    ),
                    const SizedBox(height: 16),
                    TextField(
                      controller: _output,
                      onChanged: (_) => setState(() {}),
                      decoration: InputDecoration(
                        labelText: 'Output text file (optional)',
                        hintText: 'Automatic: ~/YYYY-MM-DD_HH-MM',
                        suffixIcon: IconButton(
                          onPressed: _pickOutput,
                          tooltip: 'Choose output file',
                          icon: const Icon(Icons.folder_open_outlined),
                        ),
                      ),
                    ),
                    const SizedBox(height: 6),
                    const Text(
                      'Leave blank for a new timestamped file each session. Enter or choose a path to override; clear it to restore the default.',
                      style: TextStyle(color: Color(0xffadb8ca)),
                    ),
                    const SizedBox(height: 10),
                    OutlinedButton.icon(
                      onPressed: _pending || _output.text.trim().isEmpty
                          ? null
                          : () => _command('output', {'output': _output.text}),
                      icon: const Icon(Icons.save_outlined),
                      label: const Text('Set / change output'),
                    ),
                    const SizedBox(height: 12),
                    DropdownButtonFormField<String>(
                      initialValue: _model,
                      decoration: const InputDecoration(
                        labelText: 'Whisper model',
                      ),
                      items: ['tiny', 'base', 'small', 'medium', 'large-v3']
                          .map(
                            (model) => DropdownMenuItem(
                              value: model,
                              child: Text(model),
                            ),
                          )
                          .toList(),
                      onChanged: _busy || _pending
                          ? null
                          : (model) => setState(() => _model = model!),
                    ),
                    const SizedBox(height: 12),
                    DropdownButtonFormField<String>(
                      key: ValueKey('language-$_language'),
                      initialValue: _language,
                      decoration: const InputDecoration(
                        labelText: 'Language / Sprache',
                      ),
                      items: const [
                        DropdownMenuItem(
                          value: 'de',
                          child: Text('Deutsch (de)'),
                        ),
                        DropdownMenuItem(
                          value: 'en',
                          child: Text('English (en)'),
                        ),
                      ],
                      onChanged: _busy || _pending || !_languageReady
                          ? null
                          : (language) => unawaited(_changeLanguage(language!)),
                    ),
                    const SizedBox(height: 8),
                    const Text(
                      'Fixed for each session; saved for the next app launch. Select the main spoken language.',
                      style: TextStyle(color: Color(0xffadb8ca)),
                    ),
                    const SizedBox(height: 10),
                    OutlinedButton.icon(
                      onPressed: _busy || _pending || modelReady
                          ? null
                          : () => _command('preload_model', {'model': _model}),
                      icon: const Icon(Icons.memory),
                      label: Text(
                        modelReady
                            ? 'Model ready in memory'
                            : state == 'preloading'
                            ? 'Pre-loading model…'
                            : 'Pre-load model',
                      ),
                    ),
                    const SizedBox(height: 8),
                    const Text(
                      'Optional. Start loads the model automatically if needed. A pre-loaded model skips that wait; the first transcript still takes an audio chunk plus inference.',
                      style: TextStyle(color: Color(0xffadb8ca)),
                    ),
                  ],
                ),
              ),
            ),
            const SizedBox(height: 18),
            Card(
              child: Padding(
                padding: const EdgeInsets.all(24),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      'Live audio',
                      style: Theme.of(context).textTheme.titleLarge,
                    ),
                    const SizedBox(height: 8),
                    const Text(
                      'Choose the default system-audio monitor, a microphone, or BlackHole on macOS.',
                    ),
                    const SizedBox(height: 16),
                    DropdownButtonFormField<int>(
                      initialValue: _liveChunkSeconds,
                      decoration: const InputDecoration(
                        labelText: 'Latency / accuracy',
                      ),
                      items: const [
                        DropdownMenuItem(
                          value: 4,
                          child: Text('Low latency · 4s'),
                        ),
                        DropdownMenuItem(
                          value: 8,
                          child: Text('Higher accuracy · 8s'),
                        ),
                      ],
                      onChanged: _busy
                          ? null
                          : (value) =>
                                setState(() => _liveChunkSeconds = value!),
                    ),
                    const SizedBox(height: 8),
                    const Text(
                      '4s responds sooner but may miss words at chunk boundaries or fall behind. 8s gives Whisper more context and throughput.',
                      style: TextStyle(color: Color(0xffadb8ca)),
                    ),
                    const SizedBox(height: 16),
                    Row(
                      children: [
                        Expanded(
                          child: DropdownButtonFormField<String>(
                            key: ValueKey(_source),
                            initialValue: _source,
                            isExpanded: true,
                            decoration: const InputDecoration(
                              labelText: 'Audio source',
                            ),
                            items: _devices
                                .map(
                                  (device) => DropdownMenuItem<String>(
                                    value: device['id'] as String,
                                    child: Text(
                                      device['label'] as String,
                                      overflow: TextOverflow.ellipsis,
                                    ),
                                  ),
                                )
                                .toList(),
                            onChanged: _busy
                                ? null
                                : (value) => setState(() => _source = value),
                          ),
                        ),
                        IconButton(
                          onPressed: _refreshDevices,
                          tooltip: 'Refresh devices',
                          icon: const Icon(Icons.refresh),
                        ),
                      ],
                    ),
                    const SizedBox(height: 12),
                    FilledButton.icon(
                      onPressed:
                          _busy ||
                              _pending ||
                              _source == null ||
                              !_languageReady
                          ? null
                          : () => _command('start_live', {
                              'source': _source,
                              'model': _model,
                              'language': _language,
                              'output': _output.text,
                              'chunk_seconds': _liveChunkSeconds,
                            }),
                      icon: const Icon(Icons.fiber_manual_record),
                      label: const Text('Start live transcription'),
                    ),
                  ],
                ),
              ),
            ),
            const SizedBox(height: 18),
            Card(
              child: Padding(
                padding: const EdgeInsets.all(24),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      'Existing recording',
                      style: Theme.of(context).textTheme.titleLarge,
                    ),
                    const SizedBox(height: 8),
                    const Text(
                      'Process an audio or video file in the background, without playing it.',
                    ),
                    const SizedBox(height: 12),
                    OutlinedButton.icon(
                      onPressed: _pickFile,
                      icon: const Icon(Icons.audio_file_outlined),
                      label: const Text('Choose audio / video'),
                    ),
                    if (_file != null)
                      Padding(
                        padding: const EdgeInsets.symmetric(vertical: 12),
                        child: Text(_file!, overflow: TextOverflow.ellipsis),
                      ),
                    const SizedBox(height: 8),
                    FilledButton.icon(
                      onPressed:
                          _busy || _pending || _file == null || !_languageReady
                          ? null
                          : () => _command('start_file', {
                              'source': _file,
                              'model': _model,
                              'language': _language,
                              'output': _output.text,
                            }),
                      icon: const Icon(Icons.play_arrow),
                      label: const Text('Transcribe file'),
                    ),
                  ],
                ),
              ),
            ),
            const SizedBox(height: 20),
            const Text(
              'Audio stays on this computer. Models download once; transcription then runs locally.',
              textAlign: TextAlign.center,
              style: TextStyle(color: Color(0xffaab4c4)),
            ),
          ],
        ),
      ),
    );
    return Scaffold(
      body: SafeArea(
        child: SingleChildScrollView(child: Center(child: content)),
      ),
    );
  }
}
