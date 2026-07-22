@TestOn('browser')
library;

// Widget tests for MessageBubble: user vs companion styling, read/sent status
// ticks, streaming cursor, comm tag, and image-vs-text content.
//
// message_bubble.dart uses dart:js_interop + package:web (in tap handlers) and
// imports main.dart, so this MUST run under chrome:
//   flutter test --platform chrome test/widgets/message_bubble_test.dart
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:companion_app/widgets/message_bubble.dart';
import 'package:companion_app/models/message.dart';

Widget _wrap(Widget child) => MaterialApp(
  home: Scaffold(body: SizedBox(width: 400, child: child)),
);

// 1x1 transparent PNG.
const _pngB64 =
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==';

ChatMessage _msg({
  String role = 'assistant',
  String content = 'Hello, Commander.',
  String status = 'read',
  bool isStreaming = false,
  String? imageData,
}) {
  return ChatMessage(
    id: 'm1',
    role: role,
    content: content,
    status: status,
    isStreaming: isStreaming,
    imageData: imageData,
    createdAt: DateTime(2026, 5, 1, 14, 7),
  );
}

void main() {
  group('MessageBubble — companion (assistant) messages', () {
    testWidgets('shows the KLUKAI comm tag and a voice (speaker) control', (
      tester,
    ) async {
      await tester.pumpWidget(_wrap(MessageBubble(message: _msg())));
      await tester.pump();
      expect(find.text('KLUKAI // SST-05'), findsOneWidget);
      // Idle speaker icon is shown for non-streaming companion messages.
      expect(find.byIcon(Icons.volume_up_outlined), findsOneWidget);
      // Companion messages have no read/sent ticks.
      expect(find.byIcon(Icons.done_all), findsNothing);
      expect(find.byIcon(Icons.done), findsNothing);
    });

    testWidgets('renders the message content as selectable text', (
      tester,
    ) async {
      await tester.pumpWidget(
        _wrap(MessageBubble(message: _msg(content: 'Mission ready.'))),
      );
      await tester.pump();
      expect(find.text('Mission ready.'), findsOneWidget);
      expect(find.byType(SelectableText), findsOneWidget);
    });

    testWidgets('renders the formatted HH:MM timestamp', (tester) async {
      await tester.pumpWidget(_wrap(MessageBubble(message: _msg())));
      await tester.pump();
      expect(find.text('14:07'), findsOneWidget);
    });
  });

  group('MessageBubble — user messages', () {
    testWidgets('omits the comm tag and the speaker icon', (tester) async {
      await tester.pumpWidget(
        _wrap(
          MessageBubble(
            message: _msg(role: 'user', content: 'hi'),
          ),
        ),
      );
      await tester.pump();
      expect(find.text('KLUKAI // SST-05'), findsNothing);
      expect(find.byIcon(Icons.volume_up_outlined), findsNothing);
    });

    testWidgets('read status shows double-tick (done_all)', (tester) async {
      await tester.pumpWidget(
        _wrap(
          MessageBubble(
            message: _msg(role: 'user', status: 'read'),
          ),
        ),
      );
      await tester.pump();
      expect(find.byIcon(Icons.done_all), findsOneWidget);
      expect(find.byIcon(Icons.done), findsNothing);
    });

    testWidgets('non-read status shows single-tick (done)', (tester) async {
      await tester.pumpWidget(
        _wrap(
          MessageBubble(
            message: _msg(role: 'user', status: 'sent'),
          ),
        ),
      );
      await tester.pump();
      expect(find.byIcon(Icons.done), findsOneWidget);
      expect(find.byIcon(Icons.done_all), findsNothing);
    });
  });

  group('MessageBubble — streaming', () {
    testWidgets('while streaming, hides timestamp footer (no ticks/speaker)', (
      tester,
    ) async {
      await tester.pumpWidget(
        _wrap(
          MessageBubble(message: _msg(isStreaming: true, content: 'typing...')),
        ),
      );
      await tester.pump();
      // Footer (with speaker + timestamp) is suppressed during streaming.
      expect(find.byIcon(Icons.volume_up_outlined), findsNothing);
      expect(find.text('14:07'), findsNothing);
      // Content is still visible.
      expect(find.text('typing...'), findsOneWidget);
    });
  });

  group('MessageBubble — image content', () {
    testWidgets('shows an image (not selectable text) when imageData present', (
      tester,
    ) async {
      await tester.pumpWidget(
        _wrap(
          MessageBubble(
            message: _msg(content: 'see attached', imageData: _pngB64),
          ),
        ),
      );
      await tester.pump();
      expect(find.byType(Image), findsOneWidget);
      // Text body is replaced by the image branch.
      expect(find.byType(SelectableText), findsNothing);
      // A download affordance is overlaid on the image.
      expect(find.byIcon(Icons.download), findsOneWidget);
    });
  });

  group('MessageBubble — alignment', () {
    testWidgets('user message is right-aligned, companion left-aligned', (
      tester,
    ) async {
      await tester.pumpWidget(
        _wrap(
          MessageBubble(
            message: _msg(role: 'user', content: 'u'),
          ),
        ),
      );
      await tester.pump();
      final userRow = tester.widget<Row>(find.byType(Row).first);
      expect(userRow.mainAxisAlignment, MainAxisAlignment.end);

      await tester.pumpWidget(
        _wrap(
          MessageBubble(
            message: _msg(role: 'assistant', content: 'a'),
          ),
        ),
      );
      await tester.pump();
      final botRow = tester.widget<Row>(find.byType(Row).first);
      expect(botRow.mainAxisAlignment, MainAxisAlignment.start);
    });
  });
}
