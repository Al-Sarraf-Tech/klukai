import 'dart:convert';
import 'package:http/http.dart' as http;
import 'package:web/web.dart' as web;
import '../models/memory.dart';

/// Thrown when an archive endpoint answers with a non-200 — so the UI can
/// distinguish "you truly have no memories" from "the server errored" (which
/// previously rendered an indistinguishable fake-empty archive).
class MemoryServiceException implements Exception {
  final int statusCode;
  final String endpoint;
  MemoryServiceException(this.statusCode, this.endpoint);

  /// 401 — token expired/invalid; the UI should bounce to login.
  bool get isAuthExpired => statusCode == 401;

  @override
  String toString() => 'MemoryServiceException: HTTP $statusCode on $endpoint';
}

class MemoryService {
  final String serverUrl;
  final http.Client _client;

  /// [client] is injectable for tests; defaults to a real HTTP client.
  MemoryService({required this.serverUrl, http.Client? client})
      : _client = client ?? http.Client();

  Map<String, String> get _authHeaders {
    String token = '';
    try {
      token = web.window.localStorage.getItem('klukai_token') ?? '';
    } catch (_) {}
    return {
      'Authorization': 'Bearer $token',
      'Content-Type': 'application/json',
    };
  }

  Future<List<Memory>> fetchMemories({
    String? category,
    int limit = 20,
    String? before,
    String? month,
  }) async {
    final params = <String, String>{'limit': limit.toString()};
    if (category != null && category != 'All') params['category'] = category;
    if (before != null) params['before'] = before;
    if (month != null) params['month'] = month;

    final uri = Uri.parse('$serverUrl/api/memories').replace(queryParameters: params);
    final response = await _client.get(uri, headers: _authHeaders);
    if (response.statusCode == 200) {
      final data = jsonDecode(response.body) as List;
      return data.map((m) => Memory.fromJson(m)).toList();
    }
    // Never return [] on error: an errored archive must not masquerade as an
    // empty one — the caller decides between a retry state and a login bounce.
    throw MemoryServiceException(response.statusCode, '/api/memories');
  }

  Future<List<MonthGroup>> fetchTimeline() async {
    final uri = Uri.parse('$serverUrl/api/memories/timeline');
    final response = await _client.get(uri, headers: _authHeaders);
    if (response.statusCode == 200) {
      final data = jsonDecode(response.body) as List;
      return data.map((m) => MonthGroup.fromJson(m)).toList();
    }
    throw MemoryServiceException(response.statusCode, '/api/memories/timeline');
  }

  Future<List<MemoryCategory>> fetchCategories() async {
    final uri = Uri.parse('$serverUrl/api/memories/categories');
    final response = await _client.get(uri, headers: _authHeaders);
    if (response.statusCode == 200) {
      final data = jsonDecode(response.body) as List;
      return data.map((c) => MemoryCategory.fromJson(c)).toList();
    }
    throw MemoryServiceException(
        response.statusCode, '/api/memories/categories');
  }

  Future<bool> keepMemory(String id) async {
    final uri = Uri.parse('$serverUrl/api/memories/$id/keep');
    final response = await _client.post(uri, headers: _authHeaders);
    if (response.statusCode == 200) {
      return jsonDecode(response.body)['ok'] == true;
    }
    return false;
  }

  Future<bool> discardMemory(String id) async {
    final uri = Uri.parse('$serverUrl/api/memories/$id/discard');
    final response = await _client.post(uri, headers: _authHeaders);
    if (response.statusCode == 200) {
      return jsonDecode(response.body)['ok'] == true;
    }
    return false;
  }

  String imageUrl(String id) => '$serverUrl/api/memories/$id/image';
  String thumbnailUrl(String id) => '$serverUrl/api/memories/$id/thumbnail';
}
