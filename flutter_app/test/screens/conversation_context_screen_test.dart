import 'dart:async';
import 'dart:convert';
import 'package:companion_app/screens/conversation_context_screen.dart';
import 'package:companion_app/services/conversation_search_service.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

Map<String, dynamic> message(int i, {bool long = false}) => {
  'id': 'm$i',
  'role': i.isEven ? 'assistant' : 'user',
  'content':
      'Original $i 雨🌧️${long ? '\n${'Long original text. ' * 30}' : ''}',
  'created_at': '2026-11-01T01:${i.toString().padLeft(2, '0')}:00Z',
};
Map<String, dynamic> cursor(int i) => {
  'created_at': '2026-11-01T01:${i.toString().padLeft(2, '0')}:00Z',
  'id': 'm$i',
};
http.Response page(
  List<Map<String, dynamic>> messages, {
  int? older,
  int? newer,
}) => http.Response(
  jsonEncode({
    'anchor_id': 'm10',
    'messages': messages,
    'older_cursor': older == null ? null : cursor(older),
    'newer_cursor': newer == null ? null : cursor(newer),
  }),
  200,
  headers: {'content-type': 'application/json; charset=utf-8'},
);
Future<void> reader(
  WidgetTester tester,
  http.Client client, {
  VoidCallback? expired,
}) async {
  await tester.pumpWidget(
    MaterialApp(
      home: ConversationContextScreen(
        serverUrl: 'https://fixture.invalid',
        anchorId: 'm10',
        service: ConversationSearchService(
          serverUrl: 'https://fixture.invalid',
          client: client,
        ),
        onAuthExpired: expired,
      ),
    ),
  );
  await tester.pump();
}

void main() {
  testWidgets(
    'selected original starts visible despite long earlier messages',
    (tester) async {
      await reader(
        tester,
        MockClient(
          (_) async =>
              page([for (var i = 0; i <= 20; i++) message(i, long: true)]),
        ),
      );
      await tester.pumpAndSettle();
      final selected = find.text('Selected message');
      expect(selected, findsOneWidget);
      final rect = tester.getRect(selected);
      expect(rect.top, greaterThan(50));
      expect(rect.bottom, lessThan(600));
      expect(
        tester.widget<SelectableText>(find.byType(SelectableText).at(10)).data,
        startsWith('Original 10 雨🌧️'),
      );
    },
  );
  testWidgets(
    'directional pages merge IDs and retain the opposite edge cursor',
    (tester) async {
      final requests = <Map<String, dynamic>>[];
      await reader(
        tester,
        MockClient((r) async {
          final body = jsonDecode(r.body) as Map<String, dynamic>;
          requests.add(body);
          if (body['before'] != null) {
            return page([message(0), message(1), message(9)], newer: 9);
          }
          if (body['after'] != null) return page([message(11), message(12)]);
          return page(
            [message(9), message(10), message(11)],
            older: 9,
            newer: 11,
          );
        }),
      );
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.text('Load earlier'));
      await tester.tap(find.text('Load earlier'));
      await tester.pumpAndSettle();
      expect(find.text('Original 9 雨🌧️'), findsOneWidget);
      expect(find.text('Selected message'), findsOneWidget);
      await tester.ensureVisible(find.text('Load later'));
      await tester.tap(find.text('Load later'));
      await tester.pumpAndSettle();
      expect(requests.last['after'], cursor(11));
      expect(requests.last['before'], isNull);
      expect(find.text('Original 11 雨🌧️'), findsOneWidget);
      expect(find.byType(SelectableText), findsNWidgets(6));
    },
  );
  testWidgets(
    '404 source not found differs from 503 retry and initial loading',
    (tester) async {
      var calls = 0;
      final pending = Completer<http.Response>();
      await reader(
        tester,
        MockClient((_) {
          calls++;
          return calls == 1
              ? pending.future
              : Future.value(http.Response('{}', 404));
        }),
      );
      expect(find.byType(CircularProgressIndicator), findsOneWidget);
      pending.complete(http.Response('{}', 503));
      await tester.pumpAndSettle();
      expect(
        find.text('Original exchange is unavailable. Try again.'),
        findsOneWidget,
      );
      await tester.tap(find.text('Retry'));
      await tester.pumpAndSettle();
      expect(find.text('Original message not found.'), findsOneWidget);
      expect(find.text('Retry'), findsNothing);
    },
  );
  testWidgets('auth expires once; late responses after disposal are inert', (
    tester,
  ) async {
    var expired = 0;
    await reader(
      tester,
      MockClient((_) async => http.Response('{}', 401)),
      expired: () => expired++,
    );
    await tester.pumpAndSettle();
    expect(expired, 1);
    expect(find.text('Session expired.'), findsOneWidget);
    final pending = Completer<http.Response>();
    await tester.pumpWidget(const MaterialApp(home: SizedBox()));
    await reader(
      tester,
      MockClient((_) => pending.future),
      expired: () => expired++,
    );
    await tester.pumpWidget(const MaterialApp(home: SizedBox()));
    pending.complete(http.Response('{}', 401));
    await tester.pumpAndSettle();
    expect(expired, 1);
    expect(tester.takeException(), isNull);
  });
  testWidgets('reader remains usable at 320px and scale 2', (tester) async {
    tester.view.physicalSize = const Size(320, 900);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await tester.pumpWidget(
      MaterialApp(
        builder: (c, child) => MediaQuery(
          data: MediaQuery.of(c).copyWith(textScaler: TextScaler.linear(2)),
          child: child!,
        ),
        home: ConversationContextScreen(
          serverUrl: 'https://fixture.invalid',
          anchorId: 'm10',
          service: ConversationSearchService(
            serverUrl: 'https://fixture.invalid',
            client: MockClient((_) async => page([message(10, long: true)])),
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
    final selectedRect = tester.getRect(find.text('Selected message'));
    expect(selectedRect.top, greaterThan(50));
    expect(selectedRect.bottom, lessThan(900));
  });
}
