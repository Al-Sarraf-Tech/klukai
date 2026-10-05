// Browser seam: the Companion window policy and the VM stub. VM-runnable.
import 'package:flutter/painting.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:companion_app/platform/browser.dart';

import '../support/fake_browser.dart';

void main() {
  group('companionOpensInNewWindow', () {
    test('desktop browsers get a new window', () {
      expect(companionOpensInNewWindow(FakeBrowserPlatform()), isTrue);
    });
    test('iOS Safari tab still gets a new tab', () {
      expect(companionOpensInNewWindow(FakeBrowserPlatform(isIos: true)), isTrue);
    });
    test('iOS home-screen app navigates in place (it has no windows)', () {
      expect(
          companionOpensInNewWindow(FakeBrowserPlatform(isIos: true, isStandalone: true)),
          isFalse);
    });
    test('an installed desktop PWA still opens a window', () {
      expect(companionOpensInNewWindow(FakeBrowserPlatform(isStandalone: true)), isTrue);
    });
  });

  test('the VM stub is inert', () async {
    final b = defaultBrowserPlatform();
    expect(identical(b, defaultBrowserPlatform()), isTrue); // one instance
    expect(b.safeAreaInsets(), EdgeInsets.zero);
    expect(b.isIos, isFalse);
    expect(b.isStandalone, isFalse);
    expect(await b.playVoice('data:audio/wav;base64,'), isFalse);
    expect(await b.onResume.isEmpty, isTrue);
    b.openUrl('companion.html', newWindow: true); // no throw
  });
}
