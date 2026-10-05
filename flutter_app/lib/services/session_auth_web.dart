import 'package:web/web.dart' as web;

/// Reads the bearer token the login page stored. Never throws.
String readAuthToken() {
  try {
    return web.window.localStorage.getItem('klukai_token') ?? '';
  } catch (_) {
    return '';
  }
}
