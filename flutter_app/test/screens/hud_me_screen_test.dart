// HudMeScreen with a MockClient (stats, milestones, voice settings) and an
// in-memory LocalPrefs; VM-runnable.
//   flutter test test/screens/hud_me_screen_test.dart
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:companion_app/screens/hud_me_screen.dart';
import 'package:companion_app/services/local_prefs.dart';
import 'package:companion_app/services/voice_settings_service.dart';

class _Server {
  _Server({this.voiceStatus = 200, this.putStatus = 200});

  String language = 'ja';
  int voiceStatus;
  int putStatus;
  int statsStatus = 200;
  final List<String> puts = [];

  late final client = MockClient((req) async {
    switch (req.url.path) {
      case '/api/user/stats':
        return http.Response(
          jsonEncode({
            'user_id': 'jalsarraf',
            'days_active': 188,
            'memories': {'total': 700, 'kept': 595, 'with_image': 590},
            'affection': {'consecutive_days': 42, 'total_interactions': 9000},
          }),
          statsStatus,
        );
      case '/api/milestones':
        return http.Response(
          jsonEncode({
            'milestones': {'first_kiss': '2026-05-01'},
          }),
          200,
        );
      case '/api/voice/settings':
        if (req.method == 'PUT') {
          final lang = (jsonDecode(req.body) as Map)['language'] as String;
          puts.add(lang);
          if (putStatus != 200) return http.Response('{}', putStatus);
          language = lang;
          return http.Response(jsonEncode({'language': lang}), 200);
        }
        return http.Response(jsonEncode({'language': language}), voiceStatus);
    }
    return http.Response('{}', 404);
  });
}

Future<(_Server, MemoryLocalPrefs)> _pump(
  WidgetTester tester, {
  _Server? server,
  MemoryLocalPrefs? prefs,
  VoidCallback? onLogout,
  VoidCallback? onAuthExpired,
  double width = 390,
}) async {
  tester.view.physicalSize = Size(width, 2600);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.reset);
  final s = server ?? _Server();
  final p = prefs ?? MemoryLocalPrefs();
  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(
        body: HudMeScreen(
          serverUrl: 'http://127.0.0.1:1',
          affectionScore: 1000,
          affectionLevel: 9,
          affectionLevelName: 'Devoted',
          onLogout: onLogout ?? () {},
          onAuthExpired: onAuthExpired,
          client: s.client,
          prefs: p,
        ),
      ),
    ),
  );
  await tester.pumpAndSettle();
  return (s, p);
}

String _text(WidgetTester tester, Key key) => tester
    .widget<Text>(
      find.descendant(of: find.byKey(key), matching: find.byType(Text)).last,
    )
    .data!;

bool _selected(WidgetTester tester, String label) => isSemantics(
  isSelected: true,
).matches(tester.getSemantics(find.text(label)), <dynamic, dynamic>{});

void main() {
  testWidgets('bond: level, bar and the stats', (tester) async {
    await _pump(tester);
    expect(find.text('COMMANDER // PROFILE'), findsOneWidget);
    expect(find.text('JALSARRAF'), findsOneWidget);
    expect(find.text('BOND WITH KLUKAI'), findsOneWidget);
    expect(find.text('LV 9 · DEVOTED'), findsOneWidget);
    final bar = tester.widget<FractionallySizedBox>(
      find.byKey(const Key('me-bond-bar')),
    );
    expect(bar.widthFactor, 1.0);
    expect(_text(tester, const Key('me-days')), '188');
    expect(_text(tester, const Key('me-streak')), '42');
    expect(_text(tester, const Key('me-memories')), '595');
  });

  testWidgets('stats endpoint down: dashes, not zeros', (tester) async {
    final s = _Server()..statsStatus = 503;
    await _pump(tester, server: s);
    expect(_text(tester, const Key('me-days')), '—');
    expect(find.text('JALSARRAF'), findsNothing);
  });

  testWidgets('voice language loads JP and saves EN', (tester) async {
    final (server, _) = await _pump(tester);
    expect(_selected(tester, 'JP'), isTrue);
    expect(_selected(tester, 'EN'), isFalse);
    expect(
      find.textContaining('Plays only when you tap', findRichText: true),
      findsOneWidget,
    );

    await tester.tap(find.text('EN'));
    await tester.pumpAndSettle();
    expect(server.puts, ['en']);
    expect(_selected(tester, 'EN'), isTrue);

    await tester.tap(find.text('JP'));
    await tester.pumpAndSettle();
    expect(server.puts, ['en', 'ja']);
    expect(_selected(tester, 'JP'), isTrue);
  });

  testWidgets('a language change in the chat composer shows here too', (
    tester,
  ) async {
    // The Me tab stays mounted in the tab stack and read the language once;
    // switched to EN in the composer, it still showed JP.
    tester.view.physicalSize = const Size(390, 2600);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    final s = _Server();
    var language = VoiceLanguage.ja;
    late StateSetter rebuild;
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: StatefulBuilder(
            builder: (context, setState) {
              rebuild = setState;
              return HudMeScreen(
                serverUrl: 'http://127.0.0.1:1',
                affectionScore: 1000,
                affectionLevel: 9,
                affectionLevelName: 'Devoted',
                onLogout: () {},
                client: s.client,
                prefs: MemoryLocalPrefs(),
                voiceLanguage: language,
              );
            },
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();
    expect(_selected(tester, 'JP'), isTrue);

    s.language = 'en'; // the composer saved it
    rebuild(() => language = VoiceLanguage.en);
    await tester.pumpAndSettle();
    expect(_selected(tester, 'EN'), isTrue);
    expect(_selected(tester, 'JP'), isFalse);
    expect(s.puts, isEmpty); // shown, not saved again
  });

  testWidgets('no voice endpoint: English', (tester) async {
    await _pump(tester, server: _Server(voiceStatus: 404));
    expect(_selected(tester, 'EN'), isTrue);
  });

  testWidgets('a failed save puts the old language back', (tester) async {
    final (server, _) = await _pump(tester, server: _Server(putStatus: 500));
    await tester.tap(find.text('EN'));
    await tester.pumpAndSettle();
    expect(server.puts, ['en']);
    expect(_selected(tester, 'JP'), isTrue);
    expect(find.byKey(const Key('me-voice-error')), findsOneWidget);
  });

  testWidgets('a dead token on the voice save goes to the shell', (
    tester,
  ) async {
    var bounced = 0;
    await _pump(
      tester,
      server: _Server(putStatus: 401),
      onAuthExpired: () => bounced++,
    );
    await tester.tap(find.text('EN'));
    await tester.pumpAndSettle();
    expect(bounced, 1);
  });

  testWidgets('voice input is shown off and disabled', (tester) async {
    await _pump(tester);
    expect(find.text('Mic is off for now · type to her'), findsOneWidget);
    expect(
      tester.getSemantics(find.byKey(const Key('me-voice-input'))),
      isSemantics(
        hasToggledState: true,
        isToggled: false,
        hasEnabledState: true,
        isEnabled: false,
      ),
    );
  });

  testWidgets('open in 3D by default writes the localStorage key', (
    tester,
  ) async {
    final (_, prefs) = await _pump(tester);
    expect(prefs.read(kOpenIn3dPrefKey), isNull);
    await tester.tap(find.byKey(const Key('me-open-3d')));
    await tester.pumpAndSettle();
    expect(prefs.read(kOpenIn3dPrefKey), 'true');
    expect(
      tester.getSemantics(find.byKey(const Key('me-open-3d'))),
      isSemantics(hasToggledState: true, isToggled: true),
    );
    await tester.tap(find.byKey(const Key('me-open-3d')));
    await tester.pumpAndSettle();
    expect(prefs.read(kOpenIn3dPrefKey), 'false');
  });

  testWidgets('a saved 3D preference shows on', (tester) async {
    await _pump(tester, prefs: MemoryLocalPrefs({kOpenIn3dPrefKey: 'true'}));
    expect(
      tester.getSemantics(find.byKey(const Key('me-open-3d'))),
      isSemantics(hasToggledState: true, isToggled: true),
    );
  });

  testWidgets('stage music edits the stage prefs, keeping the rest', (
    tester,
  ) async {
    final prefs = MemoryLocalPrefs({
      kStagePrefsKey: jsonEncode({'mode': '3d', 'music': true}),
    });
    await _pump(tester, prefs: prefs);
    await tester.tap(find.byKey(const Key('me-stage-music')));
    await tester.pumpAndSettle();
    expect(jsonDecode(prefs.read(kStagePrefsKey)!), {
      'mode': '3d',
      'music': false,
    });
  });

  testWidgets('sign out asks, then calls onLogout', (tester) async {
    var out = 0;
    await _pump(tester, onLogout: () => out++);
    await tester.tap(find.byKey(const Key('me-signout')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('CANCEL'));
    await tester.pumpAndSettle();
    expect(out, 0);

    await tester.tap(find.byKey(const Key('me-signout')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('me-signout-confirm')));
    await tester.pumpAndSettle();
    expect(out, 1);
  });

  testWidgets('dossier, timeline and subscription are reachable', (
    tester,
  ) async {
    await _pump(tester);
    expect(find.text('1 milestone together'), findsOneWidget);
    await tester.tap(find.text('CLASSIFIED PROFILE'));
    await tester.pumpAndSettle();
    expect(find.textContaining('Formerly designated HK416'), findsOneWidget);
    await tester.tap(find.text('SQUAD ROSTER · H.I.D.E. 404'));
    await tester.pumpAndSettle();
    expect(find.text('MECHTY (G11)'), findsOneWidget);

    await tester.tap(find.byKey(const Key('me-timeline')));
    await tester.pumpAndSettle();
    expect(find.byType(HudMeScreen), findsNothing); // pushed over it
  });

  test('stage music helpers default on and survive junk', () {
    expect(readStageMusic(MemoryLocalPrefs()), isTrue);
    expect(
      readStageMusic(MemoryLocalPrefs({kStagePrefsKey: 'not json'})),
      isTrue,
    );
    final p = MemoryLocalPrefs();
    writeStageMusic(p, false);
    expect(readStageMusic(p), isFalse);
  });

  test('BondStats parses the stats payload null-safely', () {
    final s = BondStats.fromJson({'user_id': 7, 'memories': 'x'});
    expect(s.userId, '');
    expect(s.memoriesKept, isNull);
    expect(VoiceLanguageCode.parse('ja'), VoiceLanguage.ja);
  });

  for (final width in [360.0, 430.0]) {
    testWidgets('no overflow at $width px', (tester) async {
      await _pump(tester, width: width);
      await tester.tap(find.text('CLASSIFIED PROFILE'));
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
    });
  }
}
