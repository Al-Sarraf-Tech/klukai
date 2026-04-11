class Memory {
  final String id;
  final String? annotation;
  final List<String> sceneTags;
  final String? mood;
  final int? affectionLevel;
  final String keptBy;
  final String category;
  final DateTime createdAt;

  Memory({
    required this.id,
    this.annotation,
    this.sceneTags = const [],
    this.mood,
    this.affectionLevel,
    this.keptBy = 'klukai',
    this.category = 'Mission Records',
    DateTime? createdAt,
  }) : createdAt = createdAt ?? DateTime.now();

  factory Memory.fromJson(Map<String, dynamic> json) {
    return Memory(
      id: json['id'] ?? '',
      annotation: json['annotation'],
      sceneTags: (json['scene_tags'] as List?)?.cast<String>() ?? [],
      mood: json['mood'],
      affectionLevel: json['affection_level'],
      keptBy: json['kept_by'] ?? 'klukai',
      category: json['category'] ?? 'Mission Records',
      createdAt: json['created_at'] != null
          ? DateTime.parse(json['created_at'])
          : DateTime.now(),
    );
  }
}

class MemoryCategory {
  final String name;
  final int count;

  MemoryCategory({required this.name, required this.count});

  factory MemoryCategory.fromJson(Map<String, dynamic> json) {
    return MemoryCategory(
      name: json['name'] ?? '',
      count: json['count'] ?? 0,
    );
  }
}

class MonthGroup {
  final String month; // YYYY-MM format
  final int count;

  MonthGroup({required this.month, required this.count});

  factory MonthGroup.fromJson(Map<String, dynamic> json) {
    return MonthGroup(
      month: json['month'] ?? '',
      count: json['count'] ?? 0,
    );
  }

  /// Display label like "APR 2026"
  String get label {
    if (month.length < 7) return month;
    final parts = month.split('-');
    const months = ['', 'JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN',
                     'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];
    final m = int.tryParse(parts[1]) ?? 0;
    return '${m > 0 && m <= 12 ? months[m] : parts[1]} ${parts[0]}';
  }
}
