// SubscriptionScreen with an injected MockClient. The screen does not import
// package:web, so this runs on the VM:
//   flutter test test/screens/subscription_screen_test.dart
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:companion_app/screens/subscription_screen.dart';

const _tiers = '{"features":{"elite":{"voice_enabled":true,"images_per_day":null}}}';

/// Pumps the screen. On a 401 the screen keeps its spinner (the bounce is
/// navigating away), so those cases pump a few frames instead of settling.
Future<List<String>> _pump(
  WidgetTester tester, {
  required int subscriptionStatus,
  int usageStatus = 200,
  String? token = 'tok',
  VoidCallback? onAuthExpired,
  bool settle = true,
}) async {
  final paths = <String>[];
  await tester.pumpWidget(MaterialApp(
    home: SubscriptionScreen(
      serverUrl: 'http://127.0.0.1:1',
      authToken: token,
      client: MockClient((req) async {
        paths.add(req.url.path);
        return switch (req.url.path) {
          '/api/billing/tiers' => http.Response(_tiers, 200),
          '/api/billing/subscription' =>
            http.Response('{"tier":"elite"}', subscriptionStatus),
          '/api/billing/usage' => http.Response(
              '{"counters":{"images":{"used":3,"limit":20}}}', usageStatus),
          _ => http.Response('{}', 404),
        };
      }),
      onAuthExpired: onAuthExpired,
    ),
  ));
  if (settle) {
    await tester.pumpAndSettle();
  } else {
    for (var i = 0; i < 4; i++) {
      await tester.pump();
    }
  }
  return paths;
}

void main() {
  testWidgets('renders tier, usage and features on 200', (tester) async {
    final paths = await _pump(tester, subscriptionStatus: 200);
    expect(paths, [
      '/api/billing/tiers',
      '/api/billing/subscription',
      '/api/billing/usage',
    ]);
    expect(find.text('ELITE'), findsOneWidget);
    expect(find.text('Usage today'), findsOneWidget);
    expect(find.text('3 / 20'), findsOneWidget);
    expect(find.text('Voice'), findsOneWidget);
    expect(find.text('Images: unlimited'), findsOneWidget);
  });

  testWidgets('a dead token on the subscription call bounces to login', (tester) async {
    var bounced = 0;
    final paths = await _pump(
      tester,
      subscriptionStatus: 401,
      onAuthExpired: () => bounced++,
      settle: false,
    );
    expect(bounced, 1);
    // It stopped there rather than carrying on with a token it knows is dead.
    expect(paths, ['/api/billing/tiers', '/api/billing/subscription']);
    // No fake "ELITE" view was rendered on the way out.
    expect(find.text('ELITE'), findsNothing);
  });

  testWidgets('a dead token on the usage call bounces to login', (tester) async {
    var bounced = 0;
    await _pump(
      tester,
      subscriptionStatus: 200,
      usageStatus: 401,
      onAuthExpired: () => bounced++,
      settle: false,
    );
    expect(bounced, 1);
  });

  testWidgets('a 503 is not a dead token: renders the public tier view', (tester) async {
    var bounced = 0;
    await _pump(
      tester,
      subscriptionStatus: 503,
      usageStatus: 503,
      onAuthExpired: () => bounced++,
    );
    expect(bounced, 0);
    expect(find.text('ELITE'), findsOneWidget); // default tier
    expect(find.text('Usage today'), findsNothing);
  });

  testWidgets('without a token only the public tiers call is made', (tester) async {
    var bounced = 0;
    final paths = await _pump(
      tester,
      subscriptionStatus: 401,
      token: null,
      onAuthExpired: () => bounced++,
    );
    expect(paths, ['/api/billing/tiers']);
    expect(bounced, 0);
  });
}
