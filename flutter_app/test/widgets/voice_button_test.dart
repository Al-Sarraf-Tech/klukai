@TestOn('browser')
library;

// Widget tests for VoiceButton: idle vs recording icon, enabled/disabled tap
// gating, and tap callbacks.
//
// Imports main.dart (GFL2Colors) -> package:web, so run under chrome.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:companion_app/widgets/voice_button.dart';

Widget _wrap(Widget child) => MaterialApp(
  home: Scaffold(body: Center(child: child)),
);

void main() {
  group('VoiceButton iconography', () {
    testWidgets('idle shows the mic icon', (tester) async {
      await tester.pumpWidget(_wrap(const VoiceButton(isRecording: false)));
      expect(find.byIcon(Icons.mic), findsOneWidget);
      expect(find.byIcon(Icons.stop), findsNothing);
    });

    testWidgets('recording shows the stop icon', (tester) async {
      await tester.pumpWidget(_wrap(const VoiceButton(isRecording: true)));
      expect(find.byIcon(Icons.stop), findsOneWidget);
      expect(find.byIcon(Icons.mic), findsNothing);
    });
  });

  group('VoiceButton interaction', () {
    testWidgets('fires onTapDown then onTapUp when enabled', (tester) async {
      var downs = 0;
      var ups = 0;
      await tester.pumpWidget(
        _wrap(
          VoiceButton(
            enabled: true,
            onTapDown: () => downs++,
            onTapUp: () => ups++,
          ),
        ),
      );

      final gesture = await tester.startGesture(
        tester.getCenter(find.byType(VoiceButton)),
      );
      await tester.pump();
      expect(downs, 1);

      await gesture.up();
      await tester.pump();
      expect(ups, 1);
    });

    testWidgets('does not fire callbacks when disabled', (tester) async {
      var downs = 0;
      var ups = 0;
      await tester.pumpWidget(
        _wrap(
          VoiceButton(
            enabled: false,
            onTapDown: () => downs++,
            onTapUp: () => ups++,
          ),
        ),
      );

      await tester.tap(find.byType(VoiceButton));
      await tester.pump();
      expect(downs, 0);
      expect(ups, 0);
    });

    testWidgets('toggling to recording does not throw (animation restart)', (
      tester,
    ) async {
      await tester.pumpWidget(_wrap(const VoiceButton(isRecording: false)));
      await tester.pumpWidget(_wrap(const VoiceButton(isRecording: true)));
      await tester.pump(const Duration(milliseconds: 100));
      expect(find.byIcon(Icons.stop), findsOneWidget);
      // Toggle back off — exercises the stop/reset branch in didUpdateWidget.
      await tester.pumpWidget(_wrap(const VoiceButton(isRecording: false)));
      expect(find.byIcon(Icons.mic), findsOneWidget);
    });
  });
}
