@TestOn('browser')
library;

// Widget tests for AffectionGauge: level name, score readout, fill color,
// and the animated delta (+N / -N) popup on prop change.
//
// Imports main.dart (GFL2Colors) -> package:web, so run under chrome:
//   flutter test --platform chrome test/widgets/affection_gauge_test.dart
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:companion_app/widgets/affection_gauge.dart';
import 'package:companion_app/main.dart';

Widget _wrap(Widget child) => MaterialApp(
  home: Scaffold(body: SizedBox(width: 320, child: child)),
);

Color _levelNameColor(WidgetTester tester, String upperName) {
  final t = tester.widget<Text>(find.text(upperName));
  return t.style!.color!;
}

void main() {
  group('AffectionGauge readouts', () {
    testWidgets('renders the TRUST label and uppercased level name', (
      tester,
    ) async {
      await tester.pumpWidget(
        _wrap(
          const AffectionGauge(score: 250, level: 4, levelName: 'Trusted Ally'),
        ),
      );
      expect(find.text('TRUST'), findsOneWidget);
      expect(find.text('TRUSTED ALLY'), findsOneWidget);
    });

    testWidgets('renders the score as N/1000', (tester) async {
      await tester.pumpWidget(
        _wrap(
          const AffectionGauge(
            score: 540,
            level: 6,
            levelName: 'Deep Devotion',
          ),
        ),
      );
      expect(find.text('540/1000'), findsOneWidget);
    });

    testWidgets('zero score still shows 0/1000', (tester) async {
      await tester.pumpWidget(
        _wrap(
          const AffectionGauge(
            score: 0,
            level: 0,
            levelName: 'Cold Assessment',
          ),
        ),
      );
      expect(find.text('0/1000'), findsOneWidget);
    });
  });

  group('AffectionGauge level color mapping', () {
    testWidgets('level 0 (cold) uses the dim/grey color', (tester) async {
      await tester.pumpWidget(
        _wrap(const AffectionGauge(score: 10, level: 0, levelName: 'Cold')),
      );
      expect(_levelNameColor(tester, 'COLD'), GFL2Colors.textDim);
    });

    testWidgets('level 4 (trusted) uses the cyan primary color', (
      tester,
    ) async {
      await tester.pumpWidget(
        _wrap(const AffectionGauge(score: 260, level: 4, levelName: 'Trusted')),
      );
      expect(_levelNameColor(tester, 'TRUSTED'), GFL2Colors.primary);
    });

    testWidgets('level 6 (devotion) uses the orange accent color', (
      tester,
    ) async {
      await tester.pumpWidget(
        _wrap(
          const AffectionGauge(score: 540, level: 6, levelName: 'Devotion'),
        ),
      );
      expect(_levelNameColor(tester, 'DEVOTION'), GFL2Colors.accent);
    });

    testWidgets('level 8 (bonded) uses the pink affinity color', (
      tester,
    ) async {
      await tester.pumpWidget(
        _wrap(const AffectionGauge(score: 850, level: 8, levelName: 'Bonded')),
      );
      expect(_levelNameColor(tester, 'BONDED'), GFL2Colors.affinity);
    });

    testWidgets('out-of-range level falls back to dim color', (tester) async {
      await tester.pumpWidget(
        _wrap(const AffectionGauge(score: 999, level: 42, levelName: 'Glitch')),
      );
      expect(_levelNameColor(tester, 'GLITCH'), GFL2Colors.textDim);
    });
  });

  group('AffectionGauge fill fraction', () {
    testWidgets('fill widthFactor equals score/1000 (clamped)', (tester) async {
      await tester.pumpWidget(
        _wrap(const AffectionGauge(score: 250, level: 4, levelName: 'Trusted')),
      );
      final fsb = tester.widget<FractionallySizedBox>(
        find.byType(FractionallySizedBox),
      );
      expect(fsb.widthFactor, closeTo(0.25, 1e-9));
    });

    testWidgets('score above 1000 clamps fill to 1.0', (tester) async {
      await tester.pumpWidget(
        _wrap(const AffectionGauge(score: 5000, level: 9, levelName: 'Oath')),
      );
      final fsb = tester.widget<FractionallySizedBox>(
        find.byType(FractionallySizedBox),
      );
      expect(fsb.widthFactor, 1.0);
    });

    testWidgets('zero score yields zero fill', (tester) async {
      await tester.pumpWidget(
        _wrap(const AffectionGauge(score: 0, level: 0, levelName: 'Cold')),
      );
      final fsb = tester.widget<FractionallySizedBox>(
        find.byType(FractionallySizedBox),
      );
      expect(fsb.widthFactor, 0.0);
    });
  });

  group('AffectionGauge delta popup', () {
    testWidgets('shows "+N" when lastDelta increases between builds', (
      tester,
    ) async {
      await tester.pumpWidget(
        _wrap(
          const AffectionGauge(
            score: 100,
            level: 3,
            levelName: 'Interest',
            lastDelta: 0,
          ),
        ),
      );
      expect(find.text('+15'), findsNothing);

      // Rebuild with a new positive delta -> didUpdateWidget fires the popup.
      await tester.pumpWidget(
        _wrap(
          const AffectionGauge(
            score: 115,
            level: 3,
            levelName: 'Interest',
            lastDelta: 15,
          ),
        ),
      );
      await tester.pump(); // start the animation frame
      expect(find.text('+15'), findsOneWidget);

      // Let the 1500ms animation finish; popup should be cleared.
      await tester.pump(const Duration(milliseconds: 1600));
      expect(find.text('+15'), findsNothing);
    });

    testWidgets('shows "-N" (no plus sign) for a negative delta', (
      tester,
    ) async {
      await tester.pumpWidget(
        _wrap(
          const AffectionGauge(
            score: 100,
            level: 3,
            levelName: 'Interest',
            lastDelta: 0,
          ),
        ),
      );
      await tester.pumpWidget(
        _wrap(
          const AffectionGauge(
            score: 88,
            level: 3,
            levelName: 'Interest',
            lastDelta: -12,
          ),
        ),
      );
      await tester.pump();
      expect(find.text('-12'), findsOneWidget);
      expect(find.text('+-12'), findsNothing);
      await tester.pump(const Duration(milliseconds: 1600));
    });

    testWidgets('a zero delta does not trigger the popup', (tester) async {
      await tester.pumpWidget(
        _wrap(
          const AffectionGauge(
            score: 100,
            level: 3,
            levelName: 'Interest',
            lastDelta: 5,
          ),
        ),
      );
      // Change other fields but keep delta semantics neutral (0).
      await tester.pumpWidget(
        _wrap(
          const AffectionGauge(
            score: 100,
            level: 3,
            levelName: 'Interest',
            lastDelta: 0,
          ),
        ),
      );
      await tester.pump();
      expect(find.textContaining('+'), findsNothing);
    });
  });
}
