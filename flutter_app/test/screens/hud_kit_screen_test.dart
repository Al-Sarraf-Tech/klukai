// HudKitScreen with the scriptable FakeWardrobeService; VM-runnable.
//   flutter test test/screens/hud_kit_screen_test.dart
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:companion_app/models/her_day.dart';
import 'package:companion_app/screens/hud_kit_screen.dart';
import 'package:companion_app/services/wardrobe_service.dart';

import '../support/her_day_fixtures.dart';

Future<FakeWardrobeService> _pump(
  WidgetTester tester,
  FakeWardrobeService fake, {
  double width = 390,
  VoidCallback? onOutfitChanged,
  VoidCallback? onAuthExpired,
  bool settle = true,
}) async {
  tester.view.physicalSize = Size(width, 2400);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.reset);
  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(
        body: HudKitScreen(
          serverUrl: 'http://127.0.0.1:1',
          wardrobeService: fake,
          onOutfitChanged: onOutfitChanged,
          onAuthExpired: onAuthExpired,
        ),
      ),
    ),
  );
  if (settle) {
    await tester.pumpAndSettle();
  } else {
    await tester.pump();
    await tester.pump();
  }
  return fake;
}

Finder _card(String id) => find.byKey(ValueKey('kit-card-$id'));

Future<void> _chip(WidgetTester tester, KitFilter f) async {
  await tester.tap(find.byKey(ValueKey('kit-filter-$f')));
  await tester.pumpAndSettle();
}

String _reply(WidgetTester tester) => tester
    .widget<Text>(
      find.descendant(
        of: find.byKey(const Key('kit-reply')),
        matching: find.byType(Text),
      ),
    )
    .data!;

void main() {
  testWidgets('header counts, cards from the catalog, EQUIPPED and LV', (
    tester,
  ) async {
    await _pump(tester, FakeWardrobeService());
    expect(find.text('KIT // WARDROBE'), findsOneWidget);
    expect(find.text('WHAT SHE WEARS'), findsOneWidget);
    // Fixture: five visible, three unlocked.
    expect(
      tester.widget<Text>(find.byKey(const Key('kit-count'))).data,
      '3 / 5 UNLOCKED',
    );
    expect(find.text('Blazing Star'), findsOneWidget);
    expect(find.text('DUTY · CANON'), findsOneWidget);
    expect(find.text('WORK · ORIGINAL'), findsOneWidget);
    expect(find.text('OFF-DUTY · ORIGINAL'), findsOneWidget);
    expect(
      find.text('Military-tactical outfit befitting an elite squad leader'),
      findsOneWidget,
    );
    expect(
      find.descendant(of: _card('speed_star'), matching: find.text('EQUIPPED')),
      findsOneWidget,
    );
    expect(
      find.descendant(of: _card('blazing_star'), matching: find.text('LV 0')),
      findsOneWidget,
    );
  });

  testWidgets('locked kit is dimmed and hides its blurb', (tester) async {
    await _pump(tester, FakeWardrobeService());
    expect(
      find.byKey(const ValueKey('kit-locked-immaculate_service')),
      findsOneWidget,
    );
    expect(find.byKey(const ValueKey('kit-locked-his_jacket')), findsOneWidget);
    expect(find.text(kKitLockedLine), findsNWidgets(2));
    expect(find.text('A battle maid is a combat configuration'), findsNothing);
    expect(
      find.descendant(
        of: _card('immaculate_service'),
        matching: find.text('LV 5'),
      ),
      findsOneWidget,
    );
  });

  testWidgets('tapping locked kit asks for nothing; she says no', (
    tester,
  ) async {
    final fake = await _pump(tester, FakeWardrobeService());
    await tester.tap(_card('immaculate_service'));
    await tester.pumpAndSettle();
    expect(fake.costumeRequests, isEmpty);
    expect(_reply(tester), '"Not yet."');
  });

  testWidgets('category chips map onto the catalog categories', (tester) async {
    await _pump(tester, FakeWardrobeService());

    await _chip(tester, KitFilter.duty);
    expect(_card('blazing_star'), findsOneWidget);
    expect(_card('hangar_coveralls'), findsOneWidget);
    expect(_card('speed_star'), findsNothing);

    await _chip(tester, KitFilter.offDuty);
    expect(_card('speed_star'), findsOneWidget); // rider
    expect(_card('his_jacket'), findsOneWidget); // off_duty
    expect(_card('blazing_star'), findsNothing);

    await _chip(tester, KitFilter.formal);
    expect(_card('immaculate_service'), findsOneWidget);
    expect(_card('speed_star'), findsNothing);

    await _chip(tester, KitFilter.seasonal);
    expect(find.byKey(const Key('kit-empty')), findsOneWidget);

    await _chip(tester, KitFilter.all);
    expect(_card('speed_star'), findsOneWidget);
    expect(_card('immaculate_service'), findsOneWidget);
  });

  test('every catalog category sits under a chip', () {
    const catalog = [
      'duty',
      'rider',
      'swim',
      'formal',
      'oath',
      'off_duty',
      'work',
      'training',
      'weather',
      'sleep',
      'seasonal',
    ];
    for (final c in catalog) {
      final groups = kKitCategoryGroups.entries
          .where((e) => e.value.contains(c))
          .toList();
      expect(groups, hasLength(1), reason: c);
    }
  });

  testWidgets('REQUEST AN OUTFIT: pick, she agrees, the kit refreshes', (
    tester,
  ) async {
    var changed = 0;
    final fake = await _pump(
      tester,
      FakeWardrobeService(),
      onOutfitChanged: () => changed++,
    );
    expect(fake.outfitCalls, 1);
    await tester.tap(find.byKey(const Key('kit-request')));
    await tester.pumpAndSettle();
    // Only unlocked outfits she is not already wearing.
    expect(find.byKey(const ValueKey('kit-pick-blazing_star')), findsOneWidget);
    expect(
      find.byKey(const ValueKey('kit-pick-hangar_coveralls')),
      findsOneWidget,
    );
    expect(find.byKey(const ValueKey('kit-pick-speed_star')), findsNothing);
    expect(
      find.byKey(const ValueKey('kit-pick-immaculate_service')),
      findsNothing,
    );

    await tester.tap(find.byKey(const ValueKey('kit-pick-blazing_star')));
    await tester.pumpAndSettle();
    expect(fake.costumeRequests, ['blazing_star']);
    expect(fake.outfitCalls, 2);
    expect(changed, 1);
    expect(_reply(tester), 'REQUEST GRANTED · BLAZING STAR');
  });

  testWidgets('a refused request shows her refusal line', (tester) async {
    final fake = await _pump(
      tester,
      FakeWardrobeService(
        setCostumeError: WardrobeServiceException(403, decision: 'not_today'),
      ),
    );
    await tester.tap(find.byKey(const Key('kit-request')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('kit-pick-hangar_coveralls')));
    await tester.pumpAndSettle();
    expect(fake.costumeRequests, ['hangar_coveralls']);
    expect(_reply(tester), '"...Not today. Ask me on a day that matters."');

    fake.setCostumeError = WardrobeServiceException(403, decision: 'deny');
    await tester.tap(find.byKey(const Key('kit-request')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('kit-pick-blazing_star')));
    await tester.pumpAndSettle();
    expect(_reply(tester), '"What onesie."');

    fake.setCostumeError = WardrobeServiceException(500);
    await tester.tap(find.byKey(const Key('kit-request')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('kit-pick-blazing_star')));
    await tester.pumpAndSettle();
    expect(_reply(tester), '"Comms disrupted. Try again."');
  });

  testWidgets('tapping an unlocked card asks first, then requests', (
    tester,
  ) async {
    final fake = await _pump(tester, FakeWardrobeService());
    await tester.tap(_card('hangar_coveralls'));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('kit-confirm')), findsOneWidget);
    expect(fake.costumeRequests, isEmpty);

    await tester.tap(find.byKey(const Key('kit-confirm')));
    await tester.pumpAndSettle();
    expect(fake.costumeRequests, ['hangar_coveralls']);
  });

  testWidgets('the worn log is reachable here', (tester) async {
    await _pump(tester, FakeWardrobeService());
    expect(find.byKey(const Key('kit-worn')), findsOneWidget);
    expect(find.text('OCT 03'), findsNothing); // collapsed
    await tester.tap(find.text('WORN THIS MONTH'));
    await tester.pumpAndSettle();
    expect(find.text('OCT 03'), findsOneWidget);
    expect(find.text('YOUR REQUEST'), findsOneWidget);
  });

  testWidgets('a failed catalog offers RETRY; RETRY loads', (tester) async {
    final fake = await _pump(
      tester,
      FakeWardrobeService(
        outfits: [
          WardrobeServiceException(503),
          Wardrobe.fromJson(outfitsJson()),
        ],
      ),
    );
    expect(find.byKey(const Key('kit-error')), findsOneWidget);
    await tester.tap(find.text('RETRY'));
    await tester.pumpAndSettle();
    expect(fake.outfitCalls, 2);
    expect(_card('speed_star'), findsOneWidget);
  });

  testWidgets('a dead token goes to the shell, once', (tester) async {
    var bounced = 0;
    await _pump(
      tester,
      FakeWardrobeService(outfits: [WardrobeServiceException(401)]),
      onAuthExpired: () => bounced++,
      settle: false,
    );
    expect(bounced, 1);
  });

  for (final width in [360.0, 430.0]) {
    testWidgets('no overflow at $width px', (tester) async {
      await _pump(tester, FakeWardrobeService(), width: width);
      expect(tester.takeException(), isNull);
    });
  }
}
