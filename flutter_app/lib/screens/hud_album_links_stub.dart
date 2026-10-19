import 'package:flutter/widgets.dart';

import '../models/memory.dart';
import 'hud_album_links_api.dart';

/// VM fallback: those screens need a browser; nothing opens.
class _InertAlbumLinks implements AlbumLinks {
  const _InertAlbumLinks();

  @override
  Future<void> openDetail(
    BuildContext context, {
    required Memory memory,
    required String serverUrl,
    required void Function(String id) onDiscarded,
  }) async {}

  @override
  Future<void> openHerPov(
    BuildContext context, {
    required String serverUrl,
    required int affectionLevel,
    required String affectionLevelName,
    VoidCallback? onAuthExpired,
  }) async {}

  @override
  Future<void> openThread(
    BuildContext context, {
    required String serverUrl,
    VoidCallback? onAuthExpired,
  }) async {}
}

AlbumLinks defaultAlbumLinks() => const _InertAlbumLinks();
