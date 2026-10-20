// The HUD over her 3D stage. VM-runnable:
//   flutter test test/widgets/stage_hud_test.dart
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:companion_app/widgets/message_footer.dart';
import 'package:companion_app/widgets/stage_hud.dart';

Future<void> _pump(WidgetTester tester, Widget child, {double width = 390}) {
  tester.view.physicalSize = Size(width, 844);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.reset);
  return tester.pumpWidget(
    MaterialApp(
      home: Scaffold(
        body: Padding(padding: const EdgeInsets.all(12), child: child),
      ),
    ),
  );
}

/// Stands in for HudPointerShield (browser-only): sized by its child the
/// same way, so its rect is what the real shield would cover.
class _Shield extends StatelessWidget {
  const _Shield({required this.child});
  final Widget child;
  @override
  Widget build(BuildContext context) => child;
}

/// True when an enabled IgnorePointer sits above [finder].
bool _ignoresPointers(WidgetTester tester, Finder finder) => tester
    .widgetList<IgnorePointer>(
      find.ancestor(
        of: finder,
        matching: find.byType(IgnorePointer),
        matchRoot: true,
      ),
    )
    .any((w) => w.ignoring);

void main() {
  testWidgets('status tags: affinity, mood, heart, wearing', (tester) async {
    await _pump(
      tester,
      const Align(
        alignment: Alignment.topRight,
        child: StageStatusTags(
          level: 9,
          mood: 'PLEASED',
          bpm: 74,
          wearing: 'Stargazing Night',
        ),
      ),
    );
    for (final text in [
      'AFFINITY',
      'LV9',
      'MOOD',
      'PLEASED',
      'HEART',
      '74 bpm',
      'WEARING',
      'Stargazing Night',
    ]) {
      expect(find.text(text), findsOneWidget, reason: text);
    }
  });

  testWidgets('no outfit, no WEARING tag; a long one stays in bounds', (
    tester,
  ) async {
    await _pump(
      tester,
      const Align(
        alignment: Alignment.topRight,
        child: StageStatusTags(level: 3, mood: 'COMPOSED', bpm: 65),
      ),
    );
    expect(find.text('WEARING'), findsNothing);
    await _pump(
      tester,
      const Align(
        alignment: Alignment.topRight,
        child: StageStatusTags(
          level: 3,
          mood: 'COMPOSED',
          bpm: 65,
          wearing: 'Astral Luminous (Midnight Jetstream) with the long coat',
        ),
      ),
      width: 320,
    );
    expect(tester.takeException(), isNull);
  });

  group('status tags over the stage', () {
    const readOnly = [
      Key('stage-tag-affinity'),
      Key('stage-tag-mood'),
      Key('stage-tag-heart'),
    ];
    const wearingKey = Key('stage-tag-wearing');

    Future<void> pumpTags(
      WidgetTester tester, {
      VoidCallback? onTap,
      String wearing = 'Stargazing Night',
    }) => _pump(
      tester,
      Align(
        alignment: Alignment.topRight,
        child: StageStatusTags(
          level: 9,
          mood: 'PLEASED',
          bpm: 74,
          wearing: wearing,
          onTap: onTap,
          shield: (tag) => _Shield(child: tag),
        ),
      ),
    );

    List<Rect> shieldRects(WidgetTester tester) => [
      for (final e in find.byType(_Shield).evaluate())
        tester.getRect(find.byWidget(e.widget)),
    ];

    testWidgets('only WEARING is a control, shielded on its own box', (
      tester,
    ) async {
      var taps = 0;
      await pumpTags(tester, onTap: () => taps++);
      final shields = shieldRects(tester);
      expect(shields, [tester.getRect(find.byKey(wearingKey))]);
      // The rest of the column stays with the stage: the other tags, the
      // gaps and the space beside a shorter tag.
      final column = tester.getRect(find.byKey(const Key('stage-tags')));
      final affinity = tester.getRect(find.byKey(readOnly[0]));
      final mood = tester.getRect(find.byKey(readOnly[1]));
      expect(column.width, greaterThan(affinity.width));
      final open = [
        for (final key in readOnly) tester.getCenter(find.byKey(key)),
        Offset(mood.center.dx, (affinity.bottom + mood.top) / 2),
        Offset(column.left + 2, affinity.center.dy),
      ];
      for (final point in open) {
        expect(column.contains(point), isTrue, reason: '$point');
        expect(shields.where((r) => r.contains(point)), isEmpty);
      }
      for (final key in readOnly) {
        expect(_ignoresPointers(tester, find.byKey(key)), isTrue);
        await tester.tap(find.byKey(key), warnIfMissed: false);
      }
      expect(taps, 0);
      // WEARING opens her day.
      expect(_ignoresPointers(tester, find.byKey(wearingKey)), isFalse);
      await tester.tap(find.byKey(wearingKey));
      expect(taps, 1);
    });

    testWidgets('no outfit, or nothing to open: no shield at all', (
      tester,
    ) async {
      await pumpTags(tester, onTap: () {}, wearing: '');
      expect(find.byKey(wearingKey), findsNothing);
      expect(find.byType(_Shield), findsNothing);

      await pumpTags(tester);
      expect(find.byType(_Shield), findsNothing);
      for (final key in [...readOnly, wearingKey]) {
        expect(
          _ignoresPointers(tester, find.byKey(key)),
          isTrue,
          reason: '$key',
        );
      }
    });
  });

  testWidgets('tracking readout lines', (tester) async {
    await _pump(
      tester,
      const TrackingReadout(
        lines: ['TRACKING', 'IDLE · BREATH', 'GAZE: COMMANDER'],
      ),
    );
    expect(find.text('◢ TRACKING'), findsOneWidget);
    expect(find.text('◢ GAZE: COMMANDER'), findsOneWidget);
    // Decorative to pointers: drags there turn her.
    expect(
      _ignoresPointers(tester, find.byKey(const Key('stage-tracking'))),
      isTrue,
    );
  });

  testWidgets('dialogue: her line and VOICE; tapping calls through', (
    tester,
  ) async {
    var taps = 0;
    await _pump(
      tester,
      DialoguePanel(
        line: '...Hmph. Sit. The stars are clear tonight — I checked twice.',
        voiceState: VoicePlayState.idle,
        onVoice: () => taps++,
      ),
    );
    expect(find.text('▸ KLUKAI'), findsOneWidget);
    expect(find.textContaining('I checked twice.'), findsOneWidget);
    expect(find.text('VOICE'), findsOneWidget);
    expect(
      tester.getSize(find.byKey(const Key('voice-play'))).height,
      greaterThanOrEqualTo(44),
    );
    await tester.tap(find.byKey(const Key('voice-play')));
    expect(taps, 1);
  });

  testWidgets('dialogue: busy VOICE ignores taps', (tester) async {
    var taps = 0;
    await _pump(
      tester,
      DialoguePanel(
        line: 'Sit.',
        voiceState: VoicePlayState.loading,
        onVoice: () => taps++,
      ),
    );
    expect(find.byType(CircularProgressIndicator), findsOneWidget);
    await tester.tap(find.byKey(const Key('voice-play')));
    expect(taps, 0);
  });

  testWidgets('dialogue: no VOICE while streaming, thinking or empty', (
    tester,
  ) async {
    for (final panel in [
      DialoguePanel(
        line: 'Half a sent',
        streaming: true,
        voiceState: VoicePlayState.idle,
        onVoice: () {},
      ),
      DialoguePanel(
        line: 'Old line',
        status: 'Checking the hangar',
        voiceState: VoicePlayState.idle,
        onVoice: () {},
      ),
      DialoguePanel(line: '', voiceState: VoicePlayState.idle, onVoice: () {}),
    ]) {
      await _pump(tester, panel);
      expect(find.byKey(const Key('voice-play')), findsNothing);
    }
    expect(find.text('Awaiting orders, Commander.'), findsOneWidget);
  });

  testWidgets('dialogue: her thinking status replaces the line', (
    tester,
  ) async {
    await _pump(
      tester,
      DialoguePanel(
        line: 'Old line',
        status: 'Checking the hangar',
        voiceState: VoicePlayState.idle,
        onVoice: () {},
      ),
    );
    expect(find.text('CHECKING THE HANGAR'), findsOneWidget);
    expect(find.text('Old line'), findsNothing);
    expect(find.byType(LinearProgressIndicator), findsOneWidget);
  });
}
