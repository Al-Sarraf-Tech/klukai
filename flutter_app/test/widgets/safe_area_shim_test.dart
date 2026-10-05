// SafeAreaShim: CSS safe-area insets reach Flutter's MediaQuery. VM-runnable.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:companion_app/widgets/safe_area_shim.dart';

import '../support/fake_browser.dart';

const _notch = EdgeInsets.fromLTRB(0, 47, 0, 34); // iPhone 14 portrait

void main() {
  group('mergeSafeArea', () {
    test('takes the larger of Flutter and CSS insets', () {
      const mq = MediaQueryData(viewPadding: EdgeInsets.only(top: 20, left: 5));
      final out = mergeSafeArea(mq, _notch);
      expect(out.viewPadding, const EdgeInsets.fromLTRB(5, 47, 0, 34));
      expect(out.padding, const EdgeInsets.fromLTRB(5, 47, 0, 34));
    });

    test('keyboard up: bottom padding gives way, viewPadding keeps it', () {
      const mq = MediaQueryData(viewInsets: EdgeInsets.only(bottom: 300));
      final out = mergeSafeArea(mq, _notch);
      expect(out.viewPadding.bottom, 34);
      expect(out.padding.bottom, 0);
      expect(out.padding.top, 47);
      expect(out.viewInsets.bottom, 300); // untouched: Scaffold still resizes
    });

    test('no insets anywhere is a no-op', () {
      const mq = MediaQueryData();
      expect(mergeSafeArea(mq, EdgeInsets.zero).padding, EdgeInsets.zero);
    });
  });

  testWidgets('a SafeArea under the shim clears the notch and home indicator',
      (tester) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    await tester.pumpWidget(MaterialApp(
      builder: (context, child) =>
          SafeAreaShim(platform: FakeBrowserPlatform(insets: _notch), child: child!),
      home: const Scaffold(
        body: SafeArea(
          child: Column(
            children: [
              SizedBox(key: Key('top'), height: 10),
              Spacer(),
              SizedBox(key: Key('input-bar'), height: 48),
            ],
          ),
        ),
      ),
    ));
    expect(tester.getTopLeft(find.byKey(const Key('top'))).dy, 47);
    expect(tester.getBottomLeft(find.byKey(const Key('input-bar'))).dy, 844 - 34);
  });

  testWidgets('defaults to the stub platform on the VM (zero insets)', (tester) async {
    await tester.pumpWidget(MaterialApp(
      builder: (context, child) => SafeAreaShim(child: child!),
      home: const Scaffold(body: SafeArea(child: SizedBox(key: Key('box')))),
    ));
    expect(tester.getTopLeft(find.byKey(const Key('box'))).dy, 0);
  });
}
