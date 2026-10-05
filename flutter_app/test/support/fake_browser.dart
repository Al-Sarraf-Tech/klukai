// A scriptable BrowserPlatform for VM tests.
import 'dart:async';

import 'package:flutter/painting.dart';

import 'package:companion_app/platform/browser.dart';

class FakeBrowserPlatform implements BrowserPlatform {
  FakeBrowserPlatform({
    this.insets = EdgeInsets.zero,
    this.isIos = false,
    this.isStandalone = false,
    this.voiceResult = true,
  });

  EdgeInsets insets;
  @override
  bool isIos;
  @override
  bool isStandalone;
  bool voiceResult;

  final resume = StreamController<void>.broadcast();
  final List<(String, bool)> opened = [];
  final List<String> voices = [];

  @override
  EdgeInsets safeAreaInsets() => insets;

  @override
  Stream<void> get onResume => resume.stream;

  @override
  void openUrl(String url, {required bool newWindow}) => opened.add((url, newWindow));

  @override
  Future<bool> playVoice(String url) async {
    voices.add(url);
    return voiceResult;
  }
}
