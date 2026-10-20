@TestOn('browser')
library;

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
import 'dart:convert';
import 'dart:js_interop';
import 'dart:js_interop_unsafe';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:web/web.dart' as web;

import 'package:companion_app/models/her_day.dart';
import 'package:companion_app/screens/chat_screen.dart';
import 'package:companion_app/services/resume_policy.dart';
import 'package:companion_app/services/speech_capture.dart';
import 'package:companion_app/services/voice_settings_service.dart';
import 'package:companion_app/services/wardrobe_service.dart';
import 'package:companion_app/services/websocket_service.dart';
import 'package:companion_app/widgets/chat_composer_bar.dart';
import 'package:companion_app/widgets/chat_header.dart';
import 'package:companion_app/widgets/companion_stage_view.dart';
import 'package:companion_app/theme/hud.dart';
import 'package:companion_app/widgets/voice_button.dart';
import 'package:companion_app/widgets/canvas_message_bubble.dart';
import 'package:companion_app/widgets/message_bubble.dart';
import 'package:companion_app/widgets/tool_status_indicator.dart';

import 'support/fake_browser.dart';
import 'support/her_day_fixtures.dart';

// 1x1 transparent PNG — what an `image` frame carries, in miniature.
const _pngB64 =
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==';

/// Pretend the pretext JS bridge is loaded — `isReady` only; nothing in the
/// tests that use it may draw through it — so routing that depends on it can
/// be exercised. Pair with [_removePretextStub] in a tearDown.
void _installPretextStub() {
  final bridge = JSObject();
  bridge['isReady'] = (() => true.toJS).toJS;
  globalContext['pretextBridge'] = bridge;
}

void _removePretextStub() {
  globalContext.delete('pretextBridge'.toJS);
}

/// Records what the screen asks on resume and never redials, so a test can
/// read the duration the screen measured.
class _RecordingPolicy extends ResumePolicy {
  final List<Duration> awayFor = [];

  @override
  bool shouldReconnect({
    required bool connected,
    required Duration sinceLastFrame,
  }) {
    awayFor.add(sinceLastFrame);
    return false;
  }
}

/// In-memory stand-in for [WebSocketService]. Overrides every transport method
/// to a no-op and routes inbound frames through `emit()`. The connection state
/// can be driven via `setConnected()`. Reuses the real public API surface so
/// ChatScreen cannot tell it apart from the genuine service.
class FakeWebSocketService implements WebSocketService {
  final _messageController = StreamController<Map<String, dynamic>>.broadcast();
  final _connectionController = StreamController<bool>.broadcast();
  final _authFailureController = StreamController<void>.broadcast();

  /// Frames the screen sent outbound (so tests can assert send-side behavior).
  final List<Map<String, dynamic>> sent = [];

  /// When false, send() refuses frames (simulates a dead channel mid-reconnect).
  bool sendShouldSucceed = true;

  bool _connected = false;

  @override
  Stream<Map<String, dynamic>> get messages => _messageController.stream;

  @override
  Stream<bool> get connectionState => _connectionController.stream;

  @override
  Stream<void> get authFailure => _authFailureController.stream;

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
  bool send(Map<String, dynamic> data) {
    if (!sendShouldSucceed) return false;
    sent.add(data);
    return true;
  }

  @override
  bool sendMessage(String content) =>
      send({'type': 'message', 'content': content, 'attachments': []});

  @override
  bool sendTyping() => send({'type': 'typing'});

  @override
  bool sendVoiceEnd(String audioBase64) =>
      send({'type': 'voice_end', 'audio': audioBase64});

  @override
  void disconnect() => setConnected(false);

  /// Counts redials requested by the screen (page resume).
  int reconnectNowCalls = 0;

  @override
  void reconnectNow() => reconnectNowCalls++;

  @override
  List<Duration> get reconnectDelays => const [];

  @override
  void dispose() {
    _messageController.close();
    _connectionController.close();
    _authFailureController.close();
  }
}

/// Pumps ChatScreen with an injected fake and a primed auth token (so the
/// real `_connectWS` path doesn't bounce to the login page). Returns the fake.
/// /api/voice/settings stand-in: answers [language] and records PUTs.
class _VoiceSettingsBackend {
  _VoiceSettingsBackend({this.language = 'en', this.putStatus = 200});
  String language;
  int putStatus;
  final List<String> saved = [];

  VoiceSettingsService service() => VoiceSettingsService(
    serverUrl: 'http://localhost:0',
    // No expect() in here: the client runs inside the tester's pumps.
    client: MockClient((request) async {
      if (request.url.path != '/api/voice/settings' ||
          !(request.headers['Authorization'] ?? '').startsWith('Bearer ')) {
        return http.Response('unexpected request', 400);
      }
      if (request.method == 'PUT') {
        final next = jsonDecode(request.body)['language'] as String;
        saved.add(next);
        if (putStatus != 200) return http.Response('{}', putStatus);
        language = next;
      }
      return http.Response(jsonEncode({'language': language}), 200);
    }),
  );
}

/// Opens the chat menu. (No pumpAndSettle: the heart trace never settles.)
Future<void> _openMenu(WidgetTester tester) async {
  await tester.tap(find.byKey(const Key('chat-menu')).first);
  await tester.pump();
  await tester.pump(const Duration(milliseconds: 400));
}

/// Records what the screen asked the embedded 3D stage to say.
class _FakeStageLink implements CompanionStageLink {
  final spoken = <(String, int)>[];
  @override
  bool speak(String text, {required int id}) {
    spoken.add((text, id));
    return true;
  }
}

/// A 3D stage that loads nothing: hands the screen [link] and keeps the
/// event handler so a test can play the stage's side of the conversation.
/// Like the real iframe, it reports its link once when it is created and
/// null when it is gone, and counts both: a stage built twice is a reload.
class _FakeStage {
  final link = _FakeStageLink();
  StageEventHandler? onEvent;
  String? src;
  bool? visible;
  int created = 0;
  int disposed = 0;

  /// Taps that reached the stage itself (not the Flutter content over it).
  int taps = 0;

  Widget build(
    BuildContext context, {
    required String src,
    required bool visible,
    required ValueChanged<CompanionStageLink?> onLink,
    required StageEventHandler onEvent,
  }) {
    this.src = src;
    this.visible = visible;
    this.onEvent = onEvent;
    return _FakeStageView(stage: this, onLink: onLink);
  }
}

class _FakeStageView extends StatefulWidget {
  const _FakeStageView({required this.stage, required this.onLink})
    : super(key: const Key('fake-stage'));
  final _FakeStage stage;
  final ValueChanged<CompanionStageLink?> onLink;

  @override
  State<_FakeStageView> createState() => _FakeStageViewState();
}

class _FakeStageViewState extends State<_FakeStageView> {
  @override
  void initState() {
    super.initState();
    widget.stage.created++;
    widget.onLink(widget.stage.link);
  }

  @override
  void dispose() {
    widget.stage.disposed++;
    widget.onLink(null);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => GestureDetector(
    behavior: HitTestBehavior.opaque,
    onTap: () => widget.stage.taps++,
    child: const ColoredBox(color: Colors.black),
  );
}

/// /api/messages with one line of hers, /api/affection with a score: the
/// transcript loads, which is what starts the stage's background warm-up.
http.Client _historyBackend() => MockClient((request) async {
  if (request.url.path == '/api/messages') {
    return http.Response(
      jsonEncode({
        'messages': [
          {
            'id': 'm1',
            'role': 'assistant',
            'content': 'Report, Commander.',
            'created_at': '2026-10-19T08:00:00Z',
          },
        ],
      }),
      200,
    );
  }
  return http.Response(jsonEncode({'score': 10, 'level': 1}), 200);
});

Future<FakeWebSocketService> _pumpChat(
  WidgetTester tester, {
  SpeechBridge? speechBridge,
  http.Client? httpClient,
  VoiceSettingsService? voiceSettings,
  _FakeStage? stage,
  FakeBrowserPlatform? browser,
  bool voiceInput = false,
  http.Client? historyClient,
}) async {
  // ChatScreen redirects to "/" when no token is present; prime one.
  web.window.localStorage.setItem('klukai_token', 'test-token');

  final fake = FakeWebSocketService();
  await tester.pumpWidget(
    MaterialApp(
      // A non-routable origin keeps the (try/caught) history/affection fetches
      // from reaching any real backend; failures are swallowed by the screen.
      home: ChatScreen(
        serverUrl: 'http://localhost:0',
        webSocketService: fake,
        speechBridge: speechBridge,
        httpClient: httpClient,
        voiceSettings: voiceSettings ?? _VoiceSettingsBackend().service(),
        stageBuilder: (stage ?? _FakeStage()).build,
        browser: browser,
        voiceInputEnabled: voiceInput,
        historyClient: historyClient,
      ),
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
      web.window.localStorage.removeItem(kOpenIn3dPrefKey);
      web.window.localStorage.removeItem('flutter.$kOpenIn3dPrefKey');
      web.window.localStorage.removeItem(kStageUsedKey);
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
      // connect() set us connected -> "ONLINE".
      await tester.pump();
      expect(find.text('ONLINE'), findsOneWidget);

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
      await tester.tap(find.byKey(const Key('composer-send')));
      await tester.pump();

      // The user's message bubble appears...
      expect(find.text('status report'), findsOneWidget);
      // ...and the transport received the outbound envelope.
      expect(fake.sent, isNotEmpty);
      expect(fake.sent.last['type'], 'message');
      expect(fake.sent.last['content'], 'status report');
      fake.dispose();
    });

    testWidgets(
      'a failed send is NOT echoed as sent — error surfaces, draft kept',
      (tester) async {
        final fake = await _pumpChat(tester);
        // The channel dies under us, but the connectionState event hasn't
        // landed yet (the silent no-op window the old void send() hid).
        fake.sendShouldSucceed = false;

        await tester.enterText(find.byType(TextField), 'status report');
        await tester.pump();
        await tester.tap(find.byKey(const Key('composer-send')));
        await tester.pump();

        // No fake echo in the transcript...
        expect(find.byType(MessageBubble), findsNothing);
        // ...nothing reached the transport...
        expect(fake.sent, isEmpty);
        // ...the failure is surfaced...
        expect(find.text('TRANSMISSION FAILED // LINK DOWN'), findsOneWidget);
        // ...and the draft stays in the composer for retry.
        expect(
          tester.widget<TextField>(find.byType(TextField)).controller!.text,
          'status report',
        );
        fake.dispose();
      },
    );

    testWidgets('a transcript keeps the typed draft', (tester) async {
      final fake = await _pumpChat(
        tester,
        voiceInput: true,
        speechBridge: SpeechBridge(
          start: () async => true,
          stop: () async => 'clip',
        ),
        httpClient: MockClient((request) async {
          expect(request.url.path, '/api/stt');
          return http.Response(jsonEncode({'text': 'spoken line'}), 200);
        }),
      );
      await tester.enterText(find.byType(TextField), 'keep this draft');
      await tester.pump();
      final gesture = await tester.startGesture(
        tester.getCenter(find.byType(VoiceButton)),
      );
      await tester.pump();
      await gesture.up();
      await tester.pump();

      expect(
        tester.widget<TextField>(find.byType(TextField)).controller!.text,
        'keep this draft',
      );
      expect(fake.sent.last['content'], 'spoken line');
      fake.dispose();
    });

    testWidgets('text still sends while voice is down', (tester) async {
      final fake = await _pumpChat(
        tester,
        voiceInput: true,
        speechBridge: SpeechBridge(
          start: () async => false,
          stop: () async => null,
        ),
      );
      await tester.tap(find.byType(VoiceButton));
      await tester.pump();
      expect(find.textContaining('VOICE LINK GARBLED'), findsOneWidget);

      await tester.enterText(
        find.byType(TextField),
        'typed while voice is down',
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('composer-send')));
      await tester.pump();

      expect(fake.sent.last['content'], 'typed while voice is down');
      expect(
        tester.widget<TextField>(find.byType(TextField)).controller!.text,
        isEmpty,
      );
      fake.dispose();
    });

    testWidgets('a cancelled transcript does not replace the draft', (
      tester,
    ) async {
      final gate = Completer<http.Response>();
      final fake = await _pumpChat(
        tester,
        voiceInput: true,
        speechBridge: SpeechBridge(
          start: () async => true,
          stop: () async => 'clip',
        ),
        httpClient: MockClient((_) => gate.future),
      );
      await tester.enterText(find.byType(TextField), 'keep this draft');
      await tester.pump();
      final gesture = await tester.startGesture(
        tester.getCenter(find.byType(VoiceButton)),
      );
      await tester.pump();
      await gesture.up();
      await tester.pump();

      await tester.sendKeyDownEvent(LogicalKeyboardKey.escape);
      await tester.pump();
      gate.complete(http.Response(jsonEncode({'text': 'spoken line'}), 200));
      await tester.pump();

      expect(
        tester.widget<TextField>(find.byType(TextField)).controller!.text,
        'keep this draft',
      );
      expect(
        fake.sent.where((frame) => frame['content'] == 'spoken line'),
        isEmpty,
      );
      fake.dispose();
    });
  });

  group('ChatScreen — reconnect', () {
    testWidgets('a false→true link transition is handled without errors', (
      tester,
    ) async {
      final fake = await _pumpChat(tester);
      await tester.pump();
      expect(find.text('ONLINE'), findsOneWidget);

      // Drop the link, then restore it. The screen refetches history on the
      // false→true edge (merged by id, so this can never duplicate messages);
      // with no backend in tests the fetch fails into the retry snackbar path.
      fake.setConnected(false);
      await tester.pump();
      expect(find.text('LINK DOWN'), findsOneWidget);

      fake.setConnected(true);
      await tester.pump();
      expect(find.text('ONLINE'), findsOneWidget);
      // Allow the (failing) refetch future to settle.
      await tester.pump(const Duration(seconds: 1));
      expect(tester.takeException(), isNull);
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
      expect(tester.widget<HudEcg>(find.byType(HudEcg)).bpm, 65);
      expect(find.text('65 BPM'), findsOneWidget);

      // Drive a new mood; glow + BPM are sourced from the single _moodVisuals
      // map, so both the indicator and the heartbeat should follow.
      fake.emit({'type': 'mood', 'mood': 'passionate'});
      await tester.pump();
      // Let the MoodIndicator's AnimatedContainer settle.
      await tester.pump(const Duration(milliseconds: 900));

      expect(find.text('PASSIONATE'), findsOneWidget);
      expect(find.text('COMPOSED'), findsNothing);
      expect(
        tester.widget<HudEcg>(find.byType(HudEcg)).bpm,
        115, // passionate -> 115 BPM (must match _moodVisuals)
      );
      expect(find.text('115 BPM'), findsOneWidget);
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

  group('ChatScreen — Her Day status line', () {
    Future<(FakeWebSocketService, FakeWardrobeService)> pumpWithDay(
      WidgetTester tester,
      FakeWardrobeService wardrobe,
    ) async {
      web.window.localStorage.setItem('klukai_token', 'test-token');
      tester.view.physicalSize = const Size(390, 844);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.reset);
      final fake = FakeWebSocketService();
      await tester.pumpWidget(
        MaterialApp(
          home: ChatScreen(
            serverUrl: 'http://localhost:0',
            webSocketService: fake,
            wardrobeService: wardrobe,
            voiceSettings: _VoiceSettingsBackend().service(),
          ),
        ),
      );
      await tester.pump();
      await tester.pump();
      return (fake, wardrobe);
    }

    testWidgets('shows where she is and what she is wearing, at 390px', (
      tester,
    ) async {
      final (fake, wardrobe) = await pumpWithDay(tester, FakeWardrobeService());
      expect(find.text('Hangar \u00B7 tuning the suspension'), findsOneWidget);
      expect(find.byKey(const Key('chat-chip-wearing')), findsOneWidget);
      expect(find.byKey(const Key('chat-chip-now')), findsOneWidget);
      expect(tester.takeException(), isNull);
      expect(wardrobe.herDayCalls, greaterThanOrEqualTo(1));
      fake.dispose();
    });

    testWidgets('the newest line stays in view when her day loads late', (
      tester,
    ) async {
      final wardrobe = _LateDayWardrobe();
      final (fake, _) = await pumpWithDay(tester, wardrobe);
      expect(find.byKey(const Key('chat-chip-now')), findsNothing);

      for (var i = 0; i < 40; i++) {
        fake.emit({'type': 'token', 'text': 'line $i\n'});
        fake.emit({'type': 'done'});
        await tester.pump();
      }
      await tester.pump(const Duration(milliseconds: 600));
      // The transcript is the list the keyboard scroll-nav Focus wraps.
      ScrollPosition position() => tester
          .widget<ListView>(
            find.descendant(
              of: find.byType(Focus),
              matching: find.byType(ListView),
            ),
          )
          .controller!
          .position;
      expect(position().maxScrollExtent, greaterThan(0));
      expect(position().pixels, closeTo(position().maxScrollExtent, 1));

      // Her day arrives: the header grows by the WEARING / NOW chips and the
      // transcript's viewport shrinks. The newest line must stay in view.
      wardrobe.release();
      await tester.pump();
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));
      expect(find.byKey(const Key('chat-chip-now')), findsOneWidget);
      expect(position().pixels, closeTo(position().maxScrollExtent, 1));
      expect(tester.takeException(), isNull);
      fake.dispose();
    });

    testWidgets('an outfit frame swaps the chip and refetches', (tester) async {
      final wardrobe = FakeWardrobeService(
        herDays: [
          HerDay.fromJson(herDayJson()),
          HerDay.fromJson(herDayJson()),
          HerDay.fromJson(
            herDayJson(
              outfit: outfitJson(
                id: 'immaculate_service',
                name: 'Immaculate Service',
                source: 'commander',
              ),
            ),
          ),
        ],
      );
      final (fake, _) = await pumpWithDay(tester, wardrobe);
      final before = wardrobe.herDayCalls;
      fake.emit({
        'type': 'outfit',
        'outfit': outfitJson(
          id: 'immaculate_service',
          name: 'Immaculate Service',
          source: 'commander',
        ),
      });
      await tester.pump();
      expect(find.text('Immaculate Service'), findsOneWidget);
      expect(wardrobe.herDayCalls, before + 1);
      fake.dispose();
    });

    testWidgets('her-day failing hides the line and chat carries on', (
      tester,
    ) async {
      final (fake, _) = await pumpWithDay(
        tester,
        FakeWardrobeService(herDays: [WardrobeServiceException(503)]),
      );
      expect(find.byKey(const Key('chat-chip-now')), findsNothing);
      expect(find.byKey(const Key('chat-chip-wearing')), findsNothing);
      expect(find.text('KLUKAI'), findsOneWidget);
      expect(tester.takeException(), isNull);
      fake.dispose();
    });
  });

  group('ChatScreen — browser seam (companion, resume, voice)', () {
    Future<(FakeWebSocketService, FakeBrowserPlatform)> pumpWithBrowser(
      WidgetTester tester,
      FakeBrowserPlatform browser,
    ) async {
      web.window.localStorage.setItem('klukai_token', 'test-token');
      final fake = FakeWebSocketService();
      await tester.pumpWidget(
        MaterialApp(
          home: ChatScreen(
            serverUrl: 'http://localhost:0',
            webSocketService: fake,
            wardrobeService: FakeWardrobeService(),
            browser: browser,
            voiceSettings: _VoiceSettingsBackend().service(),
          ),
        ),
      );
      await tester.pump();
      await tester.pump();
      return (fake, browser);
    }

    testWidgets('Companion opens companion.html in a new window on desktop', (
      tester,
    ) async {
      final (fake, browser) = await pumpWithBrowser(
        tester,
        FakeBrowserPlatform(),
      );
      await _openMenu(tester);
      await tester.tap(find.byKey(const Key('chat-menu-companion')));
      await tester.pump();
      expect(browser.opened, [('companion.html', true)]);
      fake.dispose();
    });

    testWidgets('Companion navigates in place inside the iOS home-screen app', (
      tester,
    ) async {
      final (fake, browser) = await pumpWithBrowser(
        tester,
        FakeBrowserPlatform(isIos: true, isStandalone: true),
      );
      await _openMenu(tester);
      await tester.tap(find.byKey(const Key('chat-menu-companion')));
      await tester.pump();
      expect(browser.opened, [('companion.html', false)]);
      fake.dispose();
    });

    testWidgets('resume with the link down redials immediately', (
      tester,
    ) async {
      final (fake, browser) = await pumpWithBrowser(
        tester,
        FakeBrowserPlatform(),
      );
      fake.setConnected(false);
      await tester.pump();
      browser.resume.add(null);
      await tester.pump();
      expect(fake.reconnectNowCalls, 1);
      fake.dispose();
    });

    testWidgets('a voice_audio frame never plays by itself', (tester) async {
      final (fake, browser) = await pumpWithBrowser(
        tester,
        FakeBrowserPlatform(),
      );
      fake.emit({'type': 'voice_audio', 'audio': 'UklGRg=='});
      await tester.pump();
      expect(browser.voices, isEmpty);
      fake.dispose();
    });
  });

  group('ChatScreen — resume measures time away, not silence', () {
    // The web engine reports visibility through the lifecycle channel: hidden
    // when the tab/app leaves the screen, resumed when it is back.
    Future<void> setLifecycle(WidgetTester tester, AppLifecycleState state) =>
        tester.binding.defaultBinaryMessenger.handlePlatformMessage(
          SystemChannels.lifecycle.name,
          const StringCodec().encodeMessage(state.toString()),
          (_) {},
        );

    Future<(FakeWebSocketService, FakeBrowserPlatform)> pumpWithPolicy(
      WidgetTester tester,
      ResumePolicy policy,
    ) async {
      web.window.localStorage.setItem('klukai_token', 'test-token');
      final fake = FakeWebSocketService();
      final browser = FakeBrowserPlatform();
      await tester.pumpWidget(
        MaterialApp(
          home: ChatScreen(
            serverUrl: 'http://localhost:0',
            webSocketService: fake,
            wardrobeService: FakeWardrobeService(),
            browser: browser,
            resumePolicy: policy,
            voiceSettings: _VoiceSettingsBackend().service(),
          ),
        ),
      );
      await tester.pump();
      await tester.pump();
      return (fake, browser);
    }

    testWidgets(
      'a blink of a hide after a quiet stretch reports the blink, not the quiet',
      (tester) async {
        final policy = _RecordingPolicy();
        final (fake, browser) = await pumpWithPolicy(tester, policy);

        // Chat goes quiet for a while (real time: this is wall-clock)...
        await tester.runAsync(
          () => Future<void>.delayed(const Duration(milliseconds: 500)),
        );
        // ...then a one-blink tab switch and back.
        await setLifecycle(tester, AppLifecycleState.hidden);
        await setLifecycle(tester, AppLifecycleState.resumed);
        browser.resume.add(null);
        await tester.pump();

        expect(policy.awayFor, hasLength(1));
        // Measured from the hide, not from the last frame half a second ago.
        expect(
          policy.awayFor.single,
          lessThan(const Duration(milliseconds: 500)),
        );
        expect(fake.reconnectNowCalls, 0);
        fake.dispose();
      },
    );

    testWidgets('a resume that follows no hide reports no time away', (
      tester,
    ) async {
      final policy = _RecordingPolicy();
      final (fake, browser) = await pumpWithPolicy(tester, policy);
      await tester.runAsync(
        () => Future<void>.delayed(const Duration(milliseconds: 50)),
      );

      browser.resume.add(null); // e.g. the network coming back
      await tester.pump();

      expect(policy.awayFor, [Duration.zero]);
      expect(fake.reconnectNowCalls, 0);
      fake.dispose();
    });

    testWidgets('a long hide redials even though the socket still looks open', (
      tester,
    ) async {
      // iOS freezes a backgrounded PWA and drops the socket without a close
      // event; `connected` stays true. Time away is what gives it up.
      final (fake, browser) = await pumpWithPolicy(
        tester,
        const ResumePolicy(staleAfter: Duration(milliseconds: 50)),
      );
      expect(fake.isConnected, isTrue);

      await setLifecycle(tester, AppLifecycleState.hidden);
      await tester.runAsync(
        () => Future<void>.delayed(const Duration(milliseconds: 120)),
      );
      await setLifecycle(tester, AppLifecycleState.resumed);
      browser.resume.add(null);
      await tester.pump();

      expect(fake.reconnectNowCalls, 1);
      fake.dispose();
    });
  });

  testWidgets('pretext-ready finalization retains selectable text and replay', (
    tester,
  ) async {
    _installPretextStub();
    addTearDown(_removePretextStub);
    final fake = await _pumpChat(tester);
    fake.emit({
      'type': 'token',
      'text': '> Hold position.\n\n3. Three\n4. Four',
    });
    await tester.pump();
    fake.emit({'type': 'done'});
    await tester.pump();
    expect(find.byType(CanvasMessageBubble), findsNothing);
    expect(find.byType(SelectableText), findsWidgets);
    expect(find.byTooltip('Replay Klukai voice'), findsOneWidget);
    fake.dispose();
  });

  group('ChatScreen — image frames', () {
    Future<void> drainImageTimers(WidgetTester tester) async {
      // The image frame's 2s input lock and the scroll retries (3 x 150ms).
      for (var i = 0; i < 3; i++) {
        await tester.pump(const Duration(milliseconds: 200));
      }
      await tester.pump(const Duration(seconds: 2));
    }

    testWidgets(
      'an image frame renders the picture, never the literal [IMAGE]',
      (tester) async {
        final fake = await _pumpChat(tester);
        fake.emit({'type': 'image', 'data': _pngB64});
        await tester.pump();

        expect(
          find.descendant(
            of: find.byType(MessageBubble),
            matching: find.byType(Image),
          ),
          findsOneWidget,
        );
        expect(find.byIcon(Icons.download), findsOneWidget);
        expect(find.text('[IMAGE]'), findsNothing);
        await drainImageTimers(tester);
        expect(tester.takeException(), isNull);
        fake.dispose();
      },
    );

    testWidgets(
      'with the canvas renderer ready, an image still goes through MessageBubble',
      (tester) async {
        // Finalized assistant text is drawn by CanvasMessageBubble once the
        // pretext bridge is up — but that bubble only draws text (it showed the
        // literal "[IMAGE]") and has no speaker. Routing must send images
        // around it.
        _installPretextStub();
        addTearDown(_removePretextStub);
        final fake = await _pumpChat(tester);
        fake.emit({'type': 'image', 'data': _pngB64});
        await tester.pump();

        expect(find.byType(CanvasMessageBubble), findsNothing);
        expect(find.byType(MessageBubble), findsOneWidget);
        expect(find.byIcon(Icons.download), findsOneWidget);
        expect(find.text('[IMAGE]'), findsNothing);
        await drainImageTimers(tester);
        fake.dispose();
      },
    );
  });

  group('ChatScreen — a reply cut off mid-stream', () {
    MessageBubble bubbleWith(WidgetTester tester, String content) =>
        tester.widget<MessageBubble>(
          find.byWidgetPredicate(
            (w) => w is MessageBubble && w.message.content == content,
          ),
        );

    bool anyStreaming(WidgetTester tester) => tester
        .widgetList<MessageBubble>(find.byType(MessageBubble))
        .any((b) => b.message.isStreaming);

    testWidgets(
      'an error frame freezes the partial bubble and frees the composer',
      (tester) async {
        final fake = await _pumpChat(tester);
        TextField tf() => tester.widget<TextField>(find.byType(TextField));

        fake.emit({'type': 'token', 'text': 'Orders rec'});
        await tester.pump();
        expect(tf().readOnly, isTrue);
        expect(bubbleWith(tester, 'Orders rec').message.isStreaming, isTrue);

        const disrupted =
            'Communications disrupted, Commander. Standby for reconnection.';
        fake.emit({'type': 'error', 'message': disrupted});
        await tester.pump();

        // What arrived stays, frozen; her line follows as a bubble of its own.
        expect(bubbleWith(tester, 'Orders rec').message.isStreaming, isFalse);
        expect(find.text(disrupted), findsOneWidget);
        expect(anyStreaming(tester), isFalse);
        expect(tf().readOnly, isFalse);

        // The next reply gets a new bubble rather than extending the dead one.
        fake.emit({'type': 'token', 'text': 'Back online.'});
        await tester.pump();
        expect(find.text('Orders rec'), findsOneWidget);
        expect(find.text('Back online.'), findsOneWidget);
        fake.emit({'type': 'done'});
        await tester.pump();
        expect(tester.takeException(), isNull);
        fake.dispose();
      },
    );

    testWidgets(
      'a dropped link mid-reply freezes the bubble, clears the indicators, unlocks',
      (tester) async {
        final fake = await _pumpChat(tester);
        TextField tf() => tester.widget<TextField>(find.byType(TextField));

        fake.emit({
          'type': 'tool_use',
          'tool': 'web_search',
          'status': 'calling',
        });
        await tester.pump();
        fake.emit({'type': 'token', 'text': 'Standing by'});
        await tester.pump();
        expect(find.byType(ToolStatusIndicator), findsOneWidget);
        expect(tf().readOnly, isTrue);

        fake.setConnected(false);
        await tester.pump();

        expect(find.text('LINK DOWN'), findsOneWidget);
        expect(bubbleWith(tester, 'Standing by').message.isStreaming, isFalse);
        expect(find.byType(ToolStatusIndicator), findsNothing);
        expect(tf().readOnly, isFalse); // not locked for the 60s safety timeout

        // Link back: the next reply is its own bubble, not appended to the dead one.
        fake.setConnected(true);
        await tester.pump();
        fake.emit({'type': 'token', 'text': 'Link restored.'});
        await tester.pump();
        expect(find.text('Standing by'), findsOneWidget);
        expect(find.text('Link restored.'), findsOneWidget);
        fake.emit({'type': 'done'});
        await tester.pump();
        // Let the (failing) history refetch settle.
        await tester.pump(const Duration(seconds: 1));
        expect(tester.takeException(), isNull);
        fake.dispose();
      },
    );
  });

  group('ChatScreen — status (thinking) frames', () {
    testWidgets(
      'an empty thinking frame clears the status instead of spinning forever',
      (tester) async {
        final fake = await _pumpChat(tester);

        // Current frames carry `content`...
        fake.emit({'type': 'thinking', 'content': 'checking the hangar'});
        await tester.pump();
        expect(find.text('CHECKING THE HANGAR'), findsOneWidget);

        // ...and the backend clears its status with an empty one.
        fake.emit({'type': 'thinking', 'content': ''});
        await tester.pump();
        expect(find.text('CHECKING THE HANGAR'), findsNothing);
        expect(find.text('PROCESSING'), findsNothing);

        // Older frames carry `text`.
        fake.emit({'type': 'thinking', 'text': 'loading the model'});
        await tester.pump();
        expect(find.text('LOADING THE MODEL'), findsOneWidget);
        fake.emit({'type': 'thinking'});
        await tester.pump();
        expect(find.text('LOADING THE MODEL'), findsNothing);
        expect(find.text('PROCESSING'), findsNothing);
        fake.dispose();
      },
    );
  });

  group('ChatScreen — done.text is her final line', () {
    testWidgets('done.text replaces the raw stream; an empty one keeps it', (
      tester,
    ) async {
      final fake = await _pumpChat(tester);

      fake.emit({'type': 'token', 'text': '<think>weighing it</think>'});
      await tester.pump();
      fake.emit({'type': 'token', 'text': 'Understood.'});
      await tester.pump();
      fake.emit({
        'type': 'done',
        'model': 'venice',
        'text': 'Understood, Commander.',
      });
      await tester.pump();
      expect(find.text('Understood, Commander.'), findsOneWidget);
      expect(find.textContaining('<think>'), findsNothing);

      fake.emit({'type': 'token', 'text': 'Second line.'});
      await tester.pump();
      fake.emit({'type': 'done', 'text': ''});
      await tester.pump();
      expect(find.text('Second line.'), findsOneWidget);
      fake.dispose();
    });
  });

  group('ChatScreen — the first token owns the lock', () {
    testWidgets(
      'a reply arriving under a shorter lock is locked for the whole reply',
      (tester) async {
        final fake = await _pumpChat(tester);
        TextField tf() => tester.widget<TextField>(find.byType(TextField));

        // A proactive line locks for 1.5s...
        fake.emit({'type': 'proactive', 'message': 'Commander.'});
        await tester.pump();
        expect(find.textContaining('INCOMING TRANSMISSION'), findsOneWidget);

        // ...and her reply starts inside that window.
        fake.emit({'type': 'token', 'text': 'Status: '});
        await tester.pump();
        expect(find.textContaining('RECEIVING TRANSMISSION'), findsOneWidget);

        // Past the proactive lock's 1.5s the composer is still hers.
        await tester.pump(const Duration(milliseconds: 1600));
        expect(tf().readOnly, isTrue);
        expect(find.textContaining('RECEIVING TRANSMISSION'), findsOneWidget);

        fake.emit({'type': 'done'});
        await tester.pump();
        expect(tf().readOnly, isFalse);
        fake.dispose();
      },
    );
  });

  group('ChatScreen — HUD composer', () {
    VoiceLanguage language(WidgetTester tester) => tester
        .widget<HudSegmented<VoiceLanguage>>(
          find.byKey(const Key('composer-language')),
        )
        .value;

    Finder segment(String label) => find.descendant(
      of: find.byKey(const Key('composer-language')),
      matching: find.text(label),
    );

    testWidgets('EN | JP loads her saved language and saves a change', (
      tester,
    ) async {
      final backend = _VoiceSettingsBackend(language: 'ja');
      final fake = await _pumpChat(tester, voiceSettings: backend.service());
      await tester.pump();
      expect(language(tester), VoiceLanguage.ja);

      await tester.tap(segment('EN'));
      await tester.pump();
      await tester.pump();
      expect(backend.saved, ['en']);
      expect(language(tester), VoiceLanguage.en);
      expect(find.byKey(const Key('voice-language-error')), findsNothing);
      fake.dispose();
    });

    testWidgets('a failed save puts the switch back and says so', (
      tester,
    ) async {
      final backend = _VoiceSettingsBackend(putStatus: 503);
      final fake = await _pumpChat(tester, voiceSettings: backend.service());
      await tester.pump();
      expect(language(tester), VoiceLanguage.en);

      await tester.tap(segment('JP'));
      await tester.pump();
      await tester.pump();
      expect(backend.saved, ['ja']);
      expect(language(tester), VoiceLanguage.en);
      expect(find.byKey(const Key('voice-language-error')), findsOneWidget);
      expect(find.textContaining('NOT SAVED'), findsOneWidget);
      fake.dispose();
    });

    testWidgets('no voice settings endpoint yet: English, quietly', (
      tester,
    ) async {
      final fake = await _pumpChat(
        tester,
        voiceSettings: VoiceSettingsService(
          serverUrl: 'http://localhost:0',
          client: MockClient((_) async => http.Response('Not Found', 404)),
        ),
      );
      await tester.pump();
      expect(language(tester), VoiceLanguage.en);
      expect(find.byKey(const Key('voice-language-error')), findsNothing);
      fake.dispose();
    });

    testWidgets('the picture button asks for one and leaves his draft alone', (
      tester,
    ) async {
      final fake = await _pumpChat(tester);
      await tester.enterText(find.byType(TextField), 'half-typed thought');
      await tester.tap(find.byKey(const Key('composer-picture')));
      await tester.pump();

      expect(fake.sent.last['type'], 'message');
      expect(fake.sent.last['content'], kPictureRequest);
      expect(find.text(kPictureRequest), findsOneWidget);
      expect(
        tester.widget<TextField>(find.byType(TextField)).controller!.text,
        'half-typed thought',
      );
      fake.dispose();
    });

    testWidgets('picture and SEND are off while the link is down', (
      tester,
    ) async {
      final fake = await _pumpChat(tester);
      HudSquareButton button(String key) =>
          tester.widget<HudSquareButton>(find.byKey(Key(key)));
      expect(button('composer-picture').onTap, isNotNull);
      expect(button('composer-send').onTap, isNotNull);

      fake.setConnected(false);
      await tester.pump();
      expect(button('composer-picture').onTap, isNull);
      expect(button('composer-send').onTap, isNull);
      await tester.tap(find.byKey(const Key('composer-picture')));
      await tester.pump();
      expect(fake.sent, isEmpty);
      fake.dispose();
    });

    testWidgets('the mic is hidden while voice input is off', (tester) async {
      final off = await _pumpChat(tester);
      expect(find.byType(VoiceButton), findsNothing);
      off.dispose();
      final on = await _pumpChat(
        tester,
        voiceInput: true,
        speechBridge: SpeechBridge(
          start: () async => false,
          stop: () async {
            return null;
          },
        ),
      );
      expect(find.byType(VoiceButton), findsOneWidget);
      on.dispose();
    });

    testWidgets('the menu keeps Threads, Her POV, Subscription and logout', (
      tester,
    ) async {
      final fake = await _pumpChat(tester);
      await _openMenu(tester);
      for (final action in ChatMenuAction.values) {
        expect(
          find.byKey(Key('chat-menu-${action.name}')),
          findsOneWidget,
          reason: action.name,
        );
      }
      fake.dispose();
    });
  });

  group('ChatScreen — her voice only on a tap', () {
    testWidgets('replies, proactive lines and voice letters stay silent', (
      tester,
    ) async {
      final browser = FakeBrowserPlatform();
      final fake = await _pumpChat(tester, browser: browser);
      fake.emit({'type': 'token', 'text': 'You are late, Commander.'});
      fake.emit({'type': 'done', 'text': 'You are late, Commander.'});
      fake.emit({'type': 'proactive', 'message': 'The stars are clear.'});
      fake.emit({'type': 'voice_audio', 'audio': 'UklGRg=='});
      fake.emit({
        'type': 'voice_letter',
        'message': 'I kept your seat warm on the bike.',
        'voice_id': 'note-7',
      });
      await tester.pump();
      await tester.pump(const Duration(seconds: 2));

      expect(browser.voices, isEmpty);
      // Every line of hers has its own VOICE, ready for a tap.
      expect(find.byKey(const Key('voice-play')), findsNWidgets(3));
      // The letter's VOICE plays her recorded note, not a re-read.
      final letter = tester.widget<MessageBubble>(
        find.byWidgetPredicate(
          (w) =>
              w is MessageBubble &&
              w.message.content == 'I kept your seat warm on the bike.',
        ),
      );
      expect(letter.voiceOverride, isNotNull);
      fake.dispose();
    });
  });

  group('ChatScreen — the 3D view', () {
    Future<(FakeWebSocketService, _FakeStage, FakeBrowserPlatform)> pump3d(
      WidgetTester tester, {
      http.Client? httpClient,
    }) async {
      final stage = _FakeStage();
      final browser = FakeBrowserPlatform();
      final fake = await _pumpChat(
        tester,
        stage: stage,
        browser: browser,
        httpClient: httpClient,
      );
      await tester.tap(find.byKey(const Key('view-flip-3d')));
      await tester.pump();
      return (fake, stage, browser);
    }

    Future<void> herLine(FakeWebSocketService fake, WidgetTester t) async {
      fake.emit({'type': 'token', 'text': 'Sit. The stars are clear tonight.'});
      fake.emit({'type': 'done', 'text': 'Sit. The stars are clear tonight.'});
      await t.pump();
    }

    testWidgets('CHAT | 3D flips to her embedded stage and back', (
      tester,
    ) async {
      final (fake, stage, _) = await pump3d(tester);
      expect(find.byKey(const Key('fake-stage')), findsOneWidget);
      expect(find.text('3D LINK // LIVE'), findsOneWidget);
      expect(find.byKey(const Key('stage-tags')), findsOneWidget);
      expect(find.byKey(const Key('stage-dialogue')), findsOneWidget);
      expect(find.byKey(const Key('chat-composer')), findsOneWidget);
      expect(stage.src, companionStageSrc());
      expect(stage.src, startsWith('companion.html?embed=1&mode=3d'));
      expect(tester.takeException(), isNull);

      await tester.tap(find.byKey(const Key('view-flip-chat')));
      await tester.pump();
      // Still loaded, parked under the CHAT face; the 3D HUD is gone.
      expect(find.byKey(const Key('fake-stage')), findsOneWidget);
      expect(stage.visible, isFalse);
      expect(find.text('3D LINK // LIVE'), findsNothing);
      expect(find.text('H.I.D.E. 404 // SECURE LINK'), findsOneWidget);
      fake.dispose();
    });

    testWidgets('her stage loads once and stays loaded across flips', (
      tester,
    ) async {
      final (fake, stage, _) = await pump3d(tester);
      for (var i = 0; i < 3; i++) {
        await tester.tap(find.byKey(const Key('view-flip-chat')));
        await tester.pump();
        expect(stage.visible, isFalse);
        await tester.tap(find.byKey(const Key('view-flip-3d')));
        await tester.pump();
        expect(stage.visible, isTrue);
        expect(find.text('3D LINK // LIVE'), findsOneWidget);
      }
      // Built once, never torn down: no reload, no new socket.
      expect(stage.created, 1);
      expect(stage.disposed, 0);
      expect(stage.src, companionStageSrc());
      expect(tester.takeException(), isNull);
      fake.dispose();
    });

    testWidgets('parked under CHAT, the stage takes no taps; in 3D it does', (
      tester,
    ) async {
      tester.view.physicalSize = const Size(390, 844);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.reset);
      final (fake, stage, _) = await pump3d(tester);
      final middle = tester.getCenter(find.byType(ChatScreen));
      await tester.tapAt(middle);
      await tester.pump();
      expect(stage.taps, 1); // as before: the open middle of 3D is hers

      await tester.tap(find.byKey(const Key('view-flip-chat')));
      await tester.pump();
      expect(
        tester
            .widget<IgnorePointer>(find.byKey(const Key('chat-stage-layer')))
            .ignoring,
        isTrue,
      );
      await tester.tapAt(middle);
      await tester.pump();
      expect(stage.taps, 1);
      // The CHAT face is fully his: the composer still takes his words.
      await tester.enterText(find.byType(TextField), 'Status?');
      await tester.pump();
      expect(find.text('Status?'), findsOneWidget);

      await tester.tap(find.byKey(const Key('view-flip-3d')));
      await tester.pump();
      await tester.tapAt(middle);
      await tester.pump();
      expect(stage.taps, 2);
      fake.dispose();
    });

    testWidgets('back in 3D she is already tracking: no LINKING 3D again', (
      tester,
    ) async {
      tester.view.physicalSize = const Size(390, 844);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.reset);
      final (fake, stage, _) = await pump3d(tester);
      stage.onEvent!('klukai:stage-ready', {'ready': true});
      await tester.pump();
      expect(find.text('◢ TRACKING'), findsOneWidget);
      await tester.tap(find.byKey(const Key('view-flip-chat')));
      await tester.pump();
      await tester.tap(find.byKey(const Key('view-flip-3d')));
      await tester.pump();
      expect(find.text('◢ TRACKING'), findsOneWidget);
      expect(find.text('◢ LINKING 3D…'), findsNothing);
      fake.dispose();
    });

    testWidgets('a flip stops waiting on her voice; the next tap is fresh', (
      tester,
    ) async {
      final (fake, stage, browser) = await pump3d(tester);
      await herLine(fake, tester);
      await tester.tap(find.byKey(const Key('voice-play')));
      await tester.pump();
      expect(stage.link.spoken, hasLength(1));
      // He flips away mid-synth: the parked stage drops the line.
      await tester.tap(find.byKey(const Key('view-flip-chat')));
      await tester.pump();
      await tester.tap(find.byKey(const Key('view-flip-3d')));
      await tester.pump();
      // A late answer for that tap changes nothing; nothing plays by itself.
      stage.onEvent!('klukai:speak-status', {'id': 1, 'status': 'unavailable'});
      await tester.pump();
      expect(browser.voices, isEmpty);
      await tester.tap(find.byKey(const Key('voice-play')));
      await tester.pump();
      expect(stage.link.spoken.last, ('Sit. The stars are clear tonight.', 2));
      stage.onEvent!('klukai:speak-status', {'id': 2, 'status': 'empty'});
      await tester.pump(const Duration(seconds: 1)); // CHAT's scroll retries
      fake.dispose();
    });

    testWidgets('opens in 3D when his Me setting says so', (tester) async {
      web.window.localStorage.setItem(kOpenIn3dPrefKey, 'true');
      final fake = await _pumpChat(tester);
      expect(find.byKey(const Key('fake-stage')), findsOneWidget);
      fake.dispose();
    });

    testWidgets('…including when it was saved through shared_preferences', (
      tester,
    ) async {
      web.window.localStorage.setItem('flutter.$kOpenIn3dPrefKey', 'true');
      final fake = await _pumpChat(tester);
      expect(find.byKey(const Key('fake-stage')), findsOneWidget);
      fake.dispose();
    });

    testWidgets('opens in Chat by default', (tester) async {
      final fake = await _pumpChat(tester);
      expect(find.byKey(const Key('fake-stage')), findsNothing);
      expect(find.byKey(const Key('chat-header')), findsOneWidget);
      fake.dispose();
    });

    testWidgets('VOICE asks the stage to say her line, once per tap', (
      tester,
    ) async {
      final (fake, stage, browser) = await pump3d(tester);
      await herLine(fake, tester);
      expect(find.text('Sit. The stars are clear tonight.'), findsOneWidget);
      // Nothing is spoken until he taps.
      expect(stage.link.spoken, isEmpty);
      expect(browser.voices, isEmpty);

      await tester.tap(find.byKey(const Key('voice-play')));
      await tester.pump();
      expect(stage.link.spoken, [('Sit. The stars are clear tonight.', 1)]);
      // Synthesizing: the button is busy, a second tap is not a second line.
      await tester.tap(find.byKey(const Key('voice-play')));
      await tester.pump();
      expect(stage.link.spoken, hasLength(1));

      stage.onEvent!('klukai:speak-status', {'id': 1, 'status': 'playing'});
      stage.onEvent!('klukai:speaking', {'on': true});
      await tester.pump();
      expect(find.text('◢ VOICE · LIP SYNC'), findsNothing); // not ready yet
      stage.onEvent!('klukai:speaking', {'on': false});
      await tester.pump();

      await tester.tap(find.byKey(const Key('voice-play')));
      await tester.pump();
      expect(stage.link.spoken.last, ('Sit. The stars are clear tonight.', 2));
      expect(browser.voices, isEmpty);
      // The answer-timeout must not fire after the test.
      stage.onEvent!('klukai:speak-status', {'id': 2, 'status': 'empty'});
      await tester.pump();
      fake.dispose();
    });

    testWidgets('when the stage cannot play, the PWA voice reads the line', (
      tester,
    ) async {
      final paths = <String>[];
      final tts = MockClient((request) async {
        paths.add(request.url.path);
        return http.Response(jsonEncode({'audio': 'UklGRg=='}), 200);
      });
      final (fake, stage, browser) = await pump3d(tester, httpClient: tts);
      await herLine(fake, tester);
      await tester.tap(find.byKey(const Key('voice-play')));
      await tester.pump();
      stage.onEvent!('klukai:speak-status', {'id': 1, 'status': 'unavailable'});
      for (var i = 0; i < 10; i++) {
        await tester.pump(const Duration(milliseconds: 10));
      }
      expect(find.textContaining('VOICE SYNTH OFFLINE'), findsNothing);
      expect(paths, ['/api/tts', '/api/tts']);
      // Her two sentences, read through the PWA's own channel.
      expect(browser.voices, [
        'data:audio/wav;base64,UklGRg==',
        'data:audio/wav;base64,UklGRg==',
      ]);
      fake.dispose();
    });

    testWidgets('a synth failure says so once per tap', (tester) async {
      final (fake, stage, _) = await pump3d(tester);
      await herLine(fake, tester);
      await tester.tap(find.byKey(const Key('voice-play')));
      await tester.pump();
      stage.onEvent!('klukai:speak-status', {'id': 1, 'status': 'playing'});
      for (var i = 0; i < 3; i++) {
        stage.onEvent!('klukai:voice-error', {});
      }
      await tester.pump();
      expect(find.textContaining('VOICE SYNTH OFFLINE'), findsOneWidget);
      // The control is ready for another tap.
      expect(
        tester.widget<InkWell>(find.byKey(const Key('voice-play'))).onTap,
        isNotNull,
      );
      await tester.pump(const Duration(seconds: 5));
      fake.dispose();
    });

    testWidgets('a stale answer for another tap is ignored', (tester) async {
      final (fake, stage, browser) = await pump3d(tester);
      await herLine(fake, tester);
      await tester.tap(find.byKey(const Key('voice-play')));
      await tester.pump();
      stage.onEvent!('klukai:speak-status', {
        'id': 99,
        'status': 'unavailable',
      });
      await tester.pump();
      expect(browser.voices, isEmpty);
      stage.onEvent!('klukai:speak-status', {'id': 1, 'status': 'empty'});
      await tester.pump();
      fake.dispose();
    });

    testWidgets('the tracking readout follows what the stage reports', (
      tester,
    ) async {
      tester.view.physicalSize = const Size(390, 844);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.reset);
      final (fake, stage, _) = await pump3d(tester);
      expect(find.text('◢ LINKING 3D…'), findsOneWidget);
      stage.onEvent!('klukai:stage-notice', {'text': 'Loading her 3D model…'});
      await tester.pump();
      expect(find.text('◢ LOADING HER 3D MODEL…'), findsOneWidget);
      stage.onEvent!('klukai:stage-notice', {'text': ''});
      stage.onEvent!('klukai:stage-ready', {'ready': true});
      await tester.pump();
      expect(find.text('◢ TRACKING'), findsOneWidget);
      expect(find.text('◢ IDLE · BREATH'), findsOneWidget);
      fake.dispose();
    });

    testWidgets('her thinking status shows in the dialogue panel', (
      tester,
    ) async {
      final (fake, _, _) = await pump3d(tester);
      fake.emit({'type': 'thinking', 'content': 'Checking the hangar'});
      await tester.pump();
      expect(find.text('CHECKING THE HANGAR'), findsOneWidget);
      expect(find.byKey(const Key('voice-play')), findsNothing);
      fake.emit({'type': 'thinking', 'content': ''});
      await tester.pump();
      fake.dispose();
    });

    testWidgets('another tab tears the stage down; Chat brings it back', (
      tester,
    ) async {
      final (fake, stage, _) = await pump3d(tester);
      await tester.tap(find.byKey(const Key('hud-tab-kit')));
      await tester.pump();
      expect(find.byKey(const Key('fake-stage')), findsNothing);
      expect(stage.disposed, 1);
      await tester.tap(find.byKey(const Key('hud-tab-chat')));
      await tester.pump();
      expect(find.byKey(const Key('fake-stage')), findsOneWidget);
      expect(stage.created, 2);
      fake.dispose();
    });

    testWidgets('…also when it was parked under CHAT', (tester) async {
      final (fake, stage, _) = await pump3d(tester);
      await tester.tap(find.byKey(const Key('view-flip-chat')));
      await tester.pump();
      await tester.tap(find.byKey(const Key('hud-tab-kit')));
      await tester.pump();
      expect(find.byKey(const Key('fake-stage')), findsNothing);
      expect(stage.disposed, 1);
      fake.dispose();
    });
  });

  group('ChatScreen — the stage warms up behind CHAT', () {
    testWidgets('once he has used 3D here, it loads parked after history', (
      tester,
    ) async {
      web.window.localStorage.setItem(kStageUsedKey, '1');
      final stage = _FakeStage();
      final fake = await _pumpChat(
        tester,
        stage: stage,
        historyClient: _historyBackend(),
      );
      await tester.pump();
      expect(find.text('Report, Commander.'), findsOneWidget);
      // Not with the transcript: it goes first.
      expect(find.byKey(const Key('fake-stage')), findsNothing);
      await tester.pump(const Duration(seconds: 3));
      expect(find.byKey(const Key('fake-stage')), findsOneWidget);
      expect(stage.visible, isFalse);
      expect(stage.src, companionStageSrc(hidden: true));
      expect(stage.src, contains('&hidden=1'));
      // Still the CHAT face, fully his.
      expect(find.byKey(const Key('chat-header')), findsOneWidget);
      expect(
        tester
            .widget<IgnorePointer>(find.byKey(const Key('chat-stage-layer')))
            .ignoring,
        isTrue,
      );

      // His first flip finds her there: the same stage, now shown.
      await tester.tap(find.byKey(const Key('view-flip-3d')));
      await tester.pump();
      expect(stage.visible, isTrue);
      expect(stage.created, 1);
      expect(find.text('3D LINK // LIVE'), findsOneWidget);
      fake.dispose();
    });

    testWidgets('not on a device where he has never opened 3D', (tester) async {
      final stage = _FakeStage();
      final fake = await _pumpChat(
        tester,
        stage: stage,
        historyClient: _historyBackend(),
      );
      await tester.pump();
      await tester.pump(const Duration(seconds: 10));
      expect(find.byKey(const Key('fake-stage')), findsNothing);
      expect(stage.created, 0);
      // Opening 3D remembers it for next time.
      await tester.tap(find.byKey(const Key('view-flip-3d')));
      await tester.pump();
      expect(web.window.localStorage.getItem(kStageUsedKey), '1');
      fake.dispose();
    });

    testWidgets(
      'leaving the Chat tab first cancels it; coming back re-arms it',
      (tester) async {
        web.window.localStorage.setItem(kStageUsedKey, '1');
        final stage = _FakeStage();
        final fake = await _pumpChat(
          tester,
          stage: stage,
          historyClient: _historyBackend(),
        );
        await tester.pump();
        await tester.tap(find.byKey(const Key('hud-tab-kit')));
        await tester.pump(const Duration(seconds: 5));
        expect(stage.created, 0);
        await tester.tap(find.byKey(const Key('hud-tab-chat')));
        await tester.pump();
        expect(find.byKey(const Key('fake-stage')), findsNothing);
        await tester.pump(const Duration(seconds: 3));
        expect(find.byKey(const Key('fake-stage')), findsOneWidget);
        expect(stage.visible, isFalse);
        fake.dispose();
      },
    );
  });

  group('ChatScreen — phone widths', () {
    for (final width in [360.0, 390.0, 430.0]) {
      testWidgets('no overflow at ${width.toInt()}px, chat and 3D', (
        tester,
      ) async {
        tester.view.physicalSize = Size(width, 780);
        tester.view.devicePixelRatio = 1.0;
        addTearDown(tester.view.reset);
        web.window.localStorage.setItem('klukai_token', 'test-token');
        final fake = FakeWebSocketService();
        final stage = _FakeStage();
        await tester.pumpWidget(
          MaterialApp(
            home: ChatScreen(
              serverUrl: 'http://localhost:0',
              webSocketService: fake,
              wardrobeService: FakeWardrobeService(),
              voiceSettings: _VoiceSettingsBackend().service(),
              stageBuilder: stage.build,
            ),
          ),
        );
        await tester.pump();
        await tester.pump();
        fake.emit({'type': 'mood', 'mood': 'quietly_pleased'});
        fake.emit({'type': 'proactive', 'message': 'You are late, Commander.'});
        await tester.pump(const Duration(milliseconds: 1600));
        expect(find.byKey(const Key('chat-chip-wearing')), findsOneWidget);
        expect(tester.takeException(), isNull);

        await tester.tap(find.byKey(const Key('view-flip-3d')));
        await tester.pump();
        expect(tester.takeException(), isNull);
        fake.dispose();
      });
    }

    for (final size in const [
      Size(320, 480),
      Size(360, 560),
      Size(390, 600),
      Size(844, 390),
      Size(800, 600),
    ]) {
      testWidgets('no overflow at ${size.width.toInt()}×${size.height.toInt()} '
          'with tools, a status and a long draft, chat and 3D', (tester) async {
        tester.view.physicalSize = size;
        tester.view.devicePixelRatio = 1.0;
        addTearDown(tester.view.reset);
        final fake = await _pumpChat(tester);
        for (var i = 0; i < 6; i++) {
          fake.emit({'type': 'proactive', 'message': 'Line $i, Commander.'});
        }
        await tester.pump(const Duration(milliseconds: 1600));
        fake.emit({'type': 'tool_use', 'tool': 'web_search'});
        fake.emit({'type': 'tool_use', 'tool': 'read_file'});
        fake.emit({'type': 'tool_use', 'tool': 'run_code'});
        fake.emit({'type': 'thinking', 'content': 'Checking the hangar'});
        await tester.pump();
        await tester.enterText(
          find.byType(TextField),
          'one\ntwo\nthree\nfour\nfive',
        );
        await tester.pump();
        expect(tester.takeException(), isNull);
        expect(find.byKey(const Key('chat-composer')), findsOneWidget);
        // Short screens get the one-line header.
        expect(
          find.byKey(const Key('chat-header-portrait')),
          size.height - 70 < 500 ? findsNothing : findsOneWidget,
        );

        await tester.tap(find.byKey(const Key('view-flip-3d')));
        await tester.pump();
        expect(tester.takeException(), isNull);
        expect(find.byKey(const Key('stage-dialogue')), findsOneWidget);
        fake.emit({'type': 'thinking', 'content': ''});
        fake.dispose();
      });
    }

    testWidgets('the keyboard folds the header to one line', (tester) async {
      tester.view.physicalSize = const Size(390, 844);
      tester.view.devicePixelRatio = 1.0;
      tester.view.viewInsets = const FakeViewPadding(bottom: 320);
      addTearDown(tester.view.reset);
      final fake = await _pumpChat(tester);
      expect(find.byKey(const Key('chat-header-portrait')), findsNothing);
      expect(find.byKey(const Key('chat-header-link')), findsOneWidget);
      expect(find.byKey(const Key('chat-composer')), findsOneWidget);
      expect(tester.takeException(), isNull);
      fake.dispose();
    });
  });

  // Anything shielded (HudPointerShield) keeps drags and pinches from her
  // stage, so only the real controls may be.
  group('ChatScreen — 3D: she takes drags around the HUD', () {
    const readOnlyTags = [
      Key('stage-tag-affinity'),
      Key('stage-tag-mood'),
      Key('stage-tag-heart'),
    ];
    const wearingTag = Key('stage-tag-wearing');

    Future<FakeWebSocketService> pumpStage(
      WidgetTester tester,
      FakeWardrobeService wardrobe,
    ) async {
      tester.view.physicalSize = const Size(390, 844);
      tester.view.devicePixelRatio = 1.0;
      addTearDown(tester.view.reset);
      web.window.localStorage.setItem('klukai_token', 'test-token');
      final fake = FakeWebSocketService();
      await tester.pumpWidget(
        MaterialApp(
          home: ChatScreen(
            serverUrl: 'http://localhost:0',
            webSocketService: fake,
            wardrobeService: wardrobe,
            voiceSettings: _VoiceSettingsBackend().service(),
            stageBuilder: _FakeStage().build,
          ),
        ),
      );
      await tester.pump();
      await tester.pump();
      await tester.tap(find.byKey(const Key('view-flip-3d')));
      await tester.pump();
      return fake;
    }

    List<Rect> shields(WidgetTester tester) => [
      for (var i = 0; i < find.byType(HudPointerShield).evaluate().length; i++)
        tester.getRect(find.byType(HudPointerShield).at(i)),
    ];

    bool ignored(WidgetTester tester, Finder finder) => tester
        .widgetList<IgnorePointer>(
          find.ancestor(
            of: finder,
            matching: find.byType(IgnorePointer),
            matchRoot: true,
          ),
        )
        .any((w) => w.ignoring);

    testWidgets('only the controls are shielded; WEARING on its own box', (
      tester,
    ) async {
      final fake = await pumpStage(tester, FakeWardrobeService());
      fake.emit({'type': 'token', 'text': 'Sit.'});
      fake.emit({'type': 'done', 'text': 'Sit.'});
      await tester.pump();

      final rects = shields(tester);
      // The top bar, the dialogue + composer, and the WEARING tag.
      expect(rects, hasLength(3));
      expect(rects, contains(tester.getRect(find.byKey(wearingTag))));
      bool shielded(Offset p) => rects.any((r) => r.contains(p));
      for (final control in [
        find.byKey(const Key('view-flip-chat')),
        find.byKey(const Key('voice-play')),
        find.byKey(const Key('chat-composer')),
      ]) {
        expect(shielded(tester.getCenter(control)), isTrue, reason: '$control');
      }

      final stage = tester.getRect(find.byKey(const Key('chat-stage')));
      final column = tester.getRect(find.byKey(const Key('stage-tags')));
      final affinity = tester.getRect(find.byKey(readOnlyTags[0]));
      final wearing = tester.getRect(find.byKey(wearingTag));
      final open = {
        'right edge, mid-height': Offset(stage.right - 40, stage.center.dy),
        'right edge, under the tags': Offset(
          stage.right - 24,
          wearing.bottom + 16,
        ),
        for (final key in readOnlyTags)
          'the $key tag': tester.getCenter(find.byKey(key)),
        'beside a short tag': Offset(column.left + 2, affinity.center.dy),
        'the bracket': tester.getCenter(find.byKey(const Key('stage-bracket'))),
        'the readout': tester.getCenter(
          find.byKey(const Key('stage-tracking')),
        ),
      };
      open.forEach((where, point) {
        expect(stage.contains(point), isTrue, reason: where);
        expect(shielded(point), isFalse, reason: where);
      });
      // Decorative and read-only parts let every pointer through.
      for (final key in const [
        Key('stage-bracket'),
        Key('stage-tracking'),
        ...readOnlyTags,
      ]) {
        expect(ignored(tester, find.byKey(key)), isTrue, reason: '$key');
        expect(
          find.ancestor(
            of: find.byKey(key),
            matching: find.byType(HudPointerShield),
          ),
          findsNothing,
        );
      }
      expect(tester.takeException(), isNull);
      fake.dispose();
    });

    testWidgets('WEARING opens her day; before it loads, nothing is shielded', (
      tester,
    ) async {
      final wardrobe = _LateDayWardrobe();
      final fake = await pumpStage(tester, wardrobe);
      expect(find.byKey(const Key('stage-tags')), findsOneWidget);
      expect(find.byKey(wearingTag), findsNothing);
      // Just the top bar and the dialogue + composer.
      expect(shields(tester), hasLength(2));

      wardrobe.release();
      await tester.pump();
      await tester.pump();
      expect(shields(tester), hasLength(3));
      // A read-only tag opens nothing.
      await tester.tap(find.byKey(readOnlyTags[1]), warnIfMissed: false);
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));
      expect(find.byType(BottomSheet), findsNothing);

      await tester.tap(find.byKey(wearingTag));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));
      expect(find.byType(BottomSheet), findsOneWidget);
      expect(tester.takeException(), isNull);
      fake.dispose();
    });
  });
}

/// Her day only arrives when the test says so, after the transcript has
/// loaded: the order a slow /api/her-day produces in production.
class _LateDayWardrobe extends FakeWardrobeService {
  final _gate = Completer<void>();

  void release() => _gate.complete();

  @override
  Future<HerDay> fetchHerDay() async {
    await _gate.future;
    return super.fetchHerDay();
  }
}
