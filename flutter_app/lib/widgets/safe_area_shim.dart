import 'dart:math' as math;

import 'package:flutter/widgets.dart';

import '../platform/browser.dart';

/// Feeds the browser's safe-area insets into [MediaQuery].
///
/// Flutter web reports zero view padding, so on an iPhone home-screen app
/// (viewport-fit=cover, black-translucent status bar) the header slides under
/// the notch and the input bar under the home indicator. This reads CSS
/// `env(safe-area-inset-*)` and merges it in, so every existing [SafeArea],
/// [AppBar] and [Scaffold] does the right thing without knowing about iOS.
class SafeAreaShim extends StatelessWidget {
  const SafeAreaShim({super.key, required this.child, this.platform});

  final Widget child;

  /// Injectable for tests; defaults to the real browser.
  final BrowserPlatform? platform;

  @override
  Widget build(BuildContext context) {
    final mq = MediaQuery.of(context); // rebuilds on resize/rotation/keyboard
    final probed = (platform ?? defaultBrowserPlatform()).safeAreaInsets();
    return MediaQuery(data: mergeSafeArea(mq, probed), child: child);
  }
}

/// Native-like semantics: viewPadding is the larger of what Flutter and CSS
/// report; padding drops the bottom inset while the keyboard covers it (the
/// home indicator is behind the keyboard, so the input bar sits flush on it).
MediaQueryData mergeSafeArea(MediaQueryData mq, EdgeInsets probed) {
  final view = EdgeInsets.fromLTRB(
    math.max(mq.viewPadding.left, probed.left),
    math.max(mq.viewPadding.top, probed.top),
    math.max(mq.viewPadding.right, probed.right),
    math.max(mq.viewPadding.bottom, probed.bottom),
  );
  final padding = EdgeInsets.fromLTRB(
    view.left,
    view.top,
    view.right,
    math.max(0.0, view.bottom - mq.viewInsets.bottom),
  );
  return mq.copyWith(viewPadding: view, padding: padding);
}
