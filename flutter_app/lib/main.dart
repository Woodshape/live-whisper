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
  static const _models = ['tiny', 'base', 'small', 'medium', 'large-v3'];
  static const _latencyPresets = [4, 8];

  final _output = TextEditingController();
  final _youtubeUrl = TextEditingController();
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
  bool _settingsReady = false;
  bool _settingsTabSelected = false;
  bool _automaticCaptions = true;
  int _liveChunkSeconds = 4;
  String? _error;
  bool _pending = false;
  bool _cancelPending = false;
  bool _polling = false;

  @override
  void initState() {
    super.initState();
    unawaited(_refreshDevices());
    unawaited(_refreshStatus());
    unawaited(_restoreSettings());
    _poller = Timer.periodic(
      const Duration(seconds: 1),
      (_) => unawaited(_refreshStatus()),
    );
  }

  @override
  void dispose() {
    _poller?.cancel();
    _output.dispose();
    _youtubeUrl.dispose();
    unawaited(widget.client.close());
    super.dispose();
  }

  Future<void> _restoreSettings() async {
    try {
      final prefs = await SharedPreferences.getInstance();
      if (!mounted) return;
      final savedModel = prefs.getString('whisper_model');
      final savedLanguage = prefs.getString('transcription_language');
      final savedLatency = prefs.getInt('live_chunk_seconds');
      setState(() {
        _model = _models.contains(savedModel) ? savedModel! : 'base';
        _language = savedLanguage == 'en' ? 'en' : 'de';
        _liveChunkSeconds = _latencyPresets.contains(savedLatency)
            ? savedLatency!
            : 4;
        _settingsReady = true;
      });
    } catch (error) {
      if (mounted) {
        setState(() {
          _settingsReady = true;
          _error = 'Cannot load preferences: $error';
        });
      }
    }
  }

  Future<void> _savePreference(String key, Object value, String label) async {
    try {
      final prefs = await SharedPreferences.getInstance();
      final saved = switch (value) {
        String() => await prefs.setString(key, value),
        int() => await prefs.setInt(key, value),
        _ => false,
      };
      if (!saved) throw StateError('Preference was not saved.');
    } catch (error) {
      if (mounted) {
        setState(() => _error = 'Cannot save $label preference: $error');
      }
    }
  }

  Future<void> _changeModel(String model) async {
    setState(() {
      _model = model;
      _error = null;
    });
    await _savePreference('whisper_model', model, 'model');
  }

  Future<void> _changeLanguage(String language) async {
    setState(() {
      _language = language;
      _error = null;
    });
    await _savePreference('transcription_language', language, 'language');
  }

  Future<void> _changeLatency(int seconds) async {
    setState(() {
      _liveChunkSeconds = seconds;
      _error = null;
    });
    await _savePreference('live_chunk_seconds', seconds, 'latency');
  }

  bool get _busy => [
    'uploading',
    'importing',
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
        if (_busy) await _command('output', {'output': location.path});
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

  Widget _tabButton({
    required String label,
    required IconData icon,
    required bool selected,
    required VoidCallback onPressed,
  }) {
    return Expanded(
      child: Semantics(
        button: true,
        selected: selected,
        label: label,
        child: Material(
          color: selected ? const Color(0xff1d2330) : Colors.transparent,
          borderRadius: BorderRadius.circular(999),
          child: InkWell(
            borderRadius: BorderRadius.circular(999),
            onTap: onPressed,
            child: SizedBox(
              height: 40,
              child: Row(
                mainAxisAlignment: MainAxisAlignment.center,
                children: [
                  Icon(icon, size: 18),
                  const SizedBox(width: 8),
                  Flexible(
                    child: Text(
                      label,
                      maxLines: 1,
                      overflow: TextOverflow.fade,
                      softWrap: false,
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }

  Widget _buildTabs() {
    return LayoutBuilder(
      builder: (context, constraints) {
        final compact = constraints.maxWidth < 560;
        return Align(
          alignment: Alignment.centerLeft,
          child: Container(
            width: compact ? double.infinity : 250,
            padding: const EdgeInsets.all(4),
            decoration: BoxDecoration(
              color: const Color(0xff171c27),
              borderRadius: BorderRadius.circular(999),
            ),
            child: Row(
              children: [
                _tabButton(
                  label: 'Capture',
                  icon: Icons.fiber_manual_record,
                  selected: !_settingsTabSelected,
                  onPressed: () => setState(() => _settingsTabSelected = false),
                ),
                _tabButton(
                  label: 'Settings',
                  icon: Icons.settings,
                  selected: _settingsTabSelected,
                  onPressed: () => setState(() => _settingsTabSelected = true),
                ),
              ],
            ),
          ),
        );
      },
    );
  }

  Widget _buildStatusCard({
    required String state,
    required String statusLabel,
    required Color color,
    required int captured,
    required num lines,
    required String speedLabel,
    required bool fallingBehind,
    required bool youtubeImportInProgress,
    required String captionKind,
    required String captionLanguageText,
    required List<String> recentLines,
    required bool modelReady,
  }) {
    final canStart = !_busy && !_pending && _source != null && _settingsReady;
    final canStop =
        _busy && state != 'preloading' && state != 'stopping' && !_pending;
    final showCancel = state == 'stopping' && !youtubeImportInProgress;
    final cancelRequested = _status['cancel_requested'] == true;
    final cancellationPending = cancelRequested || _cancelPending;
    final chunkSeconds = _status['mode'] == 'live'
        ? (_status['live_chunk_seconds'] as num? ?? 4).toInt()
        : 30;
    final outputPath = _output.text.trim().isNotEmpty
        ? _output.text.trim()
        : _busy && (_status['output'] as String? ?? '').isNotEmpty
        ? _status['output'] as String
        : 'Automatic output: ~/YYYY-MM-DD_HH-MM (chosen at start)';

    return Card(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            LayoutBuilder(
              builder: (context, constraints) {
                final compact = constraints.maxWidth < 480;
                final startButton = FilledButton.icon(
                  key: const Key('quick-start-button'),
                  onPressed: canStart
                      ? () => _command('start_live', {
                          'source': _source,
                          'model': _model,
                          'language': _language,
                          'output': _output.text,
                          'chunk_seconds': _liveChunkSeconds,
                        })
                      : null,
                  icon: const Icon(Icons.fiber_manual_record),
                  label: const Text('Start'),
                  style: FilledButton.styleFrom(
                    minimumSize: Size(compact ? 0 : 96, 40),
                  ),
                );
                final stopButton = OutlinedButton.icon(
                  key: const Key('quick-stop-button'),
                  onPressed: canStop ? () => _command('stop') : null,
                  icon: const Icon(Icons.stop_circle_outlined),
                  label: const Text('Stop'),
                  style: OutlinedButton.styleFrom(
                    minimumSize: Size(compact ? 0 : 96, 40),
                  ),
                );
                final cancelButton = Tooltip(
                  message:
                      'Cancel after the current $chunkSeconds-second chunk finishes',
                  child: OutlinedButton.icon(
                    key: const Key('cancel-buffered-button'),
                    onPressed: showCancel && !cancellationPending && !_pending
                        ? () async {
                            setState(() => _cancelPending = true);
                            await _command('cancel_buffered');
                            if (mounted) {
                              setState(() => _cancelPending = false);
                            }
                          }
                        : null,
                    icon: const Icon(Icons.cancel_outlined),
                    label: Text(cancellationPending ? 'Canceling…' : 'Cancel'),
                    style: OutlinedButton.styleFrom(
                      foregroundColor: const Color(0xffffb4ac),
                      minimumSize: Size(compact ? 0 : 96, 40),
                    ),
                  ),
                );
                final title = Row(
                  children: [
                    Icon(Icons.circle, color: color, size: 13),
                    const SizedBox(width: 10),
                    Expanded(
                      child: Text(
                        statusLabel,
                        style: Theme.of(context).textTheme.titleLarge?.copyWith(
                          color: color,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                    ),
                  ],
                );
                if (compact) {
                  return Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      title,
                      const SizedBox(height: 4),
                      Row(
                        children: [
                          Expanded(child: startButton),
                          const SizedBox(width: 8),
                          Expanded(child: stopButton),
                        ],
                      ),
                      if (showCancel) ...[
                        const SizedBox(height: 8),
                        SizedBox(width: double.infinity, child: cancelButton),
                      ],
                    ],
                  );
                }
                return Row(
                  children: [
                    Expanded(child: title),
                    const SizedBox(width: 8),
                    startButton,
                    const SizedBox(width: 8),
                    stopButton,
                    if (showCancel) ...[const SizedBox(width: 8), cancelButton],
                  ],
                );
              },
            ),
            const SizedBox(height: 16),
            Text(
              state == 'preloading'
                  ? 'Preparing the model before capture; no audio is being recorded yet.'
                  : state == 'importing'
                  ? speedLabel
                  : youtubeImportInProgress
                  ? 'Stopping YouTube import…'
                  : _status['input_source'] == 'youtube' &&
                        _status['transcript_source'] == 'youtube_captions'
                  ? '$lines transcript lines · $captionKind$captionLanguageText used · Whisper skipped'
                  : '$lines transcript lines  ·  ${captured}s captured  ·  ${_status['audio_detected'] == true ? 'Audio detected' : 'Waiting for audio'}',
              style: Theme.of(context).textTheme.bodyLarge,
            ),
            const SizedBox(height: 10),
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Expanded(
                  child: Padding(
                    padding: const EdgeInsets.only(top: 8),
                    child: SelectableText(
                      outputPath,
                      key: const Key('output-path-display'),
                      style: const TextStyle(color: Color(0xffadb8ca)),
                    ),
                  ),
                ),
                IconButton(
                  key: const Key('output-pick-button'),
                  onPressed: _pending ? null : _pickOutput,
                  tooltip: 'Choose output file',
                  icon: const Icon(Icons.folder_open_outlined),
                  iconSize: 18,
                  visualDensity: VisualDensity.compact,
                ),
                if (_output.text.trim().isNotEmpty)
                  IconButton(
                    key: const Key('output-clear-button'),
                    onPressed: _busy || _pending
                        ? null
                        : () => setState(_output.clear),
                    tooltip: 'Use automatic output next session',
                    icon: const Icon(Icons.close),
                    iconSize: 18,
                    visualDensity: VisualDensity.compact,
                  ),
              ],
            ),
            if (_status['input_source'] == 'youtube' &&
                (_status['source_title'] as String? ?? '').isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Text(
                  'YouTube · ${_status['source_title']}',
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                ),
              ),
            const SizedBox(height: 12),
            OutlinedButton.icon(
              key: const Key('preload-model-button'),
              onPressed: _busy || _pending || modelReady || !_settingsReady
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
            if (showCancel)
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: Text(
                  cancellationPending
                      ? 'Cancellation requested; letting the current chunk finish before discarding the remaining buffer.'
                      : 'Cancel finishes the current $chunkSeconds-second chunk before discarding the rest.',
                  style: const TextStyle(color: Color(0xffadb8ca)),
                ),
              ),
            if (_status['model_phase'] != null &&
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
                          padding: const EdgeInsets.symmetric(vertical: 7),
                          child: SelectableText(line),
                        ),
                    ],
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }

  Widget _buildModelSettingsCard() {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Model', style: Theme.of(context).textTheme.titleLarge),
            const SizedBox(height: 16),
            DropdownButtonFormField<String>(
              key: ValueKey('model-$_model'),
              initialValue: _model,
              decoration: const InputDecoration(labelText: 'Whisper model'),
              items: _models
                  .map(
                    (model) =>
                        DropdownMenuItem(value: model, child: Text(model)),
                  )
                  .toList(),
              onChanged: _busy || _pending || !_settingsReady
                  ? null
                  : (model) {
                      if (model != null) unawaited(_changeModel(model));
                    },
            ),
            const SizedBox(height: 12),
            DropdownButtonFormField<String>(
              key: ValueKey('language-$_language'),
              initialValue: _language,
              decoration: const InputDecoration(
                labelText: 'Language / Sprache',
              ),
              items: const [
                DropdownMenuItem(value: 'de', child: Text('Deutsch (de)')),
                DropdownMenuItem(value: 'en', child: Text('English (en)')),
              ],
              onChanged: _busy || _pending || !_settingsReady
                  ? null
                  : (language) {
                      if (language != null) {
                        unawaited(_changeLanguage(language));
                      }
                    },
            ),
            const SizedBox(height: 8),
            const Text(
              'Fixed for each session; saved for the next app launch. Select the main spoken language.',
              style: TextStyle(color: Color(0xffadb8ca)),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildLiveSettingsCard() {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Live audio', style: Theme.of(context).textTheme.titleLarge),
            const SizedBox(height: 8),
            const Text(
              'Choose the default system-audio monitor, a microphone, or BlackHole on macOS. Used when you press Start on Capture.',
            ),
            const SizedBox(height: 16),
            DropdownButtonFormField<int>(
              key: ValueKey('latency-$_liveChunkSeconds'),
              initialValue: _liveChunkSeconds,
              decoration: const InputDecoration(
                labelText: 'Latency / accuracy',
              ),
              items: const [
                DropdownMenuItem(value: 4, child: Text('Low latency · 4s')),
                DropdownMenuItem(value: 8, child: Text('Higher accuracy · 8s')),
              ],
              onChanged: _busy || _pending || !_settingsReady
                  ? null
                  : (seconds) {
                      if (seconds != null) {
                        unawaited(_changeLatency(seconds));
                      }
                    },
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
                    key: ValueKey('source-$_source'),
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
                    onChanged: _busy || _pending || !_settingsReady
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
          ],
        ),
      ),
    );
  }

  Widget _buildRecordingCard() {
    return Card(
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
              onPressed: _busy || _pending ? null : _pickFile,
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
              onPressed: _busy || _pending || _file == null || !_settingsReady
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
    );
  }

  Widget _buildYoutubeCard() {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              'YouTube video',
              style: Theme.of(context).textTheme.titleLarge,
            ),
            const SizedBox(height: 8),
            const Text(
              'Creator-written captions are always used when the video has them. Without them, the audio is downloaded and transcribed locally — unless the option below is enabled.',
            ),
            const SizedBox(height: 12),
            TextField(
              key: const Key('youtube-url-field'),
              controller: _youtubeUrl,
              onChanged: (_) => setState(() {}),
              enabled: !_busy && !_pending && _settingsReady,
              decoration: const InputDecoration(
                labelText: 'YouTube video URL',
                hintText: 'https://www.youtube.com/watch?v=…',
              ),
            ),
            const SizedBox(height: 12),
            CheckboxListTile(
              key: const Key('youtube-captions-checkbox'),
              value: _automaticCaptions,
              onChanged: _busy || _pending || !_settingsReady
                  ? null
                  : (value) =>
                        setState(() => _automaticCaptions = value ?? false),
              contentPadding: EdgeInsets.zero,
              controlAffinity: ListTileControlAffinity.leading,
              title: const Text("Use YouTube's automatic captions"),
              subtitle: const Text(
                'Manual captions are always used. When ticked, automatic captions are used if there are none; untick to transcribe the downloaded audio with the selected model instead.',
                style: TextStyle(color: Color(0xffadb8ca)),
              ),
            ),
            const SizedBox(height: 4),
            FilledButton.icon(
              onPressed:
                  _busy ||
                      _pending ||
                      _youtubeUrl.text.trim().isEmpty ||
                      !_settingsReady
                  ? null
                  : () => _command('start_youtube', {
                      'url': _youtubeUrl.text.trim(),
                      'model': _model,
                      'language': _language,
                      'output': _output.text,
                      'automatic_captions': _automaticCaptions,
                    }),
              icon: const Icon(Icons.subtitles_outlined),
              label: const Text('Check captions & transcribe'),
            ),
            const SizedBox(height: 8),
            const Text(
              'Requires internet access. Use videos you are authorized to process; downloads are subject to YouTube availability and terms.',
              style: TextStyle(color: Color(0xffadb8ca)),
            ),
          ],
        ),
      ),
    );
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
    final importPhase = _status['import_phase'] as String?;
    final importDone = _status['import_downloaded_bytes'] as num? ?? 0;
    final importTotal = _status['import_total_bytes'] as num? ?? 0;
    final captionLanguage = (_status['transcript_language'] as String? ?? '')
        .replaceFirst(RegExp(r'-orig$'), '');
    final captionLanguageText = captionLanguage.isEmpty
        ? ''
        : ' ($captionLanguage)';
    final youtubeImportInProgress =
        _status['input_source'] == 'youtube' &&
        _status['transcript_source'] != 'whisper' &&
        (state == 'importing' || state == 'stopping');
    final captionsAutomatic = _status['captions_automatic'] == true;
    final captionKind = captionsAutomatic
        ? 'automatic captions'
        : 'manual captions';
    final importText = importPhase == 'checking_transcript'
        ? 'Checking for a caption track before downloading audio…'
        : importPhase == 'using_captions'
        ? 'Using YouTube captions; Whisper transcription skipped.'
        : importPhase == 'downloading_audio' && importTotal > 0
        ? 'Downloading audio ${(100 * importDone / importTotal).clamp(0, 100).toStringAsFixed(0)}% · ${(importDone / 1048576).toStringAsFixed(1)} / ${(importTotal / 1048576).toStringAsFixed(1)} MiB'
        : importPhase == 'downloading_audio'
        ? 'Downloading audio…'
        : importPhase == 'preparing_audio'
        ? 'Audio downloaded · starting local Whisper transcription…'
        : 'Importing YouTube video…';
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
    final speedLabel = state == 'cancelled'
        ? 'Canceled after the current chunk · remaining buffered audio discarded'
        : state == 'importing'
        ? importText
        : youtubeImportInProgress
        ? 'Stopping YouTube import…'
        : _status['transcript_source'] == 'youtube_captions'
        ? 'Completed from $captionKind$captionLanguageText · Whisper was skipped'
        : switch (modelPhase) {
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
            _
                when speed == null &&
                    (state == 'running' || state == 'stopping') =>
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
      'stopping' =>
        youtubeImportInProgress
            ? 'Stopping YouTube import'
            : 'Finishing buffered audio',
      'cancelled' => 'Canceled · buffered audio discarded',
      'loading' => 'Starting',
      'importing' => 'Importing YouTube video',
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
            const SizedBox(height: 20),
            _buildTabs(),
            const SizedBox(height: 18),
            if (_settingsTabSelected && _error != null)
              Padding(
                padding: const EdgeInsets.only(bottom: 12),
                child: SelectableText(
                  _error!,
                  style: const TextStyle(color: Color(0xffffb4ac)),
                ),
              ),
            if (!_settingsTabSelected)
              _buildStatusCard(
                state: state,
                statusLabel: statusLabel,
                color: color,
                captured: captured,
                lines: lines,
                speedLabel: speedLabel,
                fallingBehind: fallingBehind,
                youtubeImportInProgress: youtubeImportInProgress,
                captionKind: captionKind,
                captionLanguageText: captionLanguageText,
                recentLines: recentLines,
                modelReady: modelReady,
              ),
            if (_settingsTabSelected) _buildModelSettingsCard(),
            if (_settingsTabSelected) const SizedBox(height: 18),
            if (_settingsTabSelected) _buildLiveSettingsCard(),
            if (!_settingsTabSelected) const SizedBox(height: 18),
            if (!_settingsTabSelected) _buildRecordingCard(),
            if (!_settingsTabSelected) const SizedBox(height: 18),
            if (!_settingsTabSelected) _buildYoutubeCard(),
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
