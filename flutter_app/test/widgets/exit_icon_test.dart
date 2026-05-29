@TestOn('browser')
// Widget tests for ExitIcon (a CustomPainter). No main.dart dependency.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:companion_app/widgets/exit_icon.dart';

Widget _wrap(Widget child) => MaterialApp(home: Scaffold(body: Center(child: child)));

void main() {
  group('ExitIcon', () {
    testWidgets('paints at the requested size', (tester) async {
      await tester.pumpWidget(_wrap(const ExitIcon(size: 32)));
      final cp = tester.widget<CustomPaint>(
        find.descendant(
          of: find.byType(ExitIcon),
          matching: find.byType(CustomPaint),
        ),
      );
      expect(cp.size, const Size(32, 32));
    });

    testWidgets('defaults to a 20px red glyph', (tester) async {
      await tester.pumpWidget(_wrap(const ExitIcon()));
      final cp = tester.widget<CustomPaint>(
        find.descendant(
          of: find.byType(ExitIcon),
          matching: find.byType(CustomPaint),
        ),
      );
      expect(cp.size, const Size(20, 20));
    });

    testWidgets('renders without throwing for a custom color', (tester) async {
      await tester.pumpWidget(
          _wrap(const ExitIcon(size: 48, color: Color(0xFF00FF00))));
      expect(find.byType(ExitIcon), findsOneWidget);
      expect(tester.takeException(), isNull);
    });
  });
}
