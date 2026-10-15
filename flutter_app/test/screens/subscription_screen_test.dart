import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:companion_app/screens/subscription_screen.dart';

void main() {
  testWidgets('a failed status request is not a verified elite tier', (tester) async {
    await tester.pumpWidget(MaterialApp(
      home: SubscriptionScreen(
        serverUrl: 'http://127.0.0.1:1',
        authToken: 'token',
        client: MockClient((_) async => http.Response('down', 500)),
      ),
    ));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('subscription-error')), findsOneWidget);
    expect(find.byKey(const Key('subscription-empty')), findsNothing);
    expect(find.text('ELITE'), findsNothing);
    expect(find.text('Personal mode — all features unlocked'), findsNothing);
  });

  testWidgets('an empty status file is not an error or an elite tier', (tester) async {
    await tester.pumpWidget(MaterialApp(
      home: SubscriptionScreen(
        serverUrl: 'http://127.0.0.1:1',
        client: MockClient((_) async => http.Response('{"features":{}}', 200)),
      ),
    ));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('subscription-empty')), findsOneWidget);
    expect(find.byKey(const Key('subscription-error')), findsNothing);
    expect(find.text('ELITE'), findsNothing);
  });

  testWidgets('a failed refresh keeps the last verified tier and says so', (tester) async {
    var calls = 0;
    await tester.pumpWidget(MaterialApp(
      home: SubscriptionScreen(
        serverUrl: 'http://127.0.0.1:1',
        authToken: 'token',
        client: MockClient((req) async {
          calls += 1;
          if (calls > 3) return http.Response('down', 500);
          return switch (req.url.path) {
            '/api/billing/tiers' => http.Response('{"features":{"elite":{"voice_enabled":true}}}', 200),
            '/api/billing/subscription' => http.Response('{"tier":"elite"}', 200),
            _ => http.Response('{"counters":{}}', 200),
          };
        }),
      ),
    ));
    await tester.pumpAndSettle();
    expect(find.text('ELITE'), findsOneWidget);
    expect(find.byKey(const Key('subscription-error')), findsNothing);

    await tester.tap(find.byKey(const Key('subscription-retry')));
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('subscription-error')), findsOneWidget);
    expect(find.text('ELITE'), findsOneWidget);
    expect(find.byKey(const Key('subscription-empty')), findsNothing);
  });
}
