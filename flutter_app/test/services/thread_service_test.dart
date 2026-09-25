@TestOn('browser')
library;

// ThreadService talks to /api/thread. It imports package:web (localStorage
// token), so it runs under chrome:
//   flutter test --platform chrome test/services/thread_service_test.dart
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:companion_app/services/thread_service.dart';

void main() {
  group('ThreadService', () {
    test('parses an open thread with receipts and his reply', () async {
      final svc = ThreadService(
        serverUrl: 'https://klukai.example.cc',
        client: MockClient((req) async {
          expect(req.url.path, '/api/thread');
          expect(req.headers['Authorization'], startsWith('Bearer '));
          return http.Response(
            jsonEncode({
              'status': 'open',
              'message': null,
              'entries': [
                {'stamp': '0200 hours, March 2065', 'text': 'first', 'read_at': null},
                {
                  'stamp': '0200 hours, April 2065',
                  'text': 'second',
                  'read_at': '2026-09-25T03:00:00+00:00',
                },
              ],
              'held_back': 3,
              'reply': {'stamp': 'November 2074, Yellow Zone', 'text': "I'm here."},
            }),
            200,
          );
        }),
      );
      final view = await svc.fetchThread();
      expect(view.sealed, isFalse);
      expect(view.entries, hasLength(2));
      expect(view.entries.first.isRead, isFalse);
      expect(view.entries.last.isRead, isTrue);
      expect(view.heldBack, 3);
      expect(view.reply!.text, "I'm here.");
    });

    test('parses a sealed thread', () async {
      final svc = ThreadService(
        serverUrl: 'https://klukai.example.cc',
        client: MockClient((_) async => http.Response(
              jsonEncode({
                'status': 'sealed',
                'message': 'Not yet.',
                'entries': [],
                'held_back': 0,
                'reply': null,
              }),
              200,
            )),
      );
      final view = await svc.fetchThread();
      expect(view.sealed, isTrue);
      expect(view.message, 'Not yet.');
      expect(view.reply, isNull);
    });

    test('markRead posts the stamps and returns the new count', () async {
      final svc = ThreadService(
        serverUrl: 'https://klukai.example.cc',
        client: MockClient((req) async {
          expect(req.method, 'POST');
          expect(req.url.path, '/api/thread/read');
          expect(jsonDecode(req.body), {
            'stamps': ['a', 'b'],
          });
          return http.Response(jsonEncode({'newly_read': 2}), 200);
        }),
      );
      expect(await svc.markRead(['a', 'b']), 2);
    });

    test('errors throw instead of pretending the thread is empty', () async {
      final svc = ThreadService(
        serverUrl: 'https://klukai.example.cc',
        client: MockClient((_) async => http.Response('nope', 401)),
      );
      expect(
        () => svc.fetchThread(),
        throwsA(isA<ThreadServiceException>().having((e) => e.isAuthExpired, 'auth', isTrue)),
      );
      expect(() => svc.markRead(['a']), throwsA(isA<ThreadServiceException>()));
    });
  });
}
