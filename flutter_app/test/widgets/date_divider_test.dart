@TestOn('browser')
library;

// Widget tests for DateDivider: relative date labelling.
//
// Imports main.dart (GFL2Colors) -> package:web, so run under chrome:
//   flutter test --platform chrome test/widgets/date_divider_test.dart
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:companion_app/widgets/date_divider.dart';

Widget _wrap(Widget child) => MaterialApp(home: Scaffold(body: child));

void main() {
  group('DateDivider relative labels', () {
    testWidgets('today renders "Today"', (tester) async {
      await tester.pumpWidget(_wrap(DateDivider(date: DateTime.now())));
      expect(find.text('Today'), findsOneWidget);
    });

    testWidgets('yesterday renders "Yesterday"', (tester) async {
      final yesterday = DateTime.now().subtract(const Duration(days: 1));
      await tester.pumpWidget(_wrap(DateDivider(date: yesterday)));
      expect(find.text('Yesterday'), findsOneWidget);
    });

    testWidgets('today is computed by calendar day, not 24h window', (
      tester,
    ) async {
      // 00:05 this morning is still "Today" even though it may be >24h logic.
      final now = DateTime.now();
      final earlyToday = DateTime(now.year, now.month, now.day, 0, 5);
      await tester.pumpWidget(_wrap(DateDivider(date: earlyToday)));
      expect(find.text('Today'), findsOneWidget);
    });

    testWidgets('older dates render as "Mon Day"', (tester) async {
      // Fixed date far in the past so it is never Today/Yesterday.
      final past = DateTime(2026, 4, 7, 14, 0);
      await tester.pumpWidget(_wrap(DateDivider(date: past)));
      expect(find.text('Apr 7'), findsOneWidget);
    });

    testWidgets('January maps to "Jan" (month index boundary low)', (
      tester,
    ) async {
      final past = DateTime(2025, 1, 15);
      await tester.pumpWidget(_wrap(DateDivider(date: past)));
      expect(find.text('Jan 15'), findsOneWidget);
    });

    testWidgets('December maps to "Dec" (month index boundary high)', (
      tester,
    ) async {
      final past = DateTime(2025, 12, 25);
      await tester.pumpWidget(_wrap(DateDivider(date: past)));
      expect(find.text('Dec 25'), findsOneWidget);
    });
  });

  group('DateDivider layout', () {
    testWidgets('centers a single pill of text', (tester) async {
      await tester.pumpWidget(_wrap(DateDivider(date: DateTime(2026, 5, 1))));
      expect(find.byType(Center), findsWidgets);
      expect(find.text('May 1'), findsOneWidget);
    });
  });
}
