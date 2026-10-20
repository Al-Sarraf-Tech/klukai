@TestOn('browser')
library;

// The embedded 3D stage's side of the CHAT ⇄ 3D flip: a parked iframe is not
// drawn, takes no pointer events, and is told so on this page's origin only;
// and the HUD snapshot it is sent.
//   flutter test --platform chrome test/widgets/companion_stage_view_test.dart
import 'dart:async';
import 'dart:js_interop';
import 'dart:js_interop_unsafe';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:web/web.dart' as web;

import 'package:companion_app/widgets/companion_stage_view.dart';

void main() {
  group('companionStageSrc', () {
    test('a stage loaded behind the CHAT face starts parked', () {
      expect(
        companionStageSrc(),
        'companion.html?embed=1&mode=3d&inset_top=$kStageInsetTop'
        '&inset_bottom=$kStageInsetBottom',
      );
      expect(companionStageSrc(hidden: true), endsWith('&hidden=1'));
      expect(companionStageSrc(hidden: false), isNot(contains('hidden')));
    });
  });

  group('setStageVisible', () {
    late web.HTMLIFrameElement frame;
    late List<Object?> heard;
    late List<String> origins;
    late StreamSubscription<web.MessageEvent> sub;

    setUp(() async {
      frame = web.document.createElement('iframe') as web.HTMLIFrameElement;
      web.document.body!.append(frame);
      heard = [];
      origins = [];
      // about:blank shares this page's origin, so it hears what the stage would.
      sub = web.EventStreamProviders.messageEvent
          .forTarget(frame.contentWindow!)
          .listen((event) {
            // Cloned into the frame's realm: read it field by field.
            final data = event.data as JSObject;
            heard.add({
              for (final key in const [
                'type',
                'visible',
                'level',
                'line',
                'lineId',
                'status',
              ])
                if (data.hasProperty(key.toJS).toDart)
                  key: data.getProperty<JSAny?>(key.toJS).dartify(),
            });
            origins.add(event.origin);
          });
    });

    tearDown(() async {
      await sub.cancel();
      frame.remove();
    });

    Future<void> delivered(int count) async {
      for (var i = 0; i < 50 && heard.length < count; i++) {
        await Future<void>.delayed(const Duration(milliseconds: 10));
      }
    }

    test('parked: not drawn, no pointer events, told to stop', () async {
      setStageVisible(frame, visible: false);
      expect(frame.style.visibility, 'hidden');
      expect(frame.style.pointerEvents, 'none');
      await delivered(1);
      expect(heard, [
        {'type': kStageVisibilityMessage, 'visible': false},
      ]);
      expect(origins, [web.window.location.origin]);
    });

    test('shown again: drawn, takes his touch, told to carry on', () async {
      setStageVisible(frame, visible: false);
      setStageVisible(frame, visible: true);
      expect(frame.style.visibility, 'visible');
      expect(frame.style.pointerEvents, 'auto');
      await delivered(2);
      expect(heard, [
        {'type': kStageVisibilityMessage, 'visible': false},
        {'type': kStageVisibilityMessage, 'visible': true},
      ]);
    });

    test('the message is the one embed.js listens for', () {
      expect(kStageVisibilityMessage, 'klukai:visibility');
    });

    test('its HUD goes to the stage on this origin, as klukai:hud', () async {
      final message = stageHudMessage({
        'level': 3,
        'line': 'Report.',
        'lineId': 'm7',
        'status': null,
      });
      expect(message['type'], 'klukai:hud');
      expect(kStageHudMessage, 'klukai:hud');
      expect(postToStage(frame, message), isTrue);
      await delivered(1);
      expect(heard, [
        {
          'type': 'klukai:hud',
          'level': 3,
          'line': 'Report.',
          'lineId': 'm7',
          'status': null,
        },
      ]);
      expect(origins, [web.window.location.origin]);
    });
  });

  group('CompanionStageView', () {
    test('a parked stage is painted only until its page has loaded', () {
      // Shown: always painted.
      expect(paintStage(visible: true, loaded: false), isTrue);
      expect(paintStage(visible: true, loaded: true), isTrue);
      // Parked before load: painted, or the engine never puts it in the page.
      expect(paintStage(visible: false, loaded: false), isTrue);
      // Parked after load: left out of the scene (the page keeps running).
      expect(paintStage(visible: false, loaded: true), isFalse);
    });

    testWidgets('it starts painted, even when it starts parked', (
      tester,
    ) async {
      for (final visible in [false, true]) {
        await tester.pumpWidget(
          Directionality(
            textDirection: TextDirection.ltr,
            child: CompanionStageView(
              key: ValueKey(visible),
              src: 'about:blank',
              visible: visible,
              onLink: (_) {},
              onEvent: (_, _) {},
            ),
          ),
        );
        final stage = tester.widget<Offstage>(
          find.descendant(
            of: find.byType(CompanionStageView),
            matching: find.byType(Offstage),
          ),
        );
        expect(stage.offstage, isFalse);
        expect(
          find.descendant(
            of: find.byType(Offstage),
            matching: find.byKey(const Key('companion-stage-iframe')),
          ),
          findsOneWidget,
        );
      }
      await tester.pumpWidget(const SizedBox());
    });
  });
}
