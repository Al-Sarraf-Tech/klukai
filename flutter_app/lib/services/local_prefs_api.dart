import 'dart:convert';

/// A string key/value store. Reads and writes never throw: private mode or a
/// blocked storage simply reads as "not set" and drops the write.
abstract class LocalPrefs {
  String? read(String key);
  void write(String key, String value);
}

/// In-memory [LocalPrefs], for the VM and for tests.
class MemoryLocalPrefs implements LocalPrefs {
  MemoryLocalPrefs([Map<String, String>? initial]) : values = {...?initial};

  final Map<String, String> values;

  @override
  String? read(String key) => values[key];

  @override
  void write(String key, String value) => values[key] = value;
}

/// companion.html's stage settings: one JSON object (`mode`, `appearance`,
/// `music`, `voice`), read when the stage boots.
const kStagePrefsKey = 'klukai_companion_prefs';

/// Stage music, as companion.html defaults it: on unless he turned it off.
bool readStageMusic(LocalPrefs prefs) {
  final saved = _stagePrefs(prefs);
  final music = saved['music'];
  return music is bool ? music : true;
}

/// Sets stage music without touching the stage's other settings.
void writeStageMusic(LocalPrefs prefs, bool on) {
  final saved = _stagePrefs(prefs)..['music'] = on;
  prefs.write(kStagePrefsKey, jsonEncode(saved));
}

Map<String, dynamic> _stagePrefs(LocalPrefs prefs) {
  try {
    final decoded = jsonDecode(prefs.read(kStagePrefsKey) ?? '{}');
    return decoded is Map<String, dynamic> ? {...decoded} : {};
  } catch (_) {
    return {};
  }
}
