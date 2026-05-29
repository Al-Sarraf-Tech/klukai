@TestOn('browser')
// Widget tests for MoodIndicator: mood -> label + color mapping.
//
// Imports main.dart (GFL2Colors) transitively, which pulls package:web, so run
// under chrome:  flutter test --platform chrome test/widgets/mood_indicator_test.dart
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:companion_app/widgets/mood_indicator.dart';

Widget _wrap(Widget child) => MaterialApp(home: Scaffold(body: Center(child: child)));

/// Pulls the color off the status-text Text widget (which uses moodColor).
Color _statusTextColor(WidgetTester tester, String label) {
  final text = tester.widget<Text>(find.text(label));
  return text.style!.color!;
}

void main() {
  group('MoodIndicator status text mapping', () {
    final cases = <String, String>{
      'composed': 'COMPOSED',
      'focused': 'FOCUSED',
      'prideful': 'PRIDEFUL',
      'exasperated': 'IRRITATED', // exasperated renders as IRRITATED
      'protective': 'ON GUARD',
      'quietly_pleased': 'PLEASED',
      'tender': 'TENDER',
      'longing': 'WISTFUL', // longing renders as WISTFUL
      'battle_ready': 'BATTLE READY',
      'flustered': 'FLUSTERED',
      'affectionate': 'AFFECTIONATE',
      'vigilant': 'VIGILANT',
      'hunting': 'HUNTING',
      'content': 'CONTENT',
      'melancholic': 'MELANCHOLIC',
      'devoted': 'DEVOTED',
      'vulnerable': 'VULNERABLE',
    };

    cases.forEach((mood, label) {
      testWidgets('mood "$mood" renders status "$label"', (tester) async {
        await tester.pumpWidget(_wrap(MoodIndicator(mood: mood)));
        expect(find.text(label), findsOneWidget);
      });
    });

    testWidgets('unknown mood falls back to uppercased raw value',
        (tester) async {
      await tester.pumpWidget(_wrap(const MoodIndicator(mood: 'sleepy')));
      expect(find.text('SLEEPY'), findsOneWidget);
    });

    testWidgets('empty mood renders an empty (uppercased) status', (tester) async {
      await tester.pumpWidget(_wrap(const MoodIndicator(mood: '')));
      // ''.toUpperCase() == '' -> a Text('') still exists in the tree.
      expect(find.byType(Text), findsOneWidget);
    });
  });

  group('MoodIndicator color mapping', () {
    testWidgets('distinct moods produce distinct colors', (tester) async {
      await tester.pumpWidget(_wrap(const MoodIndicator(mood: 'focused')));
      final focused = _statusTextColor(tester, 'FOCUSED');

      await tester.pumpWidget(_wrap(const MoodIndicator(mood: 'hunting')));
      final hunting = _statusTextColor(tester, 'HUNTING');

      expect(focused, isNot(equals(hunting)));
    });

    testWidgets('flustered maps to a pink-family color (0xFFF472B6)',
        (tester) async {
      await tester.pumpWidget(_wrap(const MoodIndicator(mood: 'flustered')));
      expect(_statusTextColor(tester, 'FLUSTERED'),
          const Color(0xFFF472B6));
    });

    testWidgets('the status dot and text share the mood color', (tester) async {
      await tester.pumpWidget(_wrap(const MoodIndicator(mood: 'tender')));
      final textColor = _statusTextColor(tester, 'TENDER');
      // The 5x5 dot Container is the first Container with a circle shape.
      final dot = tester
          .widgetList<Container>(find.byType(Container))
          .firstWhere((c) {
        final d = c.decoration;
        return d is BoxDecoration && d.shape == BoxShape.circle;
      });
      final dotColor = (dot.decoration as BoxDecoration).color;
      expect(dotColor, textColor);
    });
  });

  group('MoodIndicator structure', () {
    testWidgets('uses AnimatedContainer for the smooth color transition',
        (tester) async {
      await tester.pumpWidget(_wrap(const MoodIndicator(mood: 'composed')));
      expect(find.byType(AnimatedContainer), findsWidgets);
    });
  });
}
