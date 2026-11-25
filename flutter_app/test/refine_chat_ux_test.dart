// Decisions the chat screen uses for socket turns, voice, and tail-follow.
// Pure, so this runs on the VM:
//   flutter test test/refine_chat_ux_test.dart
import 'package:companion_app/services/websocket_service.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('turnText', () {
    test('blank and non-string frames are not a turn', () {
      expect(turnText(null), isNull);
      expect(turnText(''), isNull);
      expect(turnText(' \n '), isNull);
      expect(turnText(0), isNull);
      expect(turnText(true), isNull);
      expect(turnText(['hi']), isNull);
      expect(turnText({'text': 'hi'}), isNull);
    });

    test('a real delta keeps its own spacing', () {
      expect(turnText('  Hi, Commander'), '  Hi, Commander');
    });
  });

  group('lineIsSpeakable', () {
    test('an error frame is not spoken', () {
      expect(
        lineIsSpeakable(
          id: 'error-2',
          content: 'Communications disrupted, Commander.',
          isStreaming: false,
        ),
        isFalse,
      );
    });

    test('her finished line is spoken; a stream and a picture are not', () {
      expect(
        lineIsSpeakable(
          id: 'streaming-4',
          content: 'On station.',
          isStreaming: false,
        ),
        isTrue,
      );
      expect(
        lineIsSpeakable(id: 'm1', content: 'On station.', isStreaming: true),
        isFalse,
      );
      expect(
        lineIsSpeakable(id: 'image-1', content: '[IMAGE]', isStreaming: false),
        isFalse,
      );
      expect(
        lineIsSpeakable(id: 'm2', content: '   ', isStreaming: false),
        isFalse,
      );
    });
  });

  group('shouldFollowTail', () {
    test('a token does not pull a 360px transcript off older lines', () {
      // Pinned is false once he has scrolled up. A delta must not jump.
      expect(shouldFollowTail(pinned: false), isFalse);
      expect(shouldFollowTail(pinned: false, dragging: true), isFalse);
      // A finger still on the list wins even if he was at the bottom.
      expect(shouldFollowTail(pinned: true, dragging: true), isFalse);
    });

    test('the tail still follows when he is already on the newest line', () {
      expect(shouldFollowTail(pinned: true), isTrue);
    });

    test('his own send still reaches the newest line', () {
      expect(shouldFollowTail(pinned: false, force: true), isTrue);
      expect(
        shouldFollowTail(pinned: false, dragging: true, force: true),
        isTrue,
      );
    });
  });
}
