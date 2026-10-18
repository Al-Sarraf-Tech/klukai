@TestOn('browser')
library;

// Widget tests for HerPovScreen's request/poll loop. The HTTP client and the
// gallery service are injected, so no network is involved. The screen pulls
// in MemoryArchiveScreen (package:web), so it runs under chrome:
//   flutter test --platform chrome test/screens/her_pov_screen_test.dart
import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:companion_app/models/memory.dart';
import 'package:companion_app/screens/her_pov_screen.dart';
import 'package:companion_app/services/memory_service.dart';

/// A JSON response. Her lines carry ellipses (U+2026), and http.Response
/// encodes a body as latin1 unless the content-type says otherwise — the
/// real server always sends utf-8.
http.Response _json(
  String body,
  int status, {
  Map<String, String> headers = const {},
}) => http.Response(
  body,
  status,
  headers: {'content-type': 'application/json; charset=utf-8', ...headers},
);

/// An empty gallery, or one that fails the way the real service does.
class _Gallery extends MemoryService {
  _Gallery({this.error}) : super(serverUrl: 'http://127.0.0.1:1');
  final Object? error;
  int calls = 0;

  @override
  Future<List<Memory>> fetchMemories({
    String? category,
    int limit = 20,
    String? before,
    String? month,
    String? beforeId,
  }) async {
    calls++;
    if (error != null) throw error!;
    return const [];
  }
}

/// Scripted answers for POST /api/memories/her-pov and its GET status poll.
class _Backend {
  final List<http.Request> requests = [];
  http.Response Function(http.Request) onStart = (_) =>
      _json('{"job_id":"j1"}', 202);
  Future<http.Response> Function(http.Request) onPoll = (_) async =>
      _json('{"phase":"searching","message":"Searching…"}', 200);

  http.Client get client => MockClient((req) async {
    requests.add(req);
    if (req.method == 'POST') return onStart(req);
    return onPoll(req);
  });

  int get polls => requests.where((r) => r.method == 'GET').length;
}

Future<void> _pump(
  WidgetTester tester,
  _Backend backend, {
  VoidCallback? onAuthExpired,
  _Gallery? gallery,
}) async {
  await tester.pumpWidget(
    MaterialApp(
      home: HerPovScreen(
        serverUrl: 'http://127.0.0.1:1',
        affectionLevel: 4,
        affectionLevelName: 'Trusted',
        httpClient: backend.client,
        memoryService: gallery ?? _Gallery(),
        onAuthExpired: onAuthExpired,
      ),
    ),
  );
  await tester.pump();
  await tester.pump();
}

Future<void> _tapFind(WidgetTester tester) async {
  await tester.tap(find.text('Find a moment'));
  await tester.pump();
  await tester.pump();
}

void main() {
  group('HerPovScreen.rateLimitedLine', () {
    test('speaks in minutes, never as a dead link', () {
      expect(
        HerPovScreen.rateLimitedLine(null),
        '…One at a time, Commander. Give me a moment.',
      );
      expect(
        HerPovScreen.rateLimitedLine(0),
        '…One at a time, Commander. Give me a moment.',
      );
      expect(
        HerPovScreen.rateLimitedLine(30),
        '…One at a time, Commander. Give me a minute.',
      );
      expect(
        HerPovScreen.rateLimitedLine(90),
        '…One at a time, Commander. Give me a minute.',
      );
      expect(
        HerPovScreen.rateLimitedLine(91),
        '…One at a time, Commander. Ask me again in 2 minutes.',
      );
      expect(
        HerPovScreen.rateLimitedLine(3600),
        '…One at a time, Commander. Ask me again in 60 minutes.',
      );
    });
  });

  testWidgets(
    'a 429 is her throttle line, not a dead link, and the button re-arms',
    (tester) async {
      final backend = _Backend()
        ..onStart = (_) => _json(
          '{"error":"Too many requests","code":"RATE_LIMITED","retry_after":540}',
          429,
          headers: {'retry-after': '540'},
        );
      await _pump(tester, backend);
      await _tapFind(tester);

      expect(
        find.text('…One at a time, Commander. Ask me again in 9 minutes.'),
        findsOneWidget,
      );
      expect(find.textContaining('Could not reach her'), findsNothing);
      expect(find.text('Interrupted.'), findsNothing);
      expect(find.text('Find a moment'), findsOneWidget); // not "Working…"
      expect(backend.polls, 0); // nothing to poll for
    },
  );

  testWidgets(
    'a 429 without the header falls back to the body\'s retry_after',
    (tester) async {
      final backend = _Backend()
        ..onStart = (_) =>
            _json('{"error":"Too many requests","retry_after":45}', 429);
      await _pump(tester, backend);
      await _tapFind(tester);
      expect(
        find.text('…One at a time, Commander. Give me a minute.'),
        findsOneWidget,
      );
    },
  );

  testWidgets('a genuinely unreachable server still says so', (tester) async {
    final backend = _Backend()
      ..onStart = (_) => throw http.ClientException('connection refused');
    await _pump(tester, backend);
    await _tapFind(tester);
    expect(find.text('Could not reach her. Try again.'), findsOneWidget);
  });

  testWidgets('transport failures while polling count toward the give-up', (
    tester,
  ) async {
    final backend = _Backend()
      ..onPoll = (_) async => throw http.ClientException('connection refused');
    await _pump(tester, backend);
    await _tapFind(tester); // start, then the first tick fails (1 of 5)
    expect(backend.polls, 1);
    expect(find.text('Working…'), findsOneWidget);

    for (var i = 0; i < 4; i++) {
      await tester.pump(const Duration(seconds: 2));
    }
    expect(backend.polls, 5);
    expect(find.text('Lost the signal. Ask me again.'), findsOneWidget);
    expect(find.text('Find a moment'), findsOneWidget); // button freed

    // The poll timer is gone: no more requests for the life of the tab.
    await tester.pump(const Duration(seconds: 6));
    expect(backend.polls, 5);
  });

  testWidgets('a 404 poll still gives up after five', (tester) async {
    final backend = _Backend()
      ..onPoll = (_) async => _json('{"error":"Not found"}', 404);
    await _pump(tester, backend);
    await _tapFind(tester);
    for (var i = 0; i < 4; i++) {
      await tester.pump(const Duration(seconds: 2));
    }
    expect(find.text('Lost track of that one. Ask me again.'), findsOneWidget);
    await tester.pump(const Duration(seconds: 4));
    expect(backend.polls, 5);
  });

  testWidgets('a slow poll is never stacked by the next tick', (tester) async {
    final gate = Completer<http.Response>();
    final backend = _Backend()..onPoll = (_) => gate.future;
    await _pump(tester, backend);
    await _tapFind(tester);
    expect(backend.polls, 1);

    // Two more periods elapse with the first poll still in flight.
    await tester.pump(const Duration(seconds: 2));
    await tester.pump(const Duration(seconds: 2));
    expect(backend.polls, 1);

    // It lands; the loop resumes on the next period.
    gate.complete(_json('{"phase":"thinking","message":"Replaying…"}', 200));
    await tester.pump();
    await tester.pump();
    expect(find.text('Replaying…'), findsOneWidget);
    backend.onPoll = (_) async =>
        _json('{"phase":"failed","message":"…It slipped away."}', 200);
    await tester.pump(const Duration(seconds: 2));
    await tester.pump();
    expect(backend.polls, 2);
    expect(find.text('…It slipped away.'), findsOneWidget);
    await tester.pump(const Duration(seconds: 4));
    expect(backend.polls, 2); // the job ended; the poll stopped with it
  });

  group('HerPovScreen — a dead token bounces to login', () {
    testWidgets('on start', (tester) async {
      var bounced = 0;
      final backend = _Backend()
        ..onStart = (_) => _json('{"error":"Authentication required"}', 401);
      await _pump(tester, backend, onAuthExpired: () => bounced++);
      await _tapFind(tester);
      expect(bounced, 1);
      expect(find.textContaining('Could not reach her'), findsNothing);
      expect(backend.polls, 0);
    });

    testWidgets('while polling, and the poll stops', (tester) async {
      var bounced = 0;
      final backend = _Backend()
        ..onPoll = (_) async =>
            _json('{"error":"Authentication required"}', 401);
      await _pump(tester, backend, onAuthExpired: () => bounced++);
      await _tapFind(tester);
      expect(bounced, 1);
      await tester.pump(const Duration(seconds: 4));
      expect(backend.polls, 1);
    });

    testWidgets('on the gallery', (tester) async {
      var bounced = 0;
      final gallery = _Gallery(
        error: MemoryServiceException(401, '/api/memories'),
      );
      await _pump(
        tester,
        _Backend(),
        gallery: gallery,
        onAuthExpired: () => bounced++,
      );
      expect(gallery.calls, 1);
      expect(bounced, 1);
    });

    testWidgets('a 503 gallery is not a bounce', (tester) async {
      var bounced = 0;
      await _pump(
        tester,
        _Backend(),
        gallery: _Gallery(error: MemoryServiceException(503, '/api/memories')),
        onAuthExpired: () => bounced++,
      );
      expect(bounced, 0);
    });
  });
}
