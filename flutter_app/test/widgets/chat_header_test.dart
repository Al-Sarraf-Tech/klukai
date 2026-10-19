// The HUD ChatHeader at real phone widths. VM-runnable:
//   flutter test test/widgets/chat_header_test.dart
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:companion_app/models/her_day.dart';
import 'package:companion_app/widgets/chat_header.dart';

import '../support/her_day_fixtures.dart';

class _Taps {
  final calls = <String>[];
  final menu = <ChatMenuAction>[];
  final views = <ChatViewMode>[];
}

Future<_Taps> _pump(
  WidgetTester tester,
  double width, {
  bool connected = true,
  int level = 6,
  String levelName = 'Trusted',
  String mood = 'battle_ready',
  int bpm = 72,
  bool spiking = false,
  bool withDay = true,
  bool compact = false,
  ChatViewMode view = ChatViewMode.chat,
  HerDay? day,
}) async {
  tester.view.physicalSize = Size(width, 844);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.reset);
  final taps = _Taps();
  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(
        body: SingleChildScrollView(
          child: ChatHeader(
            connected: connected,
            mood: mood,
            bpm: bpm,
            spiking: spiking,
            affectionScore: 640,
            affectionLevel: level,
            affectionLevelName: levelName,
            lastAffectionDelta: 12,
            view: view,
            onViewChanged: taps.views.add,
            onMenu: taps.menu.add,
            compact: compact,
            herDay: withDay
                ? day ??
                      HerDay.fromJson(
                        herDayJson(
                          label:
                              'Hangar · re-torquing every bolt on the swing arm, twice',
                          outfit: outfitJson(
                            name: 'Astral Luminous (Midnight Jetstream)',
                          ),
                        ),
                      )
                : null,
            onOpenProfile: () => taps.calls.add('profile'),
            onHerDayTap: () => taps.calls.add('her_day'),
          ),
        ),
      ),
    ),
  );
  await tester.pump();
  return taps;
}

int _lit(WidgetTester tester) =>
    find.byKey(const Key('pip-on')).evaluate().length;

void main() {
  for (final width in [320.0, 340.0, 360.0, 375.0, 390.0, 414.0, 430.0]) {
    testWidgets('no overflow at ${width.toInt()}px', (tester) async {
      await _pump(
        tester,
        width,
        connected: false,
        levelName: 'Unspoken Covenant',
        mood: 'quietly_pleased',
        bpm: 180,
      );
      expect(tester.takeException(), isNull);
      expect(find.text('H.I.D.E. 404 // SECURE LINK'), findsOneWidget);
      expect(find.byKey(const Key('chat-header-portrait')), findsOneWidget);
      expect(find.byKey(const Key('chat-chip-wearing')), findsOneWidget);
      expect(find.byKey(const Key('chat-chip-now')), findsOneWidget);
    });

    testWidgets('compact (keyboard up) has no overflow at ${width.toInt()}px', (
      tester,
    ) async {
      await _pump(tester, width, compact: true, bpm: 165);
      expect(tester.takeException(), isNull);
      expect(find.byKey(const Key('chat-header-portrait')), findsNothing);
      expect(find.byKey(const Key('chat-chip-wearing')), findsNothing);
      expect(find.text('165 BPM'), findsOneWidget);
    });
  }

  testWidgets('readouts: link, affinity pips and level, mood, heart', (
    tester,
  ) async {
    await _pump(tester, 390, level: 6, mood: 'quietly_pleased', bpm: 72);
    expect(find.text('KLUKAI'), findsOneWidget);
    expect(find.text('ONLINE'), findsOneWidget);
    expect(find.text('LV6'), findsOneWidget);
    expect(_lit(tester), 6);
    expect(find.byKey(const Key('pip-off')), findsNWidgets(3));
    expect(find.text('PLEASED'), findsOneWidget);
    expect(find.text('72 BPM'), findsOneWidget);
    expect(tester.widget<HudEcg>(find.byType(HudEcg)).bpm, 72);
  });

  testWidgets('a dropped link reads LINK DOWN', (tester) async {
    await _pump(tester, 390, connected: false);
    expect(find.text('LINK DOWN'), findsOneWidget);
    expect(find.text('ONLINE'), findsNothing);
  });

  testWidgets('affinity pips clamp to 0…9', (tester) async {
    await _pump(tester, 390, level: 12);
    expect(_lit(tester), kMaxAffinityLevel);
    expect(find.text('LV9'), findsOneWidget);
    await _pump(tester, 390, level: 0);
    expect(_lit(tester), 0);
    expect(find.text('LV0'), findsOneWidget);
  });

  testWidgets('the heart trace runs red during a surge', (tester) async {
    await _pump(tester, 390, spiking: true, bpm: 160);
    final ecg = tester.widget<HudEcg>(find.byType(HudEcg));
    expect(ecg.bpm, 160);
    expect(ecg.color, isNot(equals(const HudEcg(bpm: 1).color)));
  });

  testWidgets('WEARING and NOW come from Her Day; tapping opens it', (
    tester,
  ) async {
    final taps = await _pump(
      tester,
      390,
      day: HerDay.fromJson(
        herDayJson(
          label: 'Hangar · tuning the bike',
          outfit: outfitJson(name: 'Stargazing Night'),
        ),
      ),
    );
    expect(find.text('Stargazing Night'), findsOneWidget);
    expect(find.text('Hangar · tuning the bike'), findsOneWidget);
    await tester.tap(find.byKey(const Key('chat-chip-now')));
    await tester.tap(find.byKey(const Key('chat-chip-wearing')));
    expect(taps.calls, ['her_day', 'her_day']);
  });

  testWidgets('no Her Day: no chips, readouts carry on', (tester) async {
    await _pump(tester, 390, withDay: false);
    expect(find.byKey(const Key('chat-chip-now')), findsNothing);
    expect(find.text('KLUKAI'), findsOneWidget);
  });

  testWidgets('NOW falls back to the current schedule block', (tester) async {
    final day = HerDay.fromJson({
      ...herDayJson(label: ''),
      'status': {'label': ''},
    });
    final now = HerDayChips.nowLine(day);
    final current = day.schedule.where((b) => b.current).toList();
    expect(now, current.isEmpty ? '' : current.first.line);
  });

  testWidgets('the portrait opens her dossier', (tester) async {
    final taps = await _pump(tester, 390);
    await tester.tap(find.byKey(const Key('chat-header-portrait')));
    expect(taps.calls, ['profile']);
  });

  testWidgets('CHAT | 3D flips the view; the current face is inert', (
    tester,
  ) async {
    final taps = await _pump(tester, 390);
    await tester.tap(find.byKey(const Key('view-flip-chat')));
    await tester.tap(find.byKey(const Key('view-flip-3d')));
    expect(taps.views, [ChatViewMode.threeD]);
    expect(
      tester.getSize(find.byKey(const Key('view-flip-3d'))).height,
      greaterThanOrEqualTo(44),
    );
  });

  testWidgets('the menu holds every entry point and reports the choice', (
    tester,
  ) async {
    final taps = await _pump(tester, 360);
    expect(
      tester.getSize(find.byKey(const Key('chat-menu'))).height,
      greaterThanOrEqualTo(44),
    );
    for (final action in ChatMenuAction.values) {
      await tester.tap(find.byKey(const Key('chat-menu')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));
      await tester.tap(find.byKey(Key('chat-menu-${action.name}')));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));
    }
    expect(taps.menu, ChatMenuAction.values);
  });

  testWidgets('reduced motion holds the heart trace still', (tester) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    await tester.pumpWidget(
      const MediaQuery(
        data: MediaQueryData(disableAnimations: true),
        child: Directionality(
          textDirection: TextDirection.ltr,
          child: SizedBox(width: 120, height: 22, child: HudEcg(bpm: 72)),
        ),
      ),
    );
    // A running trace would leave a pending frame; a still one settles.
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
  });

  test('moodLabel uses the shared mood names', () {
    expect(moodLabel('composed'), 'COMPOSED');
    expect(moodLabel('quietly_pleased'), 'PLEASED');
    expect(moodLabel('some_new_mood'), 'SOME_NEW_MOOD');
  });
}
