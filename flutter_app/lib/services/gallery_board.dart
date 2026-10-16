import '../models/memory.dart';

/// Her POV gallery phases. A failed refresh keeps the last good pictures.
enum GalleryPhase { loading, ready, empty, error }

class GalleryBoard {
  final GalleryPhase phase;
  final List<Memory> items;
  final bool hasGood;

  const GalleryBoard({
    this.phase = GalleryPhase.loading,
    this.items = const [],
    this.hasGood = false,
  });

  GalleryBoard loading() =>
      GalleryBoard(phase: GalleryPhase.loading, items: items, hasGood: hasGood);

  static bool isHerSide(Memory memory) {
    final tags = memory.sceneTags.map((tag) => tag.toLowerCase());
    return tags.contains('her_pov') ||
        tags.contains('from_her_side') ||
        memory.category == 'Precious Memories';
  }

  /// Reads the archive once. Never starts an image render.
  /// Authentication expiry is rethrown so the screen can keep its logout path.
  static Future<GalleryBoard> load({
    required GalleryBoard previous,
    required Future<List<Memory>> Function() fetch,
  }) async {
    try {
      final all = await fetch();
      final hers = all.where(isHerSide).toList();
      final items = hers.isNotEmpty ? hers : all.take(12).toList();
      if (items.isEmpty) {
        return const GalleryBoard(phase: GalleryPhase.empty);
      }
      return GalleryBoard(
        phase: GalleryPhase.ready,
        items: items,
        hasGood: true,
      );
    } catch (error) {
      if (_authExpired(error)) rethrow;
      return GalleryBoard(
        phase: GalleryPhase.error,
        items: previous.items,
        hasGood: previous.hasGood,
      );
    }
  }

  static bool _authExpired(Object error) {
    try {
      return (error as dynamic).isAuthExpired == true;
    } on NoSuchMethodError {
      return false;
    }
  }
}
