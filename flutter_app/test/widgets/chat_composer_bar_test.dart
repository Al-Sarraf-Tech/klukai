// The HUD composer at real phone widths. VM-runnable:
//   flutter test test/widgets/chat_composer_bar_test.dart
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:companion_app/services/voice_settings_service.dart';
import 'package:companion_app/theme/hud.dart';
import 'package:companion_app/widgets/chat_composer_bar.dart';

class _Calls {
  int sends = 0;
  int pictures = 0;
  final languages = <VoiceLanguage>[];
}

Future<(_Calls, TextEditingController)> _pump(
  WidgetTester tester, {
  double width = 390,
  VoiceLanguage language = VoiceLanguage.en,
  bool disabled = false,
  bool locked = false,
  String? lockReason,
  bool saving = false,
  Widget? leading,
}) async {
  tester.view.physicalSize = Size(width, 844);
  tester.view.devicePixelRatio = 1.0;
  addTearDown(tester.view.reset);
  final calls = _Calls();
  final controller = TextEditingController();
  final focus = FocusNode();
  addTearDown(controller.dispose);
  addTearDown(focus.dispose);
  await tester.pumpWidget(
    MaterialApp(
      home: Scaffold(
        body: Align(
          alignment: Alignment.bottomCenter,
          child: ChatComposerBar(
            controller: controller,
            focusNode: focus,
            language: language,
            onLanguageChanged: saving ? null : calls.languages.add,
            onSend: () => calls.sends++,
            onPicture: () => calls.pictures++,
            disabled: disabled,
            locked: locked,
            lockReason: lockReason,
            leading: leading,
          ),
        ),
      ),
    ),
  );
  return (calls, controller);
}

Finder _segment(String label) => find.descendant(
  of: find.byKey(const Key('composer-language')),
  matching: find.text(label),
);

void main() {
  for (final width in [320.0, 360.0, 390.0, 430.0]) {
    testWidgets('no overflow at ${width.toInt()}px', (tester) async {
      await _pump(tester, width: width);
      expect(tester.takeException(), isNull);
      for (final key in ['composer-picture', 'composer-send']) {
        final size = tester.getSize(find.byKey(Key(key)));
        expect(size.width, greaterThanOrEqualTo(44), reason: key);
        expect(size.height, greaterThanOrEqualTo(44), reason: key);
      }
      expect(
        tester.getSize(find.byKey(const Key('composer-language'))).height,
        greaterThanOrEqualTo(44),
      );
    });
  }

  testWidgets('shows the mockup controls: picture, TRANSMIT, EN|JP, SEND', (
    tester,
  ) async {
    await _pump(tester);
    expect(find.byIcon(Icons.photo_camera_outlined), findsOneWidget);
    expect(find.text('> TRANSMIT…'), findsOneWidget);
    expect(_segment('EN'), findsOneWidget);
    expect(_segment('JP'), findsOneWidget);
    expect(find.text('SEND'), findsOneWidget);
    expect(find.byTooltip('Ask for a picture'), findsOneWidget);
  });

  testWidgets('SEND, Enter and the picture button call through', (
    tester,
  ) async {
    final (calls, controller) = await _pump(tester);
    await tester.tap(find.byKey(const Key('composer-send')));
    await tester.tap(find.byKey(const Key('composer-picture')));
    await tester.enterText(find.byType(TextField), 'Report');
    await tester.testTextInput.receiveAction(TextInputAction.send);
    expect(calls.sends, 2);
    expect(calls.pictures, 1);
    expect(controller.text, 'Report'); // clearing is the screen's job
  });

  testWidgets('EN | JP reports the choice; inert while saving', (tester) async {
    final (calls, _) = await _pump(tester);
    await tester.tap(_segment('JP'));
    expect(calls.languages, [VoiceLanguage.ja]);
    expect(
      tester
          .widget<HudSegmented<VoiceLanguage>>(
            find.byKey(const Key('composer-language')),
          )
          .value,
      VoiceLanguage.en,
    );

    final (saving, _) = await _pump(tester, saving: true);
    await tester.tap(_segment('JP'));
    expect(saving.languages, isEmpty);
  });

  testWidgets('disabled: picture and SEND do nothing', (tester) async {
    final (calls, _) = await _pump(tester, disabled: true);
    await tester.tap(find.byKey(const Key('composer-send')));
    await tester.tap(find.byKey(const Key('composer-picture')));
    expect(calls.sends, 0);
    expect(calls.pictures, 0);
  });

  testWidgets('locked: read-only with her reason as the hint', (tester) async {
    await _pump(
      tester,
      disabled: true,
      locked: true,
      lockReason: 'RECEIVING TRANSMISSION',
    );
    expect(tester.widget<TextField>(find.byType(TextField)).readOnly, isTrue);
    expect(find.text('// RECEIVING TRANSMISSION…'), findsOneWidget);
  });

  testWidgets('no mic unless the screen passes one in', (tester) async {
    await _pump(tester);
    expect(find.byIcon(Icons.mic), findsNothing);
    await _pump(tester, leading: const Icon(Icons.mic));
    expect(find.byIcon(Icons.mic), findsOneWidget);
    expect(tester.takeException(), isNull);
  });
}
