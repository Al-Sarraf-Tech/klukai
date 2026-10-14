import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

/// The voice service reads at most this many characters of one line.
const int kTtsMaxChars = 500;

/// Body for POST /api/tts — her voice reading one of her lines.
///
/// `language` is the language of the TEXT, not of her voice. Her voice is
/// always the Japanese Ai Nonaka reference clip; her lines are English, so
/// they are read as "en". "ja" asks XTTS to parse English as Japanese and
/// needs `cutlet`, which the voice image doesn't ship — it 500s.
Map<String, String> ttsRequestBody(String text) => {
  'text': clipForTts(text),
  'language': 'en',
};

/// Clips a line that is too long to read to its last sentence end within
/// [limit] (". ", "! ", "? ", or the same before a newline), so she stops at a
/// full stop instead of mid-word. If the last sentence end comes before a
/// third of the limit — one short sentence and then a wall of text — the clip
/// would drop almost everything she said, so it falls back to the hard cut.
/// Text within the limit is returned untouched.
String clipForTts(String text, {int limit = kTtsMaxChars}) {
  if (text.length <= limit) return text;
  // One extra character so a sentence ending exactly at the limit still
  // counts (its trailing space is the character just past it).
  final window = text.substring(0, limit + 1);
  var cut = -1;
  for (final boundary in const ['. ', '! ', '? ', '.\n', '!\n', '?\n']) {
    final i = window.lastIndexOf(boundary);
    if (i > cut) cut = i;
  }
  if (cut < limit ~/ 3) return text.substring(0, limit);
  return text.substring(0, cut + 1); // keep the punctuation, drop the space
}

/// Speak every sentence, splitting an oversized sentence at word boundaries.
/// This follows the stage's chunkForSpeech rules, including UTF-16 safety.
List<String> chunkForTts(String text, {int limit = kTtsMaxChars}) {
  if (limit < 2) throw ArgumentError.value(limit, 'limit');
  final line = text.replaceAll(RegExp(r'\s+'), ' ').trim();
  final chunks = <String>[];
  void append(String sentence) {
    var rest = sentence.trim();
    while (rest.length > limit) {
      var cut = rest.lastIndexOf(' ', limit);
      if (cut <= 0) {
        cut = limit;
        final previous = rest.codeUnitAt(cut - 1);
        if (previous >= 0xd800 && previous <= 0xdbff) cut--;
      }
      chunks.add(rest.substring(0, cut));
      rest = rest.substring(cut).trimLeft();
    }
    if (rest.isNotEmpty) chunks.add(rest);
  }

  const abbreviations = {
    'mr',
    'mrs',
    'ms',
    'dr',
    'prof',
    'rev',
    'capt',
    'lt',
    'col',
    'sgt',
    'gen',
    'sr',
    'jr',
    'st',
    'vs',
    'etc',
    'approx',
    'dept',
    'fig',
    'vol',
    'inc',
    'ltd',
    'ref',
  };
  final ends = RegExp(r'''[.!?…]+["'”’」』]*(?=\s|$)|[。！？]+["'”’」』]*''');
  var start = 0;
  for (final match in ends.allMatches(line)) {
    final punctuation = match
        .group(0)!
        .replaceAll(RegExp(r'''["'”’」』]+$'''), '');
    if (punctuation == '.' && match.end < line.length) {
      final token =
          RegExp(
            r'''(?:^|[\s([{“"'‘])([A-Za-z.]+)$''',
          ).firstMatch(line.substring(start, match.start))?.group(1) ??
          '';
      if (abbreviations.contains(token.toLowerCase()) ||
          RegExp(r'^[A-Z]$').hasMatch(token) ||
          RegExp(r'^(?:[A-Za-z]\.)+[A-Za-z]$').hasMatch(token)) {
        continue;
      }
    }
    append(line.substring(start, match.end));
    start = match.end;
  }
  append(line.substring(start));
  return chunks;
}

/// One cancellable synthesis request, supporting synchronous and queued hosts.
class TtsAudioRequest {
  final http.Client client;
  final String baseUrl;
  final String token;
  final Duration pollInterval;
  final Duration deadline;
  final _abort = Completer<void>();
  String? _jobId;
  Future<void>? _cleanup;

  TtsAudioRequest({
    required this.client,
    required this.baseUrl,
    required this.token,
    this.pollInterval = const Duration(seconds: 1),
    this.deadline = const Duration(seconds: 610),
  });

  Future<http.Response> _send(
    String method,
    String path, {
    Map<String, String>? body,
    Future<void>? abortTrigger,
  }) async {
    final uri = Uri.parse('$baseUrl$path');
    final trigger = abortTrigger ?? _abort.future;
    final request = http.AbortableRequest(method, uri, abortTrigger: trigger)
      ..headers['Authorization'] = 'Bearer $token';
    if (body != null) {
      request.headers['Content-Type'] = 'application/json';
      request.body = jsonEncode(body);
    }
    return Future.any([
      client.send(request).then(http.Response.fromStream),
      trigger.then<http.Response>(
        (_) => throw http.RequestAbortedException(uri),
      ),
    ]);
  }

  Future<String> run(String text) async {
    final timer = Timer(deadline, () {
      if (!_abort.isCompleted) _abort.complete();
    });
    try {
      if (_abort.isCompleted) throw http.RequestAbortedException();
      var response = await _send(
        'POST',
        '/api/tts',
        body: ttsRequestBody(text),
      );
      while (true) {
        if (response.statusCode != 200 && response.statusCode != 202) {
          throw StateError('Voice synth failed: HTTP ${response.statusCode}');
        }
        final data = jsonDecode(response.body) as Map<String, dynamic>;
        _jobId ??= data['job_id'] as String?;
        if (data['audio'] is String && (data['audio'] as String).isNotEmpty) {
          return data['audio'] as String;
        }
        if (_jobId == null ||
            data['status'] == 'failed' ||
            data['status'] == 'cancelled') {
          throw StateError('Voice service unavailable');
        }
        await Future.any([
          Future<void>.delayed(pollInterval),
          _abort.future.then<void>((_) => throw http.RequestAbortedException()),
        ]);
        response = await _send(
          'GET',
          '/api/tts/jobs/${Uri.encodeComponent(_jobId!)}',
        );
      }
    } finally {
      timer.cancel();
      await _deleteJob();
    }
  }

  Future<void> cancel() async {
    if (!_abort.isCompleted) _abort.complete();
    await _deleteJob();
  }

  Future<void> _deleteJob() {
    if (_jobId == null) return Future.value();
    return _cleanup ??= () async {
      // Cancelling this job must survive the already-aborted synthesis signal.
      final abortCleanup = Completer<void>();
      final timer = Timer(
        const Duration(seconds: 5),
        () => abortCleanup.complete(),
      );
      try {
        await _send(
          'DELETE',
          '/api/tts/jobs/${Uri.encodeComponent(_jobId!)}',
          abortTrigger: abortCleanup.future,
        );
      } catch (_) {
        // Server jobs also have a hard deadline and bounded result expiry.
      } finally {
        timer.cancel();
      }
    }();
  }
}
