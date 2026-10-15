import 'package:flutter_test/flutter_test.dart';
import 'package:companion_app/models/memory.dart';
import 'package:companion_app/services/gallery_board.dart';

Memory _memory(String id, {List<String> tags = const ['her_pov'], String category = 'Mission Records'}) {
  return Memory(id: id, annotation: id, sceneTags: tags, category: category);
}

class _Expired implements Exception {
  bool get isAuthExpired => true;
}

void main() {
  test('an empty archive is not an error', () async {
    var fetches = 0;
    final board = await GalleryBoard.load(
      previous: const GalleryBoard(),
      fetch: () async {
        fetches += 1;
        return const [];
      },
    );
    expect(fetches, 1);
    expect(board.phase, GalleryPhase.empty);
    expect(board.items, isEmpty);
    expect(board.hasGood, isFalse);
  });

  test('a failed refresh keeps the last good pictures and does not render', () async {
    final kept = GalleryBoard(
      phase: GalleryPhase.ready,
      items: [_memory('kept')],
      hasGood: true,
    );
    var fetches = 0;
    final failed = await GalleryBoard.load(
      previous: kept,
      fetch: () async {
        fetches += 1;
        throw StateError('archive down');
      },
    );
    expect(fetches, 1);
    expect(failed.phase, GalleryPhase.error);
    expect(failed.items.single.id, 'kept');
    expect(failed.hasGood, isTrue);

    final again = await GalleryBoard.load(
      previous: failed,
      fetch: () async => [_memory('kept')],
    );
    expect(again.phase, GalleryPhase.ready);
    expect(again.items.single.id, 'kept');
  });

  test('authentication expiry is not turned into an empty gallery', () async {
    expect(
      () => GalleryBoard.load(
        previous: const GalleryBoard(),
        fetch: () async => throw _Expired(),
      ),
      throwsA(isA<_Expired>()),
    );
  });
}
