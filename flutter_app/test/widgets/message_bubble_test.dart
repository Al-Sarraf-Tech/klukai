@TestOn('browser')
library;

// Widget tests for MessageBubble: user vs companion styling, read/sent status
// ticks, streaming cursor, comm tag, and image-vs-text content.
//
// message_bubble.dart uses dart:js_interop + package:web (in tap handlers) and
// imports main.dart, so this MUST run under chrome:
//   flutter test --platform chrome test/widgets/message_bubble_test.dart
import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:companion_app/widgets/message_bubble.dart';
import 'package:companion_app/models/message.dart';

import '../support/fake_browser.dart';

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

  group('MessageBubble — her voice (speaker control)', () {
    const offline = '// VOICE SYNTH OFFLINE — her words stand.';

    testWidgets('a failed TTS request drops the spinner and says so', (
      tester,
    ) async {
      // The GPU host is routinely powered off: /api/tts then 502s (or hangs —
      // the request is bounded by a timeout). Neither may leave the speaker
      // spinning, and neither may fail in silence.
      final gate = Completer<http.Response>();
      final client = MockClient((req) {
        expect(req.url.path, '/api/tts');
        return gate.future;
      });
      await tester.pumpWidget(
        _wrap(
          MessageBubble(
            message: _msg(),
            client: client,
            browser: FakeBrowserPlatform(),
          ),
        ),
      );
      await tester.pump();

      await tester.tap(find.byIcon(Icons.volume_up_outlined));
      await tester.pump();
      // Request in flight: the speaker is a spinner.
      expect(find.byType(CircularProgressIndicator), findsOneWidget);
      expect(find.byIcon(Icons.volume_up_outlined), findsNothing);

      gate.complete(http.Response('voice service unavailable', 502));
      await tester.pump();
      await tester.pump();
      expect(find.byType(CircularProgressIndicator), findsNothing);
      expect(find.byIcon(Icons.volume_up_outlined), findsOneWidget);
      expect(find.text(offline), findsOneWidget);
      // Let the snackbar run its course.
      await tester.pump(const Duration(seconds: 5));
    });

    testWidgets('a good TTS reply plays through the unlocked voice channel', (
      tester,
    ) async {
      final browser = FakeBrowserPlatform();
      final client = MockClient((req) async {
        expect(req.url.path, '/api/tts');
        expect(jsonDecode(req.body)['language'], 'en');
        return http.Response(jsonEncode({'audio': 'UklGRg=='}), 200);
      });
      await tester.pumpWidget(
        _wrap(MessageBubble(message: _msg(), client: client, browser: browser)),
      );
      await tester.pump();

      await tester.tap(find.byIcon(Icons.volume_up_outlined));
      await tester.pump();
      await tester.pump();
      expect(browser.voices, ['data:audio/wav;base64,UklGRg==']);
      expect(find.text(offline), findsNothing);
      expect(find.byType(CircularProgressIndicator), findsNothing);
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
