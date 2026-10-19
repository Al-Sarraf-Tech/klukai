// HudDayScreen with the scriptable FakeWardrobeService; VM-runnable.
//   flutter test test/screens/hud_day_screen_test.dart
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:companion_app/models/her_day.dart';
import 'package:companion_app/screens/hud_day_screen.dart';
import 'package:companion_app/services/wardrobe_service.dart';

import '../support/her_day_fixtures.dart';

final _now = DateTime(2026, 10, 4, 14, 30);

Future<FakeWardrobeService> _pump(
  WidgetTester tester,
  FakeWardrobeService fake, {
  double width = 390,
  VoidCallback? onAuthExpired,
  Listenable? refresh,
  bool settle = true,
}) async {
  tester.view.physicalSize = Size(width, 2000);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.reset);
  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(
        body: HudDayScreen(
          serverUrl: 'http://127.0.0.1:1',
          wardrobeService: fake,
          onAuthExpired: onAuthExpired,
          refresh: refresh,
          now: () => _now,
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

ScheduleBlock _b(String start, String end, {bool current = false}) =>
    ScheduleBlock(start: start, end: end, location: 'X', current: current);

void main() {
  testWidgets('equipped card, weather and requests readouts', (tester) async {
    await _pump(tester, FakeWardrobeService());
    expect(find.text('OPS LOG // TODAY'), findsOneWidget);
    expect(find.text('HER DAY'), findsOneWidget);
    expect(find.text('LIVE'), findsOneWidget);
    // The date comes from the server's day, not the device.
    expect(find.text('SUN 4 OCT'), findsOneWidget);

    expect(find.text('EQUIPPED TODAY'), findsOneWidget);
    expect(
      tester.widget<Text>(find.byKey(const Key('day-outfit-name'))).data,
      'Speed Star',
    );
    // Her own pick: her reason, in her words.
    expect(
      tester.widget<Text>(find.byKey(const Key('day-outfit-reason'))).data,
      '"Off-duty plans."',
    );
    expect(find.text('Clear · 14°C'), findsOneWidget);
    expect(find.text('None today'), findsOneWidget);
  });

  testWidgets('his granted request reads as his', (tester) async {
    await _pump(
      tester,
      FakeWardrobeService(
        herDays: [
          HerDay.fromJson(
            herDayJson(
              outfit: outfitJson(
                id: 'cat_cafe_date',
                name: 'Cat Café Date',
                source: 'commander',
                reason: '',
              ),
            ),
          ),
        ],
      ),
    );
    expect(find.text('Cat Café Date'), findsOneWidget);
    expect(find.text('Your request.'), findsOneWidget);
    expect(find.text('Yours today'), findsOneWidget);
  });

  testWidgets('timeline: done filled, current ACTIVE, later dimmed', (
    tester,
  ) async {
    await _pump(tester, FakeWardrobeService());
    // Fixture: late, morning, afternoon (current), evening.
    expect(find.byKey(const Key('day-active-block')), findsOneWidget);
    expect(find.byKey(const Key('day-active-tag')), findsOneWidget);
    expect(find.text('ACTIVE'), findsOneWidget);
    expect(find.text('12:00 — NOW'), findsOneWidget);
    expect(
      find.descendant(
        of: find.byKey(const Key('day-active-block')),
        matching: find.text('Hangar'),
      ),
      findsOneWidget,
    );
    expect(find.text('tuning the suspension'), findsOneWidget);
    expect(find.text('05:00'), findsOneWidget);
    expect(find.text('17:00'), findsOneWidget);

    // Later blocks use the dimmed (still 4.5:1) colour; done ones do not.
    final later = tester.widget<Text>(find.text('Off duty'));
    expect(later.style!.color, const Color(0xFF6D8796));
    final done = tester.widget<Text>(find.text('Range'));
    expect(done.style!.color, isNot(const Color(0xFF6D8796)));

    // Each block reads as one sentence to a screen reader.
    expect(
      find.bySemanticsLabel(RegExp(r'^12:00 to 17:00, active now\.')),
      findsOneWidget,
    );
    expect(
      find.bySemanticsLabel(RegExp(r'^05:00 to 08:00, done\.')),
      findsOneWidget,
    );
  });

  test('scheduleStates follows the server mark, then the clock', () {
    final marked = [
      _b('0500', '0800'),
      _b('0800', '1200'),
      _b('1200', '1700', current: true),
      _b('1700', '2100'),
    ];
    expect(scheduleStates(marked, _now), [
      BlockState.done,
      BlockState.done,
      BlockState.active,
      BlockState.later,
    ]);

    final unmarked = [
      _b('0500', '1200'),
      _b('1200', '1700'),
      _b('1700', '0000'),
    ];
    expect(scheduleStates(unmarked, DateTime(2026, 10, 4, 18)), [
      BlockState.done,
      BlockState.done,
      BlockState.active,
    ]);
    // Nothing fits the hour: finished blocks are done, the rest later.
    final gap = [_b('0500', '0800'), _b('1900', '2100')];
    expect(scheduleStates(gap, DateTime(2026, 10, 4, 10)), [
      BlockState.done,
      BlockState.later,
    ]);
    expect(hudHhmm('0930'), '09:30');
  });

  testWidgets('an override shows above the card', (tester) async {
    await _pump(
      tester,
      FakeWardrobeService(
        herDays: [
          HerDay.fromJson(
            herDayJson(label: 'Deployed · on mission', override: 'mission'),
          ),
        ],
      ),
    );
    expect(find.byKey(const Key('day-override')), findsOneWidget);
    expect(find.text('OVERRIDE · MISSION'), findsOneWidget);
  });

  testWidgets('a failed fetch offers RETRY; RETRY loads', (tester) async {
    final fake = await _pump(
      tester,
      FakeWardrobeService(
        herDays: [WardrobeServiceException(503), HerDay.fromJson(herDayJson())],
      ),
    );
    expect(find.byKey(const Key('day-error')), findsOneWidget);
    await tester.tap(find.text('RETRY'));
    await tester.pumpAndSettle();
    expect(fake.herDayCalls, 2);
    expect(find.byKey(const Key('day-equipped')), findsOneWidget);
  });

  testWidgets('a dead token goes to the shell, once', (tester) async {
    var bounced = 0;
    await _pump(
      tester,
      FakeWardrobeService(herDays: [WardrobeServiceException(401)]),
      onAuthExpired: () => bounced++,
      settle: false,
    );
    expect(bounced, 1);
  });

  testWidgets('the refresh signal reloads (Kit changed her outfit)', (
    tester,
  ) async {
    final signal = ValueNotifier(0);
    addTearDown(signal.dispose);
    final fake = await _pump(
      tester,
      FakeWardrobeService(
        herDays: [
          HerDay.fromJson(herDayJson()),
          HerDay.fromJson(
            herDayJson(
              outfit: outfitJson(
                id: 'blazing_star',
                name: 'Blazing Star',
                source: 'commander',
              ),
            ),
          ),
        ],
      ),
      refresh: signal,
    );
    expect(find.text('Speed Star'), findsOneWidget);
    signal.value++;
    await tester.pumpAndSettle();
    expect(fake.herDayCalls, 2);
    expect(find.text('Blazing Star'), findsOneWidget);
  });

  testWidgets('no roster says so', (tester) async {
    final json = herDayJson()..['schedule'] = <Map<String, dynamic>>[];
    await _pump(tester, FakeWardrobeService(herDays: [HerDay.fromJson(json)]));
    expect(find.byKey(const Key('day-empty')), findsOneWidget);
  });

  for (final width in [360.0, 430.0]) {
    testWidgets('no overflow at $width px', (tester) async {
      await _pump(tester, FakeWardrobeService(), width: width);
      expect(tester.takeException(), isNull);
    });
  }
}
