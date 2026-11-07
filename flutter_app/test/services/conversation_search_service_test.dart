import 'dart:convert';
import 'package:companion_app/models/conversation_search.dart';
import 'package:companion_app/services/conversation_search_service.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

void main() {
  test(
    'literal query stays in POST body with local calendar bounds and stable cursor',
    () async {
      final requests = <http.Request>[];
      final service = ConversationSearchService(
        serverUrl: 'https://fixture.invalid',
        client: MockClient((r) async {
          requests.add(r);
          return http.Response(
            jsonEncode({
              'messages': [
                {
                  'id': 'a',
                  'role': 'user',
                  'excerpt': '雨 %_\\\'',
                  'created_at': '2026-11-01T01:02:03.123456+00:00',
                },
              ],
              'next_cursor': {
                'created_at': '2026-11-01T01:02:03.123456+00:00',
                'id': 'a',
              },
            }),
            200,
            headers: {'content-type': 'application/json; charset=utf-8'},
          );
        }),
      );
      final page = await service.search(
        query: '  雨 %_\\\'  ',
        speaker: 'user',
        from: DateTime(2026, 3, 8),
        through: DateTime(2026, 3, 8),
      );
      expect(page.messages.single.excerpt, '雨 %_\\\'');
      await service.search(query: 'rain', cursor: page.nextCursor);
      expect(requests.first.method, 'POST');
      expect(
        requests.first.url.toString(),
        'https://fixture.invalid/api/messages/search',
      );
      final body = jsonDecode(requests.first.body) as Map<String, dynamic>;
      expect(body['query'], '雨 %_\\\'');
      expect(body['speaker'], 'user');
      expect(DateTime.parse(body['start']), DateTime(2026, 3, 8).toUtc());
      expect(DateTime.parse(body['end']), DateTime(2026, 3, 9).toUtc());
      expect(body['cursor'], isNull);
      expect(jsonDecode(requests.last.body)['cursor'], {
        'created_at': '2026-11-01T01:02:03.123456+00:00',
        'id': 'a',
      });
      expect(requests.first.headers['content-type'], 'application/json');
      expect(requests.first.headers['authorization'], startsWith('Bearer '));
    },
  );
  test(
    'context carries directional cursor and preserves original full Unicode',
    () async {
      late http.Request sent;
      final service = ConversationSearchService(
        serverUrl: 'https://fixture.invalid',
        client: MockClient((r) async {
          sent = r;
          return http.Response(
            jsonEncode({
              'anchor_id': 'anchor',
              'messages': [
                {
                  'id': 'anchor',
                  'role': 'assistant',
                  'content': 'exact\n雨🌧️',
                  'created_at': '2026-01-01T00:00:00Z',
                },
              ],
              'older_cursor': null,
              'newer_cursor': null,
            }),
            200,
            headers: {'content-type': 'application/json; charset=utf-8'},
          );
        }),
      );
      final page = await service.context(
        anchorId: 'anchor',
        before: const MessageCursor(
          createdAt: '2026-01-01T00:00:00Z',
          id: 'edge',
        ),
      );
      expect(sent.url.path, '/api/messages/context');
      expect(jsonDecode(sent.body), {
        'anchor_id': 'anchor',
        'before': {'created_at': '2026-01-01T00:00:00Z', 'id': 'edge'},
        'after': null,
      });
      expect(page.messages.single.content, 'exact\n雨🌧️');
    },
  );
  test('invalid inputs never send', () async {
    var sent = 0;
    final service = ConversationSearchService(
      serverUrl: 'https://fixture.invalid',
      client: MockClient((_) async {
        sent++;
        return http.Response('{}', 200);
      }),
    );
    for (final q in ['', 'a', 'x' * 201, 'ab\u0000']) {
      await expectLater(service.search(query: q), throwsArgumentError);
    }
    await expectLater(
      service.search(query: 'rain', speaker: 'system'),
      throwsArgumentError,
    );
    await expectLater(
      service.search(
        query: 'rain',
        from: DateTime(2026, 2, 2),
        through: DateTime(2026, 2, 1),
      ),
      throwsArgumentError,
    );
    const edge = MessageCursor(createdAt: '2026-01-01T00:00:00Z', id: 'a');
    await expectLater(
      service.context(anchorId: 'a', before: edge, after: edge),
      throwsArgumentError,
    );
    expect(sent, 0);
  });
  for (final status in [401, 404, 422, 503]) {
    test('HTTP $status is not empty success', () async {
      final service = ConversationSearchService(
        serverUrl: 'https://fixture.invalid',
        client: MockClient((_) async => http.Response('{}', status)),
      );
      await expectLater(
        service.search(query: 'rain'),
        throwsA(
          isA<ConversationSearchException>().having(
            (e) => e.statusCode,
            'status',
            status,
          ),
        ),
      );
    });
  }
}
