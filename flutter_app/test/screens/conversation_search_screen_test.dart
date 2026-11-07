import 'dart:async';
import 'dart:convert';
import 'package:companion_app/screens/conversation_search_screen.dart';
import 'package:companion_app/screens/conversation_context_screen.dart';
import 'package:companion_app/services/conversation_search_service.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

Map<String, dynamic> hit(String id, String text) => {
  'id': id,
  'role': 'user',
  'excerpt': text,
  'created_at': '2026-11-01T01:02:03Z',
};
http.Response results(
  List<Map<String, dynamic>> messages, {
  bool more = false,
}) => http.Response(
  jsonEncode({
    'messages': messages,
    'next_cursor': more
        ? {'created_at': '2026-11-01T01:02:03Z', 'id': messages.last['id']}
        : null,
  }),
  200,
  headers: {'content-type': 'application/json; charset=utf-8'},
);
Future<void> screen(
  WidgetTester tester,
  http.Client client, {
  VoidCallback? expired,
}) async {
  await tester.pumpWidget(
    MaterialApp(
      home: ConversationSearchScreen(
        serverUrl: 'https://fixture.invalid',
        service: ConversationSearchService(
          serverUrl: 'https://fixture.invalid',
          client: client,
        ),
        onAuthExpired: expired,
      ),
    ),
  );
}

Future<void> submit(WidgetTester tester, String query) async {
  await tester.enterText(find.byKey(const Key('conversation-query')), query);
  await tester.pump();
  await tester.tap(find.byKey(const Key('conversation-submit')));
  await tester.pump();
}

void main() {
  testWidgets(
    'explicit submit, loading, unavailable retry and empty are distinct',
    (tester) async {
      final pending = Completer<http.Response>();
      var calls = 0;
      await screen(
        tester,
        MockClient((_) {
          calls++;
          return calls == 1 ? pending.future : Future.value(results([]));
        }),
      );
      expect(find.text('Search an exact word or phrase'), findsOneWidget);
      await tester.enterText(
        find.byKey(const Key('conversation-query')),
        'rain',
      );
      await tester.pump();
      expect(calls, 0);
      await tester.tap(find.byKey(const Key('conversation-submit')));
      await tester.pump();
      expect(find.byType(CircularProgressIndicator), findsOneWidget);
      pending.complete(http.Response('{}', 503));
      await tester.pumpAndSettle();
      expect(find.text('Search is unavailable. Try again.'), findsOneWidget);
      await tester.tap(find.text('Retry'));
      await tester.pumpAndSettle();
      expect(find.text('No matching conversations.'), findsOneWidget);
    },
  );
  testWidgets(
    'query and filter editing invalidate old pagination and responses',
    (tester) async {
      final pending = Completer<http.Response>();
      var calls = 0;
      await screen(
        tester,
        MockClient((_) async {
          calls++;
          return calls == 1
              ? results([hit('a', 'first page')], more: true)
              : pending.future;
        }),
      );
      await submit(tester, 'rain');
      await tester.pumpAndSettle();
      await tester.tap(find.text('Load more'));
      await tester.pump();
      await tester.enterText(
        find.byKey(const Key('conversation-query')),
        'snow',
      );
      pending.complete(results([hit('b', 'stale page')]));
      await tester.pumpAndSettle();
      expect(find.text('first page'), findsNothing);
      expect(find.text('stale page'), findsNothing);
      expect(find.text('Load more'), findsNothing);
      await tester.tap(find.byKey(const Key('conversation-speaker')));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Klukai').last);
      await tester.pumpAndSettle();
      expect(calls, 2);
    },
  );
  testWidgets('paging retains cursor and selecting excerpt opens full source', (
    tester,
  ) async {
    var calls = 0;
    final bodies = <Map<String, dynamic>>[];
    await screen(
      tester,
      MockClient((r) async {
        bodies.add(jsonDecode(r.body));
        if (r.url.path.endsWith('/context')) {
          return http.Response(
            jsonEncode({
              'anchor_id': 'b',
              'messages': [
                {
                  'id': 'b',
                  'role': 'assistant',
                  'content': 'Original 雨🌧️',
                  'created_at': '2026-11-01T01:02:03Z',
                },
              ],
              'older_cursor': null,
              'newer_cursor': null,
            }),
            200,
            headers: {'content-type': 'application/json; charset=utf-8'},
          );
        }
        calls++;
        return results([
          hit(calls == 1 ? 'a' : 'b', calls == 1 ? 'first' : 'exact 雨🌧️'),
        ], more: calls == 1);
      }),
    );
    await submit(tester, 'rain');
    await tester.pumpAndSettle();
    await tester.tap(find.text('Load more'));
    await tester.pumpAndSettle();
    expect(bodies[1]['cursor'], {
      'created_at': '2026-11-01T01:02:03Z',
      'id': 'a',
    });
    await tester.tap(find.text('exact 雨🌧️'));
    await tester.pumpAndSettle();
    expect(find.byType(ConversationContextScreen), findsOneWidget);
    expect(find.text('Original 雨🌧️'), findsOneWidget);
    expect(find.text('Selected message'), findsOneWidget);
  });
  testWidgets(
    '401 expires once and suppresses later result and disposed work',
    (tester) async {
      var expired = 0;
      final requests = <Completer<http.Response>>[];
      await screen(
        tester,
        MockClient((_) {
          final c = Completer<http.Response>();
          requests.add(c);
          return c.future;
        }),
        expired: () => expired++,
      );
      await submit(tester, 'rain');
      await submit(tester, 'snow');
      requests.first.complete(http.Response('{}', 401));
      await tester.pumpAndSettle();
      requests.last.complete(results([hit('a', 'after expiry')]));
      await tester.pumpAndSettle();
      expect(expired, 1);
      expect(find.text('after expiry'), findsNothing);
      await tester.pumpWidget(const MaterialApp(home: SizedBox()));
      expect(tester.takeException(), isNull);
    },
  );
  testWidgets('320px and scale 2 retain controls with long Unicode', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(320, 900);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await tester.pumpWidget(
      MaterialApp(
        builder: (context, child) => MediaQuery(
          data: MediaQuery.of(
            context,
          ).copyWith(textScaler: TextScaler.linear(2)),
          child: child!,
        ),
        home: ConversationSearchScreen(
          serverUrl: 'https://fixture.invalid',
          service: ConversationSearchService(
            serverUrl: 'https://fixture.invalid',
            client: MockClient((_) async => results([hit('a', '雨🌧️' * 150)])),
          ),
        ),
      ),
    );
    await submit(tester, '雨🌧️');
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
    expect(find.byKey(const Key('conversation-from')), findsOneWidget);
    expect(find.byKey(const Key('conversation-through')), findsOneWidget);
  });
  testWidgets(
    'clearing speaker filters updates the visible selection and request',
    (tester) async {
      final bodies = <Map<String, dynamic>>[];
      await screen(
        tester,
        MockClient((r) async {
          bodies.add(jsonDecode(r.body));
          return results([]);
        }),
      );
      await tester.tap(find.byKey(const Key('conversation-speaker')));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Klukai').last);
      await tester.pumpAndSettle();
      await tester.tap(find.text('Clear filters'));
      await tester.pumpAndSettle();
      expect(find.text('Everyone'), findsOneWidget);
      expect(find.text('Klukai'), findsNothing);
      await submit(tester, 'rain');
      await tester.pumpAndSettle();
      expect(bodies.single['speaker'], 'all');
    },
  );
  testWidgets(
    'local date filters reject reversed range visibly before a request',
    (tester) async {
      var requests = 0;
      await screen(
        tester,
        MockClient((_) async {
          requests++;
          return results([]);
        }),
      );
      Future<void> choose(String key, String date) async {
        await tester.tap(find.byKey(Key(key)));
        await tester.pumpAndSettle();
        await tester.tap(find.byIcon(Icons.edit_outlined));
        await tester.pumpAndSettle();
        await tester.enterText(find.byType(TextField).last, date);
        await tester.tap(find.text('OK'));
        await tester.pumpAndSettle();
      }

      await choose('conversation-from', '11/06/2026');
      await choose('conversation-through', '11/05/2026');
      await submit(tester, 'rain');
      await tester.pumpAndSettle();
      expect(find.text('From must be on or before Through.'), findsOneWidget);
      expect(requests, 0);
      await tester.tap(find.text('Clear filters'));
      await tester.pumpAndSettle();
      expect(find.text('From: Any date'), findsOneWidget);
      expect(find.text('Through: Any date'), findsOneWidget);
      await submit(tester, 'rain');
      await tester.pumpAndSettle();
      expect(requests, 1);
    },
  );
  testWidgets(
    'changing speaker invalidates a pending first page without relabeling it',
    (tester) async {
      final pending = Completer<http.Response>();
      await screen(tester, MockClient((_) => pending.future));
      await submit(tester, 'rain');
      await tester.tap(find.byKey(const Key('conversation-speaker')));
      await tester.pump(const Duration(milliseconds: 300));
      await tester.pump();
      await tester.tap(find.text('You').last);
      await tester.pump();
      pending.complete(results([hit('a', 'old everyone result')]));
      await tester.pumpAndSettle();
      expect(find.text('old everyone result'), findsNothing);
      expect(find.text('No matching conversations.'), findsNothing);
    },
  );
  testWidgets('disposed search ignores late auth failure', (tester) async {
    final pending = Completer<http.Response>();
    var expired = 0;
    await screen(
      tester,
      MockClient((_) => pending.future),
      expired: () => expired++,
    );
    await submit(tester, 'rain');
    await tester.pumpWidget(const MaterialApp(home: SizedBox()));
    pending.complete(http.Response('{}', 401));
    await tester.pumpAndSettle();
    expect(expired, 0);
    expect(tester.takeException(), isNull);
  });
}
