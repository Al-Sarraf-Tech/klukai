import 'package:flutter_test/flutter_test.dart';

import 'package:companion_app/models/her_day.dart';
import 'package:companion_app/screens/hud_day_screen.dart';
import 'package:companion_app/widgets/wardrobe_widgets.dart';

void main() {
  test('a date-only her-day is that calendar day, not the previous evening', () {
    final date = calendarDate('2026-10-04');
    expect(date, isNotNull);
    expect(date!.year, 2026);
    expect(date.month, 10);
    expect(date.day, 4);
    expect(date.isUtc, isFalse);
    expect(calendarDate('not-a-day'), isNull);
    expect(calendarDate('2026-02-31'), isNull);
  });

  test('her day rolls at 05:00, not midnight', () {
    expect(herDayId(DateTime(2026, 10, 5, 4, 59)), '2026-10-04');
    expect(herDayId(DateTime(2026, 10, 5, 5)), '2026-10-05');
  });

  test('a current mark the clock has left does not stay active', () {
    final blocks = [
      ScheduleBlock(
        start: '1200',
        end: '1700',
        current: true,
        location: 'Hangar',
      ),
    ];
    expect(scheduleStates(blocks, DateTime(2026, 10, 4, 18)), [
      BlockState.done,
    ]);
    expect(scheduleStates(blocks, DateTime(2026, 10, 4, 14, 30)), [
      BlockState.active,
    ]);
  });

  test('the worn log labels the calendar day', () {
    expect(wornDayLabel('2026-10-03'), 'OCT 03');
    expect(wornDayLabel('yesterday'), 'yesterday');
  });
}
