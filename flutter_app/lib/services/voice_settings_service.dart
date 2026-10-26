import 'dart:convert';
import 'package:http/http.dart' as http;
import 'session_auth.dart';

/// Her voice language: `en` speaks her reply as written, `ja` speaks it in
/// Japanese (core translates it into her polite register first). Audio only
/// ever plays when he taps; this only chooses the language.
enum VoiceLanguage { en, ja }

extension VoiceLanguageCode on VoiceLanguage {
  String get code => name;
  String get label => switch (this) {
    VoiceLanguage.en => 'EN',
    VoiceLanguage.ja => 'JP',
  };
  static VoiceLanguage parse(Object? value) =>
      value == 'ja' ? VoiceLanguage.ja : VoiceLanguage.en;
}

/// GET/PUT /api/voice/settings with bearer auth from `klukai_token`.
/// Shared by the chat composer's EN/JP switch and the Me screen.
class VoiceSettingsService {
  VoiceSettingsService({required this.serverUrl, http.Client? client})
    : _client = client ?? http.Client();

  final String serverUrl;
  final http.Client _client;

  Map<String, String> get _headers => {
    'Authorization': 'Bearer ${readAuthToken()}',
    'Content-Type': 'application/json',
  };

  /// The saved language; English when the server has no setting (or no
  /// voice settings endpoint yet). Throws on 401 so callers can re-login.
  Future<VoiceLanguage> load() async {
    final response = await _client
        .get(Uri.parse('$serverUrl/api/voice/settings'), headers: _headers)
        .timeout(const Duration(seconds: 10));
    if (response.statusCode == 401) throw const VoiceSettingsAuthExpired();
    if (response.statusCode != 200) return VoiceLanguage.en;
    final body = jsonDecode(response.body);
    return VoiceLanguageCode.parse(body is Map ? body['language'] : null);
  }

  /// Missing or unreachable capability endpoints keep the microphone hidden.
  Future<bool> inputEnabled() async {
    final response = await _client
        .get(Uri.parse('$serverUrl/api/voice/capabilities'), headers: _headers)
        .timeout(const Duration(seconds: 10));
    if (response.statusCode == 401) throw const VoiceSettingsAuthExpired();
    if (response.statusCode != 200) return false;
    final body = jsonDecode(response.body);
    return body is Map && body['input_enabled'] == true;
  }

  /// Saves the language; returns what the server stored.
  Future<VoiceLanguage> save(VoiceLanguage language) async {
    final response = await _client
        .put(
          Uri.parse('$serverUrl/api/voice/settings'),
          headers: _headers,
          body: jsonEncode({'language': language.code}),
        )
        .timeout(const Duration(seconds: 10));
    if (response.statusCode == 401) throw const VoiceSettingsAuthExpired();
    if (response.statusCode != 200) {
      throw VoiceSettingsError(response.statusCode);
    }
    final body = jsonDecode(response.body);
    return VoiceLanguageCode.parse(
      body is Map ? body['language'] : language.code,
    );
  }
}

class VoiceSettingsAuthExpired implements Exception {
  const VoiceSettingsAuthExpired();
}

class VoiceSettingsError implements Exception {
  const VoiceSettingsError(this.statusCode);
  final int statusCode;
  @override
  String toString() => 'VoiceSettingsError: HTTP $statusCode';
}

/// localStorage / shared_preferences key: open the Chat tab in 3D by default.
/// Written by the Me screen, read by the chat view.
const kOpenIn3dPrefKey = 'klukai_hud_open_3d';
