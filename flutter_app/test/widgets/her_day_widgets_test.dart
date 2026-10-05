// Widget tests for the Her Day header line, the schedule strip and the sheet.
// No package:web in the import graph, so these run on the VM:
//   flutter test test/widgets/her_day_widgets_test.dart
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:companion_app/models/her_day.dart';
import 'package:companion_app/widgets/her_day_widgets.dart';

import '../support/her_day_fixtures.dart';

/// The chat header's geometry: 12px padding, a 52px portrait, a 12px gap, then
/// the name column the status line lives in.
Widget _header(HerDay day, {VoidCallback? onTap}) => MaterialApp(
      home: Scaffold(
        body: Container(
          padding: const EdgeInsets.fromLTRB(12, 10, 12, 10),
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const SizedBox(width: 52, height: 52),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    const Text('KLUKAI'),
                    HerDayStatusLine(day: day, onTap: onTap),
                  ],
                ),
              ),
            ],
          ),
        ),
      ),
    );

void _phone(WidgetTester tester) {
  tester.view.physicalSize = const Size(390, 844);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.reset);
}

void main() {
  group('HerDayStatusLine', () {
    testWidgets('shows location · activity and the outfit chip', (tester) async {
      _phone(tester);
      await tester.pumpWidget(_header(HerDay.fromJson(herDayJson())));
      expect(find.text('Hangar · tuning the suspension'), findsOneWidget);
      expect(find.text('Speed Star'), findsOneWidget);
      expect(find.byIcon(Icons.place_outlined), findsOneWidget);
    });

    testWidgets('never overflows at 390px, even with absurd strings', (tester) async {
      _phone(tester);
      final day = HerDay.fromJson(herDayJson(
        label: 'Hangar · ${'re-torquing every single bolt on the swing arm ' * 4}',
        outfit: outfitJson(name: 'Astral Luminous (Midnight Jetstream accessories, full set)'),
      ));
      await tester.pumpWidget(_header(day));
      expect(tester.takeException(), isNull); // a RenderFlex overflow would surface here

      final label = tester.widget<Text>(find.byKey(const Key('her-day-label')));
      expect(label.maxLines, 1);
      expect(label.overflow, TextOverflow.ellipsis);

      final line = tester.getRect(find.byKey(const Key('her-day-status')));
      expect(line.right, lessThanOrEqualTo(390 - 12));
      final chip = tester.getSize(find.byKey(const Key('her-day-outfit-chip')));
      expect(chip.width, lessThanOrEqualTo(HerDayStatusLine.chipMaxWidth));
      // The label still gets real room: the chip can't squeeze it out.
      final labelBox = tester.getSize(find.byKey(const Key('her-day-label')));
      expect(labelBox.width, greaterThan(150));
    });

    testWidgets('the "Off duty" label for a private block renders alone', (tester) async {
      _phone(tester);
      await tester.pumpWidget(_header(HerDay.fromJson(herDayJson(label: 'Off duty'))));
      expect(find.text('Off duty'), findsOneWidget);
    });

    testWidgets('override icons for gaming and mission', (tester) async {
      _phone(tester);
      await tester.pumpWidget(_header(HerDay.fromJson(herDayJson(override: 'gaming'))));
      expect(find.byIcon(Icons.sports_esports_outlined), findsOneWidget);
      await tester.pumpWidget(_header(HerDay.fromJson(herDayJson(override: 'mission'))));
      expect(find.byIcon(Icons.gps_fixed), findsOneWidget);
    });

    testWidgets('no chip without an outfit; taps reach the callback', (tester) async {
      _phone(tester);
      var taps = 0;
      final json = herDayJson()..remove('outfit');
      await tester.pumpWidget(_header(HerDay.fromJson(json), onTap: () => taps++));
      expect(find.byKey(const Key('her-day-outfit-chip')), findsNothing);
      await tester.tap(find.byKey(const Key('her-day-status')));
      expect(taps, 1);
    });
  });

  group('ScheduleStrip', () {
    testWidgets('times, lines, and the current block highlighted', (tester) async {
      final day = HerDay.fromJson(herDayJson());
      await tester.pumpWidget(MaterialApp(home: Scaffold(body: ScheduleStrip(blocks: day.schedule))));

      expect(find.text('0500–0800'), findsOneWidget);
      expect(find.text('Range · drills'), findsOneWidget);
      expect(find.text('Off duty'), findsOneWidget); // private block: location only

      final current = find.byKey(const Key('schedule-current'));
      expect(current, findsOneWidget);
      expect(find.descendant(of: current, matching: find.text('1200–1700')), findsOneWidget);
      expect(find.descendant(of: current, matching: find.text('NOW')), findsOneWidget);
      expect(find.text('NOW'), findsOneWidget);
      expect(find.byKey(const Key('schedule-block-0')), findsOneWidget);
      expect(find.byKey(const Key('schedule-block-2')), findsNothing); // that one is current
    });

    testWidgets('empty schedule says so', (tester) async {
      await tester.pumpWidget(const MaterialApp(home: Scaffold(body: ScheduleStrip(blocks: []))));
      expect(find.text('No schedule filed.'), findsOneWidget);
    });
  });

  group('HerDaySheet', () {
    testWidgets('status, outfit, weather, schedule, and the dossier link', (tester) async {
      var opened = 0;
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: HerDaySheet(day: HerDay.fromJson(herDayJson()), onOpenDossier: () => opened++),
        ),
      ));
      expect(find.text('TODAY // 2026-10-04'), findsOneWidget);
      expect(find.text('Speed Star — Off-duty plans.'), findsOneWidget);
      expect(find.text('14°C · clear'), findsOneWidget);
      expect(find.byKey(const Key('schedule-current')), findsOneWidget);
      await tester.tap(find.byKey(const Key('her-day-open-dossier')));
      expect(opened, 1);
    });

    testWidgets('minimal day: no date, no weather, no dossier button', (tester) async {
      final day = HerDay.fromJson({
        'outfit': outfitJson(source: 'commander', reason: ''),
        'status': {'location': 'Off duty', 'activity': ''},
        'weather': null,
      });
      await tester.pumpWidget(MaterialApp(home: Scaffold(body: HerDaySheet(day: day))));
      expect(find.text('TODAY'), findsOneWidget);
      expect(find.text('Speed Star — Your request.'), findsOneWidget);
      expect(find.byKey(const Key('her-day-open-dossier')), findsNothing);
    });
  });
}
