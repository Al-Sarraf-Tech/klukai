import 'dart:convert';

import 'package:http/http.dart' as http;

import '../models/her_day.dart';
import 'session_auth.dart';

/// Non-200 from a wardrobe / her-day endpoint.
class WardrobeServiceException implements Exception {
  final int statusCode;
  WardrobeServiceException(this.statusCode);

  bool get isAuthExpired => statusCode == 401;

  /// 403 from POST /api/costume: she hasn't unlocked it for him yet.
  bool get isLocked => statusCode == 403;

  /// 400 from POST /api/costume: unknown, or one she won't acknowledge yet.
  bool get isUnknown => statusCode == 400;

  @override
  String toString() => 'WardrobeServiceException: HTTP $statusCode';
}

/// Today's Outfit and Her Day. Same shape as ThreadService: an injectable
/// [http.Client] and bearer auth from `klukai_token`.
class WardrobeService {
  final String serverUrl;
  final http.Client _client;
  final String Function() _token;

  /// [client] and [token] are injectable for tests; they default to a real
  /// HTTP client and the token the login page stored.
  WardrobeService({
    required this.serverUrl,
    http.Client? client,
    String Function()? token,
  })  : _client = client ?? http.Client(),
        _token = token ?? readAuthToken;

  Map<String, String> get _headers => {
        'Authorization': 'Bearer ${_token()}',
        'Content-Type': 'application/json',
      };

  Future<Map<String, dynamic>> _getJson(String pathAndQuery) async {
    final response =
        await _client.get(Uri.parse('$serverUrl$pathAndQuery'), headers: _headers);
    if (response.statusCode != 200) {
      throw WardrobeServiceException(response.statusCode);
    }
    final decoded = _decode(response);
    return decoded is Map<String, dynamic> ? decoded : const {};
  }

  /// JSON is UTF-8 (RFC 8259). Decode the bytes as such rather than trusting
  /// the content-type charset, so `Hangar · tuning…` and `0200 — …` survive a
  /// proxy that drops or mangles the header.
  static Object? _decode(http.Response response) =>
      jsonDecode(utf8.decode(response.bodyBytes, allowMalformed: true));

  Future<Wardrobe> fetchOutfits() async => Wardrobe.fromJson(await _getJson('/api/outfits'));

  Future<HerDay> fetchHerDay() async => HerDay.fromJson(await _getJson('/api/her-day'));

  Future<List<WardrobeLogEntry>> fetchLog({int days = 30}) async =>
      WardrobeLogEntry.listFromJson(await _getJson('/api/wardrobe/log?days=$days'));

  /// Ask her to wear [id] today. Returns the costume she confirmed. Throws
  /// [WardrobeServiceException] (403 locked, 400 unknown) otherwise.
  Future<String> setCostume(String id) async {
    final response = await _client.post(
      Uri.parse('$serverUrl/api/costume'),
      headers: _headers,
      body: jsonEncode({'costume': id}),
    );
    if (response.statusCode != 200) {
      throw WardrobeServiceException(response.statusCode);
    }
    final decoded = _decode(response);
    final costume = decoded is Map<String, dynamic> ? decoded['costume'] : null;
    return costume is String && costume.isNotEmpty ? costume : id;
  }
}
