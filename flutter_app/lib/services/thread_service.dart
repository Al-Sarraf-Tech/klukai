import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:web/web.dart' as web;

import '../models/thread.dart';

/// Non-200 from a thread endpoint, so the screen can tell an error from a
/// sealed thread.
class ThreadServiceException implements Exception {
  final int statusCode;
  ThreadServiceException(this.statusCode);

  bool get isAuthExpired => statusCode == 401;

  @override
  String toString() => 'ThreadServiceException: HTTP $statusCode';
}

class ThreadService {
  final String serverUrl;
  final http.Client _client;

  /// [client] is injectable for tests; defaults to a real HTTP client.
  ThreadService({required this.serverUrl, http.Client? client})
      : _client = client ?? http.Client();

  Map<String, String> get _authHeaders {
    var token = '';
    try {
      token = web.window.localStorage.getItem('klukai_token') ?? '';
    } catch (_) {}
    return {
      'Authorization': 'Bearer $token',
      'Content-Type': 'application/json',
    };
  }

  Future<ThreadView> fetchThread() async {
    final response =
        await _client.get(Uri.parse('$serverUrl/api/thread'), headers: _authHeaders);
    if (response.statusCode != 200) {
      throw ThreadServiceException(response.statusCode);
    }
    return ThreadView.fromJson(jsonDecode(response.body) as Map<String, dynamic>);
  }

  /// Records read receipts; returns how many entries were newly read.
  Future<int> markRead(List<String> stamps) async {
    final response = await _client.post(
      Uri.parse('$serverUrl/api/thread/read'),
      headers: _authHeaders,
      body: jsonEncode({'stamps': stamps}),
    );
    if (response.statusCode != 200) {
      throw ThreadServiceException(response.statusCode);
    }
    final data = jsonDecode(response.body) as Map<String, dynamic>;
    return data['newly_read'] as int? ?? 0;
  }
}
