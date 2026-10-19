import 'package:flutter/widgets.dart';

import '../models/memory.dart';

/// Navigation out of the HUD album. Each future completes when the opened
/// screen or dialog closes.
abstract class AlbumLinks {
  /// The existing memory detail (authenticated full image, DOWNLOAD,
  /// DISCARD). [onDiscarded] gets the id once the server hid it.
  Future<void> openDetail(
    BuildContext context, {
    required Memory memory,
    required String serverUrl,
    required void Function(String id) onDiscarded,
  });

  /// Her POV: she picks a moment and draws it.
  Future<void> openHerPov(
    BuildContext context, {
    required String serverUrl,
    required int affectionLevel,
    required String affectionLevelName,
    VoidCallback? onAuthExpired,
  });

  /// The Thread: her letters and replies over time.
  Future<void> openThread(
    BuildContext context, {
    required String serverUrl,
    VoidCallback? onAuthExpired,
  });
}
