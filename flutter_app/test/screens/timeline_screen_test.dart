// TimelineScreen: milestone stamps are shown in the Commander's local time,
// and a legacy naive stamp (written by datetime.now() in the UTC container)
// is read as UTC rather than as local wall-clock digits. VM-runnable:
//   flutter test test/screens/timeline_screen_test.dart
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:companion_app/screens/timeline_screen.dart';

String _two(int n) => n.toString().padLeft(2, '0');

/// The screen's own output format, computed from a local DateTime.
String _shown(DateTime local) =>
    '${local.year}-${_two(local.month)}-${_two(local.day)} ${_two(local.hour)}:${_two(local.minute)}';

void main() {
  group('TimelineScreen.formatMilestoneDate', () {
    test('an offset-qualified stamp is shown in local time', () {
      const iso = '2026-04-01T12:34:56.789012+00:00';
      expect(
        TimelineScreen.formatMilestoneDate(iso),
        _shown(DateTime.parse(iso).toLocal()),
      );
    });

    test('a Z stamp is shown in local time', () {
      const iso = '2026-04-01T23:59:00Z';
      expect(
        TimelineScreen.formatMilestoneDate(iso),
        _shown(DateTime.parse(iso).toLocal()),
      );
    });

    test('a naive legacy stamp is read as UTC, never as local wall clock', () {
      // Same instant, three spellings — one answer.
      const naive = '2026-04-01T12:34:56.789012';
      final asZ = TimelineScreen.formatMilestoneDate('${naive}Z');
      expect(TimelineScreen.formatMilestoneDate(naive), asZ);
      expect(TimelineScreen.formatMilestoneDate('$naive+00:00'), asZ);
      // A naive stamp inside a local DST gap (02:30 on 2026-03-08 does not
      // exist in Chicago) is still the UTC instant, not a normalised one.
      expect(
        TimelineScreen.formatMilestoneDate('2026-03-08T02:30:00'),
        TimelineScreen.formatMilestoneDate('2026-03-08T02:30:00Z'),
      );
      expect(
        TimelineScreen.formatMilestoneDate('2026-03-08 02:30:00'),
        TimelineScreen.formatMilestoneDate('2026-03-08T02:30:00Z'),
      );
      expect(asZ, _shown(DateTime.parse('${naive}Z').toLocal()));
    });

    test('a non-UTC offset is honoured', () {
      // 09:00 at +02:00 is 07:00Z.
      expect(
        TimelineScreen.formatMilestoneDate('2026-04-01T09:00:00+02:00'),
        TimelineScreen.formatMilestoneDate('2026-04-01T07:00:00Z'),
      );
      // Compact offset spelling too.
      expect(
        TimelineScreen.formatMilestoneDate('2026-04-01T09:00:00+0200'),
        TimelineScreen.formatMilestoneDate('2026-04-01T07:00:00Z'),
      );
    });

    test('a date-only stamp is midnight UTC', () {
      expect(
        TimelineScreen.formatMilestoneDate('2026-04-01'),
        TimelineScreen.formatMilestoneDate('2026-04-01T00:00:00Z'),
      );
    });

    test('an unparseable stamp comes back untouched', () {
      expect(TimelineScreen.formatMilestoneDate('soon'), 'soon');
      expect(TimelineScreen.formatMilestoneDate(''), '');
    });
  });

  testWidgets('renders milestones with a labelled back control', (
    tester,
  ) async {
    await tester.pumpWidget(
      const MaterialApp(
        home: TimelineScreen(
          milestones: {
            'first_conversation': '2026-04-01T12:00:00+00:00',
            'affection_level_1': '2026-04-03T08:30:00+00:00',
          },
        ),
      ),
    );
    expect(find.byTooltip('Back'), findsOneWidget);
    expect(find.text('FIRST CONTACT: Neural link established'), findsOneWidget);
    expect(find.text('TRUST EARNED: Professional Respect'), findsOneWidget);
    expect(
      find.text(
        TimelineScreen.formatMilestoneDate('2026-04-01T12:00:00+00:00'),
      ),
      findsOneWidget,
    );
  });

  testWidgets('an empty timeline keeps the back control and her line', (
    tester,
  ) async {
    await tester.pumpWidget(
      const MaterialApp(home: TimelineScreen(milestones: {})),
    );
    expect(find.byTooltip('Back'), findsOneWidget);
    expect(find.text('No milestones recorded yet.'), findsOneWidget);
  });
}
