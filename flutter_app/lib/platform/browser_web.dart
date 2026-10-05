import 'dart:async';
import 'dart:js_interop';
import 'dart:js_interop_unsafe';

import 'package:flutter/painting.dart';
import 'package:web/web.dart' as web;

import 'browser_api.dart';

@JS('klukaiVoice.play')
external JSPromise<JSBoolean> _voicePlay(String url);

@JS('klukaiVoice')
external JSObject? get _voiceBridge;

class WebBrowserPlatform implements BrowserPlatform {
  WebBrowserPlatform() {
    void emit() => _resume.add(null);
    web.document.addEventListener(
      'visibilitychange',
      ((web.Event _) {
        if (web.document.visibilityState == 'visible') emit();
      }).toJS,
    );
    web.window.addEventListener(
      'pageshow',
      ((web.Event e) {
        if ((e as web.PageTransitionEvent).persisted) emit();
      }).toJS,
    );
    web.window.addEventListener('online', ((web.Event _) => emit()).toJS);
  }

  final _resume = StreamController<void>.broadcast();
  web.HTMLElement? _probe;

  @override
  Stream<void> get onResume => _resume.stream;

  @override
  EdgeInsets safeAreaInsets() {
    try {
      final probe = _probe ??= _makeProbe();
      final style = web.window.getComputedStyle(probe);
      double px(String v) => double.tryParse(v.replaceAll('px', '').trim()) ?? 0;
      return EdgeInsets.fromLTRB(
        px(style.paddingLeft),
        px(style.paddingTop),
        px(style.paddingRight),
        px(style.paddingBottom),
      );
    } catch (_) {
      return EdgeInsets.zero;
    }
  }

  web.HTMLElement _makeProbe() {
    final el = web.document.createElement('div') as web.HTMLElement;
    el.setAttribute('aria-hidden', 'true');
    el.style.cssText = 'position:fixed;top:0;left:0;width:0;height:0;'
        'visibility:hidden;pointer-events:none;'
        'padding:env(safe-area-inset-top) env(safe-area-inset-right) '
        'env(safe-area-inset-bottom) env(safe-area-inset-left);';
    web.document.body!.appendChild(el);
    return el;
  }

  @override
  bool get isIos {
    final nav = web.window.navigator;
    final ua = nav.userAgent;
    if (RegExp(r'iPad|iPhone|iPod').hasMatch(ua)) return true;
    // iPadOS 13+ reports itself as a Mac; a Mac has no touch screen.
    return ua.contains('Macintosh') && nav.maxTouchPoints > 1;
  }

  @override
  bool get isStandalone {
    try {
      if (web.window.matchMedia('(display-mode: standalone)').matches) return true;
      // iOS Safari's own flag for home-screen launches.
      final standalone = (web.window.navigator as JSObject)['standalone'];
      return standalone.isA<JSBoolean>() && (standalone as JSBoolean).toDart;
    } catch (_) {
      return false;
    }
  }

  @override
  void openUrl(String url, {required bool newWindow}) {
    if (newWindow) {
      final opened = web.window.open(url, '_blank');
      if (opened != null) {
        try {
          opened.opener = null; // the companion page can't reach back into chat
        } catch (_) {}
        return;
      }
      // Popup blocked: same-window navigation still gets him there.
    }
    web.window.location.assign(url);
  }

  @override
  Future<bool> playVoice(String url) async {
    try {
      if (_voiceBridge != null) return (await _voicePlay(url).toDart).toDart;
      final audio = web.HTMLAudioElement()..src = url;
      final ended = audio.onEnded.first;
      await audio.play().toDart;
      await ended;
      return true;
    } catch (_) {
      return false;
    }
  }
}

BrowserPlatform? _instance;

/// The process-wide platform (one set of DOM listeners).
BrowserPlatform defaultBrowserPlatform() => _instance ??= WebBrowserPlatform();
