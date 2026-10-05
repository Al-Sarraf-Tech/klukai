// ProfileScreen with an injected fake WardrobeService and a MockClient for the
// milestones/stats fetches. ProfileScreen no longer imports package:web, so
// this runs on the VM:  flutter test test/screens/profile_screen_test.dart
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:companion_app/models/her_day.dart';
import 'package:companion_app/screens/profile_screen.dart';
import 'package:companion_app/services/wardrobe_service.dart';

import '../support/her_day_fixtures.dart';

Future<FakeWardrobeService> _pump(WidgetTester tester, FakeWardrobeService fake) async {
  // Tall enough that every section is laid out without scrolling.
  tester.view.physicalSize = const Size(390, 3000);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.reset);
  await tester.pumpWidget(MaterialApp(
    home: ProfileScreen(
      serverUrl: 'http://127.0.0.1:1',
      affectionScore: 420,
      affectionLevel: 4,
      affectionLevelName: 'Trusted',
      wardrobeService: fake,
      client: MockClient((_) async => http.Response('{}', 404)),
    ),
  ));
  await tester.pumpAndSettle();
  return fake;
}

String _statValue(WidgetTester tester, String label) {
  final row = find.ancestor(of: find.text(label), matching: find.byType(Row)).first;
  final texts = tester.widgetList<Text>(find.descendant(of: row, matching: find.byType(Text)));
  return texts.last.data!;
}

void main() {
  testWidgets('renders TODAY schedule, wardrobe groups, TODAY card and worn log',
      (tester) async {
    final fake = await _pump(tester, FakeWardrobeService());
    expect(fake.outfitCalls, 1);
    expect(fake.herDayCalls, 1);
    expect(fake.logCalls, 1);

    expect(find.byKey(const Key('profile-today')), findsOneWidget);
    expect(find.byKey(const Key('schedule-current')), findsOneWidget);
    expect(find.byKey(const Key('profile-wardrobe')), findsOneWidget);
    expect(find.text('CANON'), findsOneWidget);
    expect(find.text('EVERYDAY KIT'), findsOneWidget);
    expect(find.text('LV 5'), findsOneWidget);
    expect(tester.widget<Text>(find.byKey(const Key('today-reason'))).data, 'Off-duty plans.');
    expect(find.byKey(const Key('profile-worn')), findsOneWidget);
    expect(find.text('OCT 03'), findsOneWidget);
    expect(_statValue(tester, 'CURRENT OUTFIT'), 'SPEED STAR');
    // The old hard-coded, non-canon list is gone.
    expect(find.text('Midnight Sovereign'), findsNothing);
    expect(find.text('Starlit Vow'), findsNothing);
  });

  testWidgets('tapping an unlocked outfit POSTs it and refreshes', (tester) async {
    final fake = await _pump(
      tester,
      FakeWardrobeService(outfits: [
        Wardrobe.fromJson(outfitsJson()),
        Wardrobe.fromJson(outfitsJson(
            today: outfitJson(
                id: 'hangar_coveralls', name: 'Hangar Coveralls', source: 'commander', reason: ''))),
      ]),
    );
    await tester.tap(find.byKey(const Key('wardrobe-item-hangar_coveralls')));
    await tester.pumpAndSettle();

    expect(fake.costumeRequests, ['hangar_coveralls']);
    expect(fake.outfitCalls, 2);
    expect(fake.herDayCalls, 2);
    expect(fake.logCalls, 2);
    expect(tester.widget<Text>(find.byKey(const Key('today-reason'))).data, 'Your request.');
    expect(_statValue(tester, 'CURRENT OUTFIT'), 'HANGAR COVERALLS');
    expect(find.byKey(const Key('wardrobe-snackbar')), findsNothing);
  });

  testWidgets('403 from the server: "Not yet." and no refresh', (tester) async {
    final fake = await _pump(
        tester, FakeWardrobeService(setCostumeError: WardrobeServiceException(403)));
    await tester.tap(find.byKey(const Key('wardrobe-item-blazing_star')));
    await tester.pump();
    expect(find.text('Not yet.'), findsOneWidget);
    expect(fake.outfitCalls, 1);
    expect(_statValue(tester, 'CURRENT OUTFIT'), 'SPEED STAR');
  });

  testWidgets('400 and transport errors get their own lines', (tester) async {
    final fake = await _pump(
        tester, FakeWardrobeService(setCostumeError: WardrobeServiceException(400)));
    await tester.tap(find.byKey(const Key('wardrobe-item-blazing_star')));
    await tester.pump();
    expect(find.text("That isn't in my wardrobe, Commander."), findsOneWidget);

    fake.setCostumeError = WardrobeServiceException(500);
    await tester.tap(find.byKey(const Key('wardrobe-item-blazing_star')));
    await tester.pump();
    expect(find.text('Comms disrupted. Try again.'), findsOneWidget);

    fake.setCostumeError = StateError('socket closed');
    await tester.tap(find.byKey(const Key('wardrobe-item-blazing_star')));
    await tester.pump();
    expect(find.text('Comms disrupted. Try again.'), findsOneWidget);
  });

  testWidgets('a locked tile answers locally without a POST', (tester) async {
    final fake = await _pump(tester, FakeWardrobeService());
    await tester.tap(find.byKey(const Key('wardrobe-item-immaculate_service')));
    await tester.pump();
    expect(find.text('Not yet.'), findsOneWidget);
    expect(fake.costumeRequests, isEmpty);
  });

  testWidgets('every fetch failing hides the new sections and keeps the dossier',
      (tester) async {
    await _pump(
      tester,
      FakeWardrobeService(
        outfits: [WardrobeServiceException(503)],
        herDays: [WardrobeServiceException(503)],
        logs: [WardrobeServiceException(503)],
      ),
    );
    expect(tester.takeException(), isNull);
    expect(find.byKey(const Key('profile-today')), findsNothing);
    expect(find.byKey(const Key('profile-wardrobe')), findsNothing);
    expect(find.byKey(const Key('profile-worn')), findsNothing);
    expect(find.text('CLASSIFIED PROFILE'), findsOneWidget);
    expect(_statValue(tester, 'CURRENT OUTFIT'), '—');
  });

  testWidgets('outfits down but her-day up: CURRENT OUTFIT falls back to her-day',
      (tester) async {
    await _pump(tester, FakeWardrobeService(outfits: [WardrobeServiceException(503)]));
    expect(find.byKey(const Key('profile-wardrobe')), findsNothing);
    expect(find.byKey(const Key('profile-today')), findsOneWidget);
    expect(_statValue(tester, 'CURRENT OUTFIT'), 'SPEED STAR');
  });

  testWidgets('milestones and stats come through the injected client', (tester) async {
    tester.view.physicalSize = const Size(390, 3000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    final paths = <String>[];
    await tester.pumpWidget(MaterialApp(
      home: ProfileScreen(
        serverUrl: 'http://127.0.0.1:1',
        affectionScore: 420,
        affectionLevel: 4,
        affectionLevelName: 'Trusted',
        wardrobeService: FakeWardrobeService(),
        client: MockClient((req) async {
          paths.add(req.url.path);
          return switch (req.url.path) {
            '/api/milestones' => http.Response('{"milestones":{"first_message":"2026-04-01"}}', 200),
            _ => http.Response('{"affection":{"total_interactions":77}}', 200),
          };
        }),
      ),
    ));
    await tester.pumpAndSettle();
    expect(paths, ['/api/milestones', '/api/user/stats']);
    expect(_statValue(tester, 'INTERACTIONS'), '77');
    expect(_statValue(tester, 'MILESTONES'), '1');
  });
}
