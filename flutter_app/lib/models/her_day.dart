/// Today's Outfit and Her Day: what she's wearing, where she is, and her day.
///
/// Pure Dart (no package:web), so these parse and test on the VM. Every
/// `fromJson` is null-safe: a missing or mistyped field falls back to a sane
/// default rather than throwing, because the header line must never break chat.
library;

String _str(Object? v, [String fallback = '']) => v is String ? v : fallback;
bool _bool(Object? v, [bool fallback = false]) => v is bool ? v : fallback;
int _int(Object? v, [int fallback = 0]) => v is int ? v : (v is num ? v.toInt() : fallback);
double? _double(Object? v) => v is num ? v.toDouble() : null;
Map<String, dynamic>? _map(Object? v) => v is Map<String, dynamic> ? v : null;
List<Map<String, dynamic>> _maps(Object? v) =>
    v is List ? v.whereType<Map<String, dynamic>>().toList() : const [];

/// What she is wearing today (`today` in /api/outfits, `outfit` in /api/her-day,
/// and the `outfit` WS frame).
class OutfitInfo {
  final String id;
  final String name;
  final String blurb;
  final String category;

  /// `auto` (she chose), `roster` (the block's kit), or `commander` (he asked).
  final String source;

  /// Her reason. Non-empty only when [source] is `auto`.
  final String reason;
  final bool canon;
  final bool levelOk;

  const OutfitInfo({
    required this.id,
    required this.name,
    this.blurb = '',
    this.category = '',
    this.source = 'auto',
    this.reason = '',
    this.canon = false,
    this.levelOk = true,
  });

  factory OutfitInfo.fromJson(Map<String, dynamic> json) {
    final id = _str(json['id']);
    return OutfitInfo(
      id: id,
      name: _str(json['name'], _titleCase(id)),
      blurb: _str(json['blurb']),
      category: _str(json['category']),
      source: _str(json['source'], 'auto'),
      reason: _str(json['reason']),
      canon: _bool(json['canon']),
      levelOk: _bool(json['level_ok'], true),
    );
  }

  bool get requestedByCommander => source == 'commander';

  /// The line under the outfit name on the TODAY card: her reason when she
  /// chose it, otherwise who did.
  String get sourceLine => switch (source) {
        'commander' => 'Your request.',
        'roster' => 'On duty kit.',
        _ => reason,
      };
}

/// One entry in the wardrobe (`outfits[]` in /api/outfits).
class WardrobeItem {
  final String id;
  final String name;
  final String blurb;
  final String category;

  /// `canon` (a GFL2 skin) or `original` (her everyday kit).
  final String source;
  final int unlockLevel;
  final bool unlocked;
  final bool current;

  const WardrobeItem({
    required this.id,
    required this.name,
    this.blurb = '',
    this.category = '',
    this.source = 'original',
    this.unlockLevel = 0,
    this.unlocked = false,
    this.current = false,
  });

  factory WardrobeItem.fromJson(Map<String, dynamic> json) {
    final id = _str(json['id']);
    return WardrobeItem(
      id: id,
      name: _str(json['name'], _titleCase(id)),
      blurb: _str(json['blurb']),
      category: _str(json['category']),
      source: _str(json['source'], 'original'),
      unlockLevel: _int(json['unlock_level']),
      unlocked: _bool(json['unlocked']),
      current: _bool(json['current']),
    );
  }

  bool get isCanon => source == 'canon';
}

/// GET /api/outfits.
class Wardrobe {
  final List<WardrobeItem> outfits;
  final OutfitInfo? today;

  const Wardrobe({this.outfits = const [], this.today});

  factory Wardrobe.fromJson(Map<String, dynamic> json) {
    final today = _map(json['today']);
    return Wardrobe(
      outfits: _maps(json['outfits'])
          .map(WardrobeItem.fromJson)
          .where((o) => o.id.isNotEmpty)
          .toList(),
      today: today == null ? null : OutfitInfo.fromJson(today),
    );
  }

  List<WardrobeItem> get canon => outfits.where((o) => o.isCanon).toList();
  List<WardrobeItem> get everyday => outfits.where((o) => !o.isCanon).toList();
}

/// Where she is right now (`status` in /api/her-day).
class HerDayStatus {
  final String location;
  final String activity;
  final String label;
  final String slot;

  /// null, `gaming` or `mission`.
  final String? override;

  const HerDayStatus({
    this.location = '',
    this.activity = '',
    this.label = '',
    this.slot = '',
    this.override,
  });

  factory HerDayStatus.fromJson(Map<String, dynamic> json) {
    final location = _str(json['location']);
    final activity = _str(json['activity']);
    final label = _str(json['label']);
    final override = json['override'];
    return HerDayStatus(
      location: location,
      activity: activity,
      label: label.isNotEmpty ? label : _joinLine(location, activity),
      slot: _str(json['slot']),
      override: override is String && override.isNotEmpty ? override : null,
    );
  }
}

/// One block of her day (`schedule[]` in /api/her-day).
class ScheduleBlock {
  final String slot;
  final String start;
  final String end;
  final String location;
  final String activity;
  final bool current;

  const ScheduleBlock({
    this.slot = '',
    this.start = '',
    this.end = '',
    this.location = '',
    this.activity = '',
    this.current = false,
  });

  factory ScheduleBlock.fromJson(Map<String, dynamic> json) => ScheduleBlock(
        slot: _str(json['slot']),
        start: _str(json['start']),
        end: _str(json['end']),
        location: _str(json['location']),
        activity: _str(json['activity']),
        current: _bool(json['current']),
      );

  /// `0500–0800`.
  String get timeRange => end.isEmpty ? start : '$start–$end';

  /// `Hangar · tuning the suspension`, or just the location for a private block.
  String get line => _joinLine(location, activity);
}

class HerDayWeather {
  final double? tempC;
  final String condition;

  const HerDayWeather({this.tempC, this.condition = ''});

  factory HerDayWeather.fromJson(Map<String, dynamic> json) => HerDayWeather(
        tempC: _double(json['temp_c']),
        condition: _str(json['condition']),
      );

  /// `14°C · clear`.
  String get label {
    final temp = tempC == null ? '' : '${tempC!.round()}°C';
    if (temp.isEmpty) return condition;
    return condition.isEmpty ? temp : '$temp · $condition';
  }
}

/// GET /api/her-day.
class HerDay {
  final String date;
  final OutfitInfo? outfit;
  final HerDayStatus? status;
  final List<ScheduleBlock> schedule;
  final HerDayWeather? weather;

  const HerDay({
    this.date = '',
    this.outfit,
    this.status,
    this.schedule = const [],
    this.weather,
  });

  factory HerDay.fromJson(Map<String, dynamic> json) {
    final outfit = _map(json['outfit']);
    final status = _map(json['status']);
    final weather = _map(json['weather']);
    return HerDay(
      date: _str(json['date']),
      outfit: outfit == null ? null : OutfitInfo.fromJson(outfit),
      status: status == null ? null : HerDayStatus.fromJson(status),
      schedule: _maps(json['schedule']).map(ScheduleBlock.fromJson).toList(),
      weather: weather == null ? null : HerDayWeather.fromJson(weather),
    );
  }

  /// The same day with a new outfit (the `outfit` WS frame).
  HerDay withOutfit(OutfitInfo next) => HerDay(
        date: date,
        outfit: next,
        status: status,
        schedule: schedule,
        weather: weather,
      );

  /// Whether the header has anything to show.
  bool get hasStatusLine => (status?.label.isNotEmpty ?? false) || outfit != null;
}

/// One row of GET /api/wardrobe/log.
class WardrobeLogEntry {
  final String day;
  final String outfitId;
  final String name;
  final String reason;
  final bool requested;

  const WardrobeLogEntry({
    required this.day,
    required this.outfitId,
    required this.name,
    this.reason = '',
    this.requested = false,
  });

  factory WardrobeLogEntry.fromJson(Map<String, dynamic> json) {
    final id = _str(json['outfit_id']);
    return WardrobeLogEntry(
      day: _str(json['day']),
      outfitId: id,
      name: _str(json['name'], _titleCase(id)),
      reason: _str(json['reason']),
      requested: _bool(json['requested']),
    );
  }

  static List<WardrobeLogEntry> listFromJson(Map<String, dynamic> json) =>
      _maps(json['history']).map(WardrobeLogEntry.fromJson).toList();

  /// `OCT 03` from `2026-10-03`; the raw string if it doesn't parse.
  String get shortDay {
    final parsed = DateTime.tryParse(day);
    if (parsed == null) return day;
    const months = [
      'JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN',
      'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC',
    ];
    return '${months[parsed.month - 1]} ${parsed.day.toString().padLeft(2, '0')}';
  }
}

String _joinLine(String location, String activity) {
  if (activity.isEmpty) return location;
  if (location.isEmpty) return activity;
  return '$location · $activity';
}

String _titleCase(String id) => id
    .split('_')
    .where((w) => w.isNotEmpty)
    .map((w) => w[0].toUpperCase() + w.substring(1))
    .join(' ');
