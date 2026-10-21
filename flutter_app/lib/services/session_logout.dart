import 'package:http/http.dart' as http;

/// Ends this device's session on the server (POST /api/auth/logout), so the
/// token stops working there too instead of staying valid until it expires.
///
/// Best effort and bounded: signing out never waits on a dead link, and any
/// failure is swallowed (the local token is cleared either way).
Future<void> endServerSession(
  String serverUrl,
  String token, {
  http.Client? client,
  Duration timeout = const Duration(seconds: 3),
}) async {
  if (token.isEmpty) return;
  final owned = client == null;
  final c = client ?? http.Client();
  try {
    await c
        .post(
          Uri.parse('$serverUrl/api/auth/logout'),
          headers: {'Authorization': 'Bearer $token'},
        )
        .timeout(timeout);
  } catch (_) {
    // Offline or slow: the local sign-out still happens.
  } finally {
    if (owned) c.close();
  }
}
