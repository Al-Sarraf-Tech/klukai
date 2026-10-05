// ChatHeader at real phone widths. VM-runnable:
//   flutter test test/widgets/chat_header_test.dart
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:companion_app/models/her_day.dart';
import 'package:companion_app/widgets/chat_header.dart';

import '../support/her_day_fixtures.dart';

class _Taps {
  final calls = <String>[];
  VoidCallback on(String name) => () => calls.add(name);
}

Future<_Taps> _pump(
  WidgetTester tester,
  double width, {
  bool connected = true,
  String levelName = 'Trusted',
  String mood = 'battle_ready',
  bool withDay = true,
}) async {
  tester.view.physicalSize = Size(width, 844);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.reset);
  final taps = _Taps();
  await tester.pumpWidget(MaterialApp(
    home: Scaffold(
      body: SingleChildScrollView(
        child: ChatHeader(
          connected: connected,
          mood: mood,
          glow: Colors.cyan,
          bpm: 72,
          heartbeatColor: Colors.cyan,
          ambientMuted: true,
          affectionScore: 640,
          affectionLevel: 6,
          affectionLevelName: levelName,
          lastAffectionDelta: 12,
          herDay: withDay
              ? HerDay.fromJson(herDayJson(
                  label: 'Hangar · re-torquing every bolt on the swing arm, twice',
                  outfit: outfitJson(name: 'Astral Luminous (Midnight Jetstream)')))
              : null,
          onOpenProfile: taps.on('profile'),
          onHerDayTap: taps.on('her_day'),
          onToggleAmbient: taps.on('ambient'),
          onCompanion: taps.on('companion'),
          onHerPov: taps.on('her_pov'),
          onArchive: taps.on('archive'),
          onSubscription: taps.on('subscription'),
          onLogout: taps.on('logout'),
        ),
      ),
    ),
  ));
  await tester.pump();
  return taps;
}

void main() {
  for (final width in [320.0, 340.0, 360.0, 375.0, 390.0, 414.0, 430.0, 768.0, 1280.0]) {
    testWidgets('no overflow at ${width.toInt()}px', (tester) async {
      await _pump(tester, width, connected: false, levelName: 'Unspoken Covenant', mood: 'battle_ready');
      expect(tester.takeException(), isNull);
      expect(find.byKey(const Key('chat-header-companion')), findsOneWidget);
      expect(find.byKey(const Key('chat-header-exit')), findsOneWidget);
    });
  }

  testWidgets('wide: every action is a direct button, no overflow menu', (tester) async {
    await _pump(tester, 430);
    expect(find.byKey(const Key('chat-header-more')), findsNothing);
    for (final k in ['ambient', 'her-pov', 'archive', 'subscription']) {
      expect(find.byKey(Key('chat-header-$k')), findsOneWidget, reason: k);
    }
  });

  testWidgets('narrow: secondary actions fold into the menu and still work', (tester) async {
    final taps = await _pump(tester, 340);
    expect(find.byKey(const Key('chat-header-more')), findsOneWidget);
    expect(find.byKey(const Key('chat-header-ambient')), findsNothing);
    expect(find.byKey(const Key('chat-header-subscription')), findsNothing);

    for (final (label, call) in [
      ('Her POV', 'her_pov'),
      ('Subscription', 'subscription'),
      ('Ambient audio on', 'ambient'),
    ]) {
      // The heartbeat animates forever, so settle by time, not pumpAndSettle.
      await tester.tap(find.byKey(const Key('chat-header-more')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));
      await tester.tap(find.text(label));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));
      expect(taps.calls.last, call);
    }
  });

  testWidgets('companion, archive, exit, portrait and Her Day line call through', (tester) async {
    final taps = await _pump(tester, 390);
    await tester.tap(find.byKey(const Key('chat-header-companion')));
    await tester.tap(find.byKey(const Key('chat-header-archive')));
    await tester.tap(find.byKey(const Key('chat-header-exit')));
    await tester.tap(find.byKey(const Key('chat-header-portrait')));
    await tester.tap(find.byKey(const Key('her-day-status')));
    expect(taps.calls, ['companion', 'archive', 'logout', 'profile', 'her_day']);
    expect(find.byTooltip('Companion'), findsOneWidget);
  });

  testWidgets('link state text and no Her Day line without a day', (tester) async {
    await _pump(tester, 390, connected: false, withDay: false);
    expect(find.text('LINK DOWN'), findsOneWidget);
    expect(find.byKey(const Key('her-day-status')), findsNothing);
    await _pump(tester, 390);
    expect(find.text('LINK ACTIVE'), findsOneWidget);
  });
}
