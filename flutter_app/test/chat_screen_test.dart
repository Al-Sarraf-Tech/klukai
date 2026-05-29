@TestOn('browser')
// Widget tests for ChatScreen — the chat orchestration layer.
//
// ChatScreen.initState() opens a WebSocket and schedules self-reconnecting
// Timers, which is why the app-level smoke test (widget_test.dart) deliberately
// does NOT pump it. Here we make it pumpable by injecting a FAKE
// WebSocketService through the (newly added) `webSocketService` constructor
// param — a backward-compatible hook that defaults to the real service in
// production. The fake never touches the network: it exposes the same
// broadcast streams plus an `emit()` helper so a test can feed it inbound
// frames exactly as a backend would.
//
// ChatScreen imports `dart:js_interop`/`package:web`, so this runs under chrome:
//   flutter test --platform chrome test/chat_screen_test.dart
//
// Note: when the pretext JS bridge is absent (as in tests) PretextService.isReady
// is false, so finalized assistant messages render through MessageBubble (a
// SelectableText), which is what we assert against.
import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:web/web.dart' as web;

import 'package:companion_app/screens/chat_screen.dart';
import 'package:companion_app/services/websocket_service.dart';
import 'package:companion_app/widgets/message_bubble.dart';
import 'package:companion_app/widgets/heartbeat_sensor.dart';

/// In-memory stand-in for [WebSocketService]. Overrides every transport method
/// to a no-op and routes inbound frames through `emit()`. The connection state
/// can be driven via `setConnected()`. Reuses the real public API surface so
/// ChatScreen cannot tell it apart from the genuine service.
class FakeWebSocketService implements WebSocketService {
  final _messageController = StreamController<Map<String, dynamic>>.broadcast();
  final _connectionController = StreamController<bool>.broadcast();

  /// Frames the screen sent outbound (so tests can assert send-side behavior).
  final List<Map<String, dynamic>> sent = [];

  bool _connected = false;

  @override
  Stream<Map<String, dynamic>> get messages => _messageController.stream;

  @override
  Stream<bool> get connectionState => _connectionController.stream;

  @override
  bool get isConnected => _connected;

  @override
  void connect(String url, {String? token}) {
    // No network. Emit "connected" asynchronously so the screen's
    // connectionState listener (subscribed right after this call in
    // _connectWS) is attached before the event lands — broadcast streams drop
    // events fired before there are listeners.
    scheduleMicrotask(() => setConnected(true));
  }

  /// Push an inbound frame to the screen, just like a real backend would.
  void emit(Map<String, dynamic> frame) => _messageController.add(frame);

  /// Drive the connection indicator.
  void setConnected(bool value) {
    _connected = value;
    _connectionController.add(value);
  }

  @override
  void send(Map<String, dynamic> data) => sent.add(data);

  @override
  void sendMessage(String content) =>
      send({'type': 'message', 'content': content, 'attachments': []});

  @override
  void sendTyping() => send({'type': 'typing'});

  @override
  void sendVoiceEnd(String audioBase64) =>
      send({'type': 'voice_end', 'audio': audioBase64});

  @override
  void disconnect() => setConnected(false);

  @override
  void dispose() {
    _messageController.close();
    _connectionController.close();
  }
}

/// Pumps ChatScreen with an injected fake and a primed auth token (so the
/// real `_connectWS` path doesn't bounce to the login page). Returns the fake.
Future<FakeWebSocketService> _pumpChat(WidgetTester tester) async {
  // ChatScreen redirects to "/" when no token is present; prime one.
  web.window.localStorage.setItem('klukai_token', 'test-token');

  final fake = FakeWebSocketService();
  await tester.pumpWidget(
    MaterialApp(
      // A non-routable origin keeps the (try/caught) history/affection fetches
      // from reaching any real backend; failures are swallowed by the screen.
      home: ChatScreen(serverUrl: 'http://localhost:0', webSocketService: fake),
    ),
  );
  // Let initState() wire up its stream listeners.
  await tester.pump();
  return fake;
}

void main() {
  tearDown(() {
    try {
      web.window.localStorage.removeItem('klukai_token');
    } catch (_) {}
  });

  group('ChatScreen — injectable transport', () {
    testWidgets('pumps with a fake WebSocket without a live backend', (
      tester,
    ) async {
      final fake = await _pumpChat(tester);
      expect(find.byType(ChatScreen), findsOneWidget);
      expect(tester.takeException(), isNull);
      fake.dispose();
    });

    testWidgets('reflects connection state from the transport stream', (
      tester,
    ) async {
      final fake = await _pumpChat(tester);
      // connect() set us connected -> "LINK ACTIVE".
      await tester.pump();
      expect(find.text('LINK ACTIVE'), findsOneWidget);

      fake.setConnected(false);
      await tester.pump();
      expect(find.text('LINK DOWN'), findsOneWidget);
      fake.dispose();
    });
  });

  group('ChatScreen — inbound token/done renders a bubble', () {
    testWidgets('token then done renders the streamed text into a bubble', (
      tester,
    ) async {
      final fake = await _pumpChat(tester);

      fake.emit({'type': 'token', 'text': 'Orders received, '});
      await tester.pump();
      fake.emit({'type': 'token', 'text': 'Commander.'});
      await tester.pump();

      // While streaming, the buffer is shown through a MessageBubble.
      expect(find.byType(MessageBubble), findsWidgets);
      expect(find.text('Orders received, Commander.'), findsOneWidget);

      fake.emit({'type': 'done', 'model': 'venice'});
      await tester.pump();

      // PretextService.isReady is false in tests, so the finalized assistant
      // message still renders via MessageBubble (SelectableText).
      expect(find.text('Orders received, Commander.'), findsOneWidget);
      expect(tester.takeException(), isNull);
      fake.dispose();
    });
  });

  group('ChatScreen — input lock lifecycle', () {
    testWidgets('first token locks input; done unlocks it', (tester) async {
      final fake = await _pumpChat(tester);

      // Locate the composer field.
      final field = find.byType(TextField);
      expect(field, findsOneWidget);

      // Initially unlocked & connected -> hint is the command prompt.
      TextField tf() => tester.widget<TextField>(field);
      expect(tf().readOnly, isFalse);

      // Inbound token must lock the composer (RECEIVING TRANSMISSION).
      fake.emit({'type': 'token', 'text': 'hold on'});
      await tester.pump();
      expect(tf().readOnly, isTrue);
      expect(find.textContaining('RECEIVING TRANSMISSION'), findsOneWidget);

      // done() must unlock it again.
      fake.emit({'type': 'done'});
      await tester.pump();
      expect(tf().readOnly, isFalse);
      fake.dispose();
    });

    testWidgets('lock auto-unlocks after the safety timeout (no done frame)', (
      tester,
    ) async {
      final fake = await _pumpChat(tester);
      TextField tf() => tester.widget<TextField>(find.byType(TextField));

      // A proactive frame locks for 1500ms then self-unlocks via a Timer —
      // a tight, deterministic window to exercise the timeout path.
      fake.emit({'type': 'proactive', 'message': 'Thinking of you.'});
      await tester.pump();
      expect(tf().readOnly, isTrue);

      // Advance past the auto-unlock timer.
      await tester.pump(const Duration(milliseconds: 1600));
      expect(tf().readOnly, isFalse);
      expect(tester.takeException(), isNull);
      fake.dispose();
    });

    testWidgets('sending a message pushes it to the transport and echoes it', (
      tester,
    ) async {
      final fake = await _pumpChat(tester);

      await tester.enterText(find.byType(TextField), 'status report');
      await tester.pump();
      // Tap the send button.
      await tester.tap(find.byIcon(Icons.send));
      await tester.pump();

      // The user's message bubble appears...
      expect(find.text('status report'), findsOneWidget);
      // ...and the transport received the outbound envelope.
      expect(fake.sent, isNotEmpty);
      expect(fake.sent.last['type'], 'message');
      expect(fake.sent.last['content'], 'status report');
      fake.dispose();
    });
  });

  group('ChatScreen — mood updates the glow / heartbeat', () {
    testWidgets('a mood frame updates the mood indicator and BPM', (
      tester,
    ) async {
      final fake = await _pumpChat(tester);

      // Default mood is "composed" -> 65 BPM, label COMPOSED.
      expect(find.text('COMPOSED'), findsOneWidget);
      expect(
        tester.widget<HeartbeatSensor>(find.byType(HeartbeatSensor)).bpm,
        65,
      );

      // Drive a new mood; glow + BPM are sourced from the single _moodVisuals
      // map, so both the indicator and the heartbeat should follow.
      fake.emit({'type': 'mood', 'mood': 'passionate'});
      await tester.pump();
      // Let the MoodIndicator's AnimatedContainer settle.
      await tester.pump(const Duration(milliseconds: 900));

      expect(find.text('PASSIONATE'), findsOneWidget);
      expect(find.text('COMPOSED'), findsNothing);
      expect(
        tester.widget<HeartbeatSensor>(find.byType(HeartbeatSensor)).bpm,
        115, // passionate -> 115 BPM (must match _moodVisuals)
      );
      fake.dispose();
    });
  });

  group('ChatScreen — keyboard scroll-nav wiring', () {
    testWidgets('a Focus wraps the message list with an onKeyEvent handler', (
      tester,
    ) async {
      final fake = await _pumpChat(tester);

      // Populate enough messages to make the list scrollable.
      for (var i = 0; i < 40; i++) {
        fake.emit({'type': 'token', 'text': 'line $i\n'});
        fake.emit({'type': 'done'});
        await tester.pump();
      }

      // The message list is wrapped by a Focus with our key handler bound —
      // proves _handleKeyScroll is actually wired (not dead code).
      final listFocus = find.ancestor(
        of: find.byType(ListView),
        matching: find.byType(Focus),
      );
      expect(listFocus, findsWidgets);
      final focusWidget = tester
          .widgetList<Focus>(listFocus)
          .firstWhere((f) => f.onKeyEvent != null);
      expect(focusWidget.onKeyEvent, isNotNull);

      // Driving Home through the handler must not throw and is consumed.
      final node = FocusNode();
      addTearDown(node.dispose);
      const homeDown = KeyDownEvent(
        physicalKey: PhysicalKeyboardKey.home,
        logicalKey: LogicalKeyboardKey.home,
        timeStamp: Duration.zero,
      );
      final result = focusWidget.onKeyEvent!(node, homeDown);
      expect(result, KeyEventResult.handled);

      // A non-nav key falls through (so typing still works elsewhere).
      const keyA = KeyDownEvent(
        physicalKey: PhysicalKeyboardKey.keyA,
        logicalKey: LogicalKeyboardKey.keyA,
        timeStamp: Duration.zero,
      );
      expect(focusWidget.onKeyEvent!(node, keyA), KeyEventResult.ignored);

      await tester.pump(const Duration(milliseconds: 500));
      expect(tester.takeException(), isNull);
      fake.dispose();
    });
  });
}
