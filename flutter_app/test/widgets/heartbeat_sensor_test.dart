@TestOn('browser')
// Widget tests for HeartbeatSensor: BPM readout, color, icon, ECG painter.
// No main.dart dependency (runs on VM or chrome).
//
// REGRESSION COVERED: HeartbeatSensor previously recreated its AnimationController
// in didUpdateWidget() on every `bpm` change, allocating a second ticker from a
// SingleTickerProviderStateMixin (which throws) and leaking the old controller.
// It now reuses the single controller and just retargets its duration. The
// "updates BPM on rebuild" test below guards that fix.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:companion_app/widgets/heartbeat_sensor.dart';

Widget _wrap(Widget child) => MaterialApp(home: Scaffold(body: Center(child: child)));

void main() {
  group('HeartbeatSensor readout', () {
    testWidgets('renders the BPM number and the BPM label', (tester) async {
      await tester.pumpWidget(
          _wrap(const HeartbeatSensor(bpm: 72, color: Colors.red)));
      expect(find.text('72'), findsOneWidget);
      expect(find.text('BPM'), findsOneWidget);
      // Tear down the infinite-repeat ticker cleanly.
      await tester.pumpWidget(const SizedBox.shrink());
    });

    testWidgets('renders the heart icon and an ECG CustomPaint', (tester) async {
      await tester.pumpWidget(
          _wrap(const HeartbeatSensor(bpm: 90, color: Colors.pink)));
      expect(find.byIcon(Icons.favorite), findsOneWidget);
      expect(find.byType(CustomPaint), findsWidgets);
      await tester.pumpWidget(const SizedBox.shrink());
    });

    testWidgets('applies the provided color to the BPM number', (tester) async {
      const c = Color(0xFF12AB34);
      await tester.pumpWidget(_wrap(const HeartbeatSensor(bpm: 60, color: c)));
      final t = tester.widget<Text>(find.text('60'));
      // Text uses color.withValues(alpha: 0.6); compare RGB channels only.
      final rendered = t.style!.color!;
      expect(rendered.toARGB32() & 0x00FFFFFF, c.toARGB32() & 0x00FFFFFF);
      await tester.pumpWidget(const SizedBox.shrink());
    });

    testWidgets('high BPM (spike) renders cleanly and pulses', (tester) async {
      await tester.pumpWidget(
          _wrap(const HeartbeatSensor(bpm: 175, color: Colors.red)));
      await tester.pump(const Duration(milliseconds: 120));
      expect(find.text('175'), findsOneWidget);
      await tester.pumpWidget(const SizedBox.shrink());
    });

    testWidgets('low BPM renders cleanly', (tester) async {
      await tester.pumpWidget(
          _wrap(const HeartbeatSensor(bpm: 48, color: Colors.cyan)));
      expect(find.text('48'), findsOneWidget);
      await tester.pumpWidget(const SizedBox.shrink());
    });

    testWidgets('updates BPM on rebuild without throwing (ticker reuse)',
        (tester) async {
      // Regression: a BPM change must retarget the existing controller, not
      // allocate a second ticker from the SingleTickerProviderStateMixin.
      await tester.pumpWidget(
          _wrap(const HeartbeatSensor(bpm: 72, color: Colors.red)));
      expect(find.text('72'), findsOneWidget);
      // Rebuild the same widget position with a new BPM → didUpdateWidget.
      await tester.pumpWidget(
          _wrap(const HeartbeatSensor(bpm: 130, color: Colors.red)));
      await tester.pump(const Duration(milliseconds: 50));
      expect(tester.takeException(), isNull);
      expect(find.text('130'), findsOneWidget);
      expect(find.text('72'), findsNothing);
      await tester.pumpWidget(const SizedBox.shrink());
    });
  });
}
