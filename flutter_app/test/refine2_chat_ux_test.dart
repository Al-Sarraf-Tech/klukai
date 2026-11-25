// His bubble shrink-wraps only when the line is plain selectable text.
// A bare URL or a rule is a button or a block control; IntrinsicWidth throws
// on those in debug and collapses the row when asserts are stripped.
// Pure, so this runs on the VM:
//   flutter test test/refine2_chat_ux_test.dart
import 'package:companion_app/widgets/message_bubble.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('hisLineShrinkWraps', () {
    test('a plain line can shrink-wrap', () {
      expect(hisLineShrinkWraps('On station, Commander.'), isTrue);
      expect(hisLineShrinkWraps(''), isTrue);
    });

    test('a bare url is not shrink-wrapped', () {
      expect(hisLineShrinkWraps('https://example.com/briefing'), isFalse);
      expect(hisLineShrinkWraps('see http://example.com/a now'), isFalse);
      expect(hisLineShrinkWraps('WWW.Example.com/status'), isFalse);
    });

    test('a setext underline or a thematic rule is not shrink-wrapped', () {
      expect(hisLineShrinkWraps('Orders\n===='), isFalse);
      expect(hisLineShrinkWraps('Hold\n-'), isFalse);
      expect(hisLineShrinkWraps('---'), isFalse);
      expect(hisLineShrinkWraps('***'), isFalse);
    });

    test('emphasis, fences, tables and lists stay on the bounded path', () {
      expect(hisLineShrinkWraps('see *this*'), isFalse);
      expect(hisLineShrinkWraps('```\ncode\n```'), isFalse);
      expect(
        hisLineShrinkWraps('| a | b |\n| --- | --- |\n| 1 | 2 |'),
        isFalse,
      );
      expect(hisLineShrinkWraps('- Hold position'), isFalse);
    });
  });
}
