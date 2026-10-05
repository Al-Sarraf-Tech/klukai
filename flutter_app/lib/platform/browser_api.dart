import 'package:flutter/painting.dart';

/// What the PWA needs from the browser, behind one injectable interface.
abstract class BrowserPlatform {
  /// CSS `env(safe-area-inset-*)`: the notch, the status bar under
  /// `black-translucent`, and the home indicator. Zero outside iOS.
  EdgeInsets safeAreaInsets();

  /// Fires when the page comes back: tab visible again, or restored from the
  /// back/forward cache, or the network comes back online. iOS freezes a
  /// backgrounded PWA and silently drops its WebSocket.
  Stream<void> get onResume;

  /// iPhone/iPad (including iPadOS reporting as a Mac).
  bool get isIos;

  /// Launched from the home screen (no browser chrome, no tabs).
  bool get isStandalone;

  /// Open [url] (relative URLs resolve against `<base href>`). [newWindow]
  /// falls back to same-window navigation if the popup is blocked.
  void openUrl(String url, {required bool newWindow});

  /// Play a `data:` / same-origin audio URL through the gesture-unlocked
  /// voice channel (queued, never overlapping). Resolves true once the line
  /// has finished, false if the browser refused it or it failed.
  Future<bool> playVoice(String url);
}

/// How the Companion page opens: a separate window on desktop, the same
/// window inside an iOS home-screen app (which has no windows to open, and
/// where companion.html carries its own way back).
bool companionOpensInNewWindow(BrowserPlatform b) => !(b.isIos && b.isStandalone);
