import 'package:flutter/material.dart';

import '../models/memory.dart';
import '../services/session_auth.dart';
import 'her_pov_screen.dart';
import 'hud_album_links_api.dart';
import 'memory_archive_screen.dart';
import 'thread_screen.dart';

class _WebAlbumLinks implements AlbumLinks {
  const _WebAlbumLinks();

  @override
  Future<void> openDetail(
    BuildContext context, {
    required Memory memory,
    required String serverUrl,
    required void Function(String id) onDiscarded,
  }) => showMemoryDetail(
    context,
    memory: memory,
    serverUrl: serverUrl,
    authToken: readAuthToken(),
    onDiscarded: onDiscarded,
  );

  @override
  Future<void> openHerPov(
    BuildContext context, {
    required String serverUrl,
    required int affectionLevel,
    required String affectionLevelName,
    VoidCallback? onAuthExpired,
  }) => Navigator.of(context).push<void>(
    MaterialPageRoute(
      builder: (_) => HerPovScreen(
        serverUrl: serverUrl,
        affectionLevel: affectionLevel,
        affectionLevelName: affectionLevelName,
        onAuthExpired: onAuthExpired,
      ),
    ),
  );

  @override
  Future<void> openThread(
    BuildContext context, {
    required String serverUrl,
    VoidCallback? onAuthExpired,
  }) => Navigator.of(context).push<void>(
    MaterialPageRoute(
      builder: (_) =>
          ThreadScreen(serverUrl: serverUrl, onAuthExpired: onAuthExpired),
    ),
  );
}

AlbumLinks defaultAlbumLinks() => const _WebAlbumLinks();
