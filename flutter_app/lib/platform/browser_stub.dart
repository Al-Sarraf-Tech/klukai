import 'package:flutter/painting.dart';

import 'browser_api.dart';

/// Inert implementation for the Dart VM.
class StubBrowserPlatform implements BrowserPlatform {
  const StubBrowserPlatform();

  @override
  EdgeInsets safeAreaInsets() => EdgeInsets.zero;

  @override
  Stream<void> get onResume => const Stream<void>.empty();

  @override
  bool get isIos => false;

  @override
  bool get isStandalone => false;

  @override
  void openUrl(String url, {required bool newWindow}) {}

  @override
  Future<bool> playVoice(String url) async => false;
}

BrowserPlatform? _instance;

/// The process-wide platform (one set of DOM listeners).
BrowserPlatform defaultBrowserPlatform() => _instance ??= const StubBrowserPlatform();
