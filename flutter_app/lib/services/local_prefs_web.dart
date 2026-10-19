import 'package:web/web.dart' as web;

import 'local_prefs_api.dart';

class _WebLocalPrefs implements LocalPrefs {
  const _WebLocalPrefs();

  @override
  String? read(String key) {
    try {
      return web.window.localStorage.getItem(key);
    } catch (_) {
      return null;
    }
  }

  @override
  void write(String key, String value) {
    try {
      web.window.localStorage.setItem(key, value);
    } catch (_) {}
  }
}

/// The page's localStorage.
LocalPrefs defaultLocalPrefs() => const _WebLocalPrefs();
