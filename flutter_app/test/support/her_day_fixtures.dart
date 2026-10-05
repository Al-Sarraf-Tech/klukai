// Shared fixtures for the Today's Outfit / Her Day tests: JSON exactly as the
// API contract describes it, plus a scriptable fake WardrobeService.
import 'package:companion_app/models/her_day.dart';
import 'package:companion_app/services/wardrobe_service.dart';

Map<String, dynamic> outfitJson({
  String id = 'speed_star',
  String name = 'Speed Star',
  String source = 'auto',
  String reason = 'Off-duty plans.',
}) =>
    {
      'id': id,
      'name': name,
      'blurb': 'Silver-white rider suit',
      'category': 'rider',
      'source': source,
      'reason': reason,
      'canon': true,
      'level_ok': true,
    };

Map<String, dynamic> herDayJson({
  String label = 'Hangar · tuning the suspension',
  String? override,
  Map<String, dynamic>? outfit,
}) =>
    {
      'date': '2026-10-04',
      'outfit': outfit ?? outfitJson(),
      'status': {
        'location': 'Hangar',
        'activity': 'tuning the suspension',
        'label': label,
        'slot': 'afternoon',
        'override': override,
      },
      'schedule': [
        {
          'slot': 'late',
          'start': '0000',
          'end': '0500',
          'location': 'Command deck',
          'activity': "reports at 0200 — she wasn't sleeping anyway",
          'current': false,
        },
        {
          'slot': 'morning',
          'start': '0500',
          'end': '0800',
          'location': 'Range',
          'activity': 'drills',
          'current': false,
        },
        {
          'slot': 'afternoon',
          'start': '1200',
          'end': '1700',
          'location': 'Hangar',
          'activity': 'tuning the suspension',
          'current': true,
        },
        {
          'slot': 'evening',
          'start': '1700',
          'end': '2100',
          'location': 'Off duty',
          'activity': '',
          'current': false,
        },
      ],
      'weather': {'temp_c': 14.2, 'condition': 'clear'},
    };

Map<String, dynamic> outfitsJson({Map<String, dynamic>? today}) => {
      'outfits': [
        {
          'id': 'blazing_star',
          'name': 'Blazing Star',
          'blurb': 'Military-tactical outfit befitting an elite squad leader',
          'category': 'duty',
          'source': 'canon',
          'unlock_level': 0,
          'unlocked': true,
          'current': false,
        },
        {
          'id': 'speed_star',
          'name': 'Speed Star',
          'blurb': 'Silver-white rider suit',
          'category': 'rider',
          'source': 'canon',
          'unlock_level': 0,
          'unlocked': true,
          'current': true,
        },
        {
          'id': 'immaculate_service',
          'name': 'Immaculate Service',
          'blurb': 'A battle maid is a combat configuration',
          'category': 'formal',
          'source': 'canon',
          'unlock_level': 5,
          'unlocked': false,
          'current': false,
        },
        {
          'id': 'hangar_coveralls',
          'name': 'Hangar Coveralls',
          'blurb': 'Oil on the sleeves',
          'category': 'work',
          'source': 'original',
          'unlock_level': 1,
          'unlocked': true,
          'current': false,
        },
        {
          'id': 'his_jacket',
          'name': 'His Jacket',
          'blurb': 'It was the nearest available',
          'category': 'off_duty',
          'source': 'original',
          'unlock_level': 6,
          'unlocked': false,
          'current': false,
        },
      ],
      'today': today ?? outfitJson(),
    };

Map<String, dynamic> logJson() => {
      'history': [
        {
          'day': '2026-10-03',
          'outfit_id': 'speed_star',
          'name': 'Speed Star',
          'reason': 'Off-duty plans.',
          'requested': false,
        },
        {
          'day': '2026-10-02',
          'outfit_id': 'immaculate_service',
          'name': 'Immaculate Service',
          'reason': '',
          'requested': true,
        },
      ],
    };

/// Scriptable fake. Each queue entry is a value to return or an exception to
/// throw; the last entry repeats once the queue is down to one.
class FakeWardrobeService extends WardrobeService {
  FakeWardrobeService({
    List<Object>? outfits,
    List<Object>? herDays,
    List<Object>? logs,
    this.setCostumeError,
  })  : _outfits = outfits ?? [Wardrobe.fromJson(outfitsJson())],
        _herDays = herDays ?? [HerDay.fromJson(herDayJson())],
        _logs = logs ?? [WardrobeLogEntry.listFromJson(logJson())],
        super(serverUrl: 'http://127.0.0.1:1', token: () => 'test-token');

  final List<Object> _outfits;
  final List<Object> _herDays;
  final List<Object> _logs;
  Object? setCostumeError;

  int outfitCalls = 0;
  int herDayCalls = 0;
  int logCalls = 0;
  final List<String> costumeRequests = [];

  T _next<T>(List<Object> queue) {
    final value = queue.length > 1 ? queue.removeAt(0) : queue.first;
    if (value is! T) throw value;
    return value as T;
  }

  @override
  Future<Wardrobe> fetchOutfits() async {
    outfitCalls++;
    return _next<Wardrobe>(_outfits);
  }

  @override
  Future<HerDay> fetchHerDay() async {
    herDayCalls++;
    return _next<HerDay>(_herDays);
  }

  @override
  Future<List<WardrobeLogEntry>> fetchLog({int days = 30}) async {
    logCalls++;
    return _next<List<WardrobeLogEntry>>(_logs);
  }

  @override
  Future<String> setCostume(String id) async {
    costumeRequests.add(id);
    final error = setCostumeError;
    if (error != null) throw error;
    return id;
  }
}
