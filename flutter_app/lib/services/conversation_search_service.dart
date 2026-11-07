import 'dart:convert';
import 'package:http/http.dart' as http;
import '../models/conversation_search.dart';
import 'session_auth.dart';

class ConversationSearchException implements Exception {
  const ConversationSearchException(this.statusCode);
  final int statusCode;
  @override
  String toString() => 'ConversationSearchException: HTTP $statusCode';
}

/// Read-only POSTs: phrases and source text never enter URLs or logs.
class ConversationSearchService {
  ConversationSearchService({required this.serverUrl, http.Client? client})
    : _client = client ?? http.Client(),
      _ownsClient = client == null;
  final String serverUrl;
  final http.Client _client;
  final bool _ownsClient;
  void dispose() {
    if (_ownsClient) _client.close();
  }

  Future<Map<String, dynamic>> _post(
    String path,
    Map<String, dynamic> body,
  ) async {
    final response = await _client
        .post(
          Uri.parse('$serverUrl/api/messages/$path'),
          headers: {
            'Authorization': 'Bearer ${readAuthToken()}',
            'Content-Type': 'application/json',
          },
          body: jsonEncode(body),
        )
        .timeout(const Duration(seconds: 15));
    if (response.statusCode != 200) {
      throw ConversationSearchException(response.statusCode);
    }
    return jsonDecode(utf8.decode(response.bodyBytes)) as Map<String, dynamic>;
  }

  Future<ConversationSearchPage> search({
    required String query,
    String speaker = 'all',
    DateTime? from,
    DateTime? through,
    MessageCursor? cursor,
  }) async {
    final phrase = query.trim();
    if (phrase.runes.length < 2 ||
        phrase.runes.length > 200 ||
        phrase.contains('\u0000')) {
      throw ArgumentError('Enter 2–200 characters without NUL.');
    }
    if (!const ['all', 'user', 'assistant'].contains(speaker)) {
      throw ArgumentError('Invalid speaker.');
    }
    final start = from == null
        ? null
        : DateTime(from.year, from.month, from.day);
    // Construct the next LOCAL calendar day: a DST day need not last 24 hours.
    final end = through == null
        ? null
        : DateTime(through.year, through.month, through.day + 1);
    if (start != null && end != null && !start.isBefore(end)) {
      throw ArgumentError('From must be on or before Through.');
    }
    return ConversationSearchPage.fromJson(
      await _post('search', {
        'query': phrase,
        'speaker': speaker,
        'start': start?.toUtc().toIso8601String(),
        'end': end?.toUtc().toIso8601String(),
        'cursor': cursor?.toJson(),
      }),
    );
  }

  Future<ConversationContextPage> context({
    required String anchorId,
    MessageCursor? before,
    MessageCursor? after,
  }) async {
    if (before != null && after != null) {
      throw ArgumentError('Choose one direction.');
    }
    return ConversationContextPage.fromJson(
      await _post('context', {
        'anchor_id': anchorId,
        'before': before?.toJson(),
        'after': after?.toJson(),
      }),
    );
  }
}
