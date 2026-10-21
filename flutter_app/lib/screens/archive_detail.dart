part of 'memory_archive_screen.dart';

/// Authenticated full-size image retrieval, export, and confirmed discard.
// ── Full-Screen Detail Dialog ────────────────────────────────────────────────

/// Opens a memory's full-size detail: the authenticated image, DOWNLOAD and
/// a confirmed DISCARD. [onDiscarded] gets the id once the server hid it.
/// Shared by the archive screen and the HUD album tab.
Future<void> showMemoryDetail(
  BuildContext context, {
  required Memory memory,
  required String serverUrl,
  required String authToken,
  void Function(String id)? onDiscarded,
}) => showDialog<void>(
  context: context,
  barrierColor: Colors.black.withValues(alpha: 0.88),
  builder: (_) => _MemoryDetailDialog(
    memory: memory,
    serverUrl: serverUrl,
    authToken: authToken,
    onDiscarded: onDiscarded,
  ),
);

class _MemoryDetailDialog extends StatefulWidget {
  final Memory memory;
  final String serverUrl;
  final String authToken;
  final void Function(String id)? onDiscarded;

  const _MemoryDetailDialog({
    required this.memory,
    required this.serverUrl,
    required this.authToken,
    this.onDiscarded,
  });

  @override
  State<_MemoryDetailDialog> createState() => _MemoryDetailDialogState();
}

class _MemoryDetailDialogState extends State<_MemoryDetailDialog> {
  bool _downloading = false;
  bool _discarding = false;
  Uint8List? _imageBytes;
  bool _imageFailed = false;

  String get _imageUrl =>
      '${widget.serverUrl}/api/memories/${widget.memory.id}/image';

  @override
  void initState() {
    super.initState();
    _loadImage();
  }

  Future<void> _loadImage() async {
    try {
      final response = await http
          .get(
            Uri.parse(_imageUrl),
            headers: {'Authorization': 'Bearer ${widget.authToken}'},
          )
          .timeout(const Duration(seconds: 20));
      if (response.statusCode == 200 && mounted) {
        setState(() => _imageBytes = response.bodyBytes);
      } else if (mounted) {
        setState(() => _imageFailed = true);
      }
    } catch (_) {
      if (mounted) setState(() => _imageFailed = true);
    }
  }

  void _showSnack(String text) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(
          text,
          style: const TextStyle(
            fontFamily: 'monospace',
            letterSpacing: 1.0,
            fontSize: 12,
          ),
        ),
        backgroundColor: GFL2Colors.surface,
        behavior: SnackBarBehavior.floating,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(4),
          side: BorderSide(color: GFL2Colors.primary.withValues(alpha: 0.4)),
        ),
      ),
    );
  }

  Future<void> _download() async {
    setState(() => _downloading = true);
    try {
      // Reuse the already-loaded full image when present; otherwise fetch it.
      Uint8List? bytes = _imageBytes;
      if (bytes == null) {
        final response = await http
            .get(
              Uri.parse(_imageUrl),
              headers: {'Authorization': 'Bearer ${widget.authToken}'},
            )
            .timeout(const Duration(seconds: 30));
        if (response.statusCode == 200) bytes = response.bodyBytes;
      }
      if (bytes == null) {
        _showSnack('DOWNLOAD FAILED');
        return;
      }
      // Real browser download via blob + anchor (same path the chat image
      // bubble uses). The full image is served as WebP; sniff the RIFF magic
      // so the saved file gets the right extension/MIME.
      final isWebp =
          bytes.length > 12 &&
          bytes[0] == 0x52 &&
          bytes[1] == 0x49 &&
          bytes[2] == 0x46 &&
          bytes[3] == 0x46; // "RIFF"
      final ext = isWebp ? 'webp' : 'png';
      final mime = isWebp ? 'image/webp' : 'image/png';
      final idShort = widget.memory.id.substring(
        0,
        widget.memory.id.length.clamp(0, 8),
      );
      final blob = web.Blob([bytes.toJS].toJS, web.BlobPropertyBag(type: mime));
      final url = web.URL.createObjectURL(blob);
      final a = web.document.createElement('a') as web.HTMLAnchorElement
        ..href = url
        ..download = 'klukai_memory_$idShort.$ext';
      a.click();
      web.URL.revokeObjectURL(url);
      _showSnack('MEMORY DOWNLOADED');
    } catch (e) {
      debugPrint('Download failed: $e');
      _showSnack('DOWNLOAD FAILED');
    } finally {
      if (mounted) setState(() => _downloading = false);
    }
  }

  Future<void> _confirmDiscard() async {
    final ok = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        backgroundColor: GFL2Colors.surface,
        title: const Text(
          'Discard memory?',
          style: TextStyle(
            color: GFL2Colors.textPrimary,
            fontSize: 14,
            fontFamily: 'monospace',
          ),
        ),
        content: const Text(
          'It will be hidden from the album. Nothing is deleted.',
          style: TextStyle(color: GFL2Colors.textDim, fontSize: 12),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: const Text(
              'CANCEL',
              style: TextStyle(color: GFL2Colors.textDim),
            ),
          ),
          TextButton(
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text(
              'DISCARD',
              style: TextStyle(color: GFL2Colors.primary),
            ),
          ),
        ],
      ),
    );
    if (ok == true) await _discard();
  }

  Future<void> _discard() async {
    setState(() => _discarding = true);
    try {
      final r = await http
          .post(
            Uri.parse(
              '${widget.serverUrl}/api/memories/${widget.memory.id}/discard',
            ),
            headers: {'Authorization': 'Bearer ${widget.authToken}'},
          )
          .timeout(const Duration(seconds: 15));
      if (r.statusCode == 200) {
        // The server has hidden it: the album drops it even if he already
        // closed this dialog (the callback checks its own screen).
        widget.onDiscarded?.call(widget.memory.id);
        if (mounted) Navigator.of(context).pop();
      } else if (mounted) {
        _showSnack('DISCARD FAILED');
      }
    } catch (_) {
      if (mounted) _showSnack('DISCARD FAILED');
    } finally {
      if (mounted) setState(() => _discarding = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Dialog(
      backgroundColor: Colors.transparent,
      insetPadding: const EdgeInsets.all(16),
      child: GestureDetector(
        onTap: () => Navigator.of(context).pop(),
        child: Container(
          color: Colors.transparent,
          child: Center(
            child: GestureDetector(
              // Prevent tap-inside from closing
              onTap: () {},
              child: Container(
                constraints: const BoxConstraints(maxWidth: 600),
                decoration: BoxDecoration(
                  color: GFL2Colors.surface,
                  borderRadius: BorderRadius.circular(4),
                  border: Border.all(
                    color: GFL2Colors.border.withValues(alpha: 0.5),
                  ),
                ),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    // Header bar
                    Container(
                      padding: const EdgeInsets.fromLTRB(14, 10, 8, 10),
                      decoration: BoxDecoration(
                        border: Border(
                          bottom: BorderSide(
                            color: GFL2Colors.border.withValues(alpha: 0.4),
                          ),
                        ),
                      ),
                      child: Row(
                        children: [
                          Text(
                            'MEMORY // ${widget.memory.id.substring(0, widget.memory.id.length.clamp(0, 8)).toUpperCase()}',
                            style: TextStyle(
                              color: GFL2Colors.primary.withValues(alpha: 0.7),
                              fontSize: 10,
                              fontFamily: 'monospace',
                              letterSpacing: 1.2,
                              fontWeight: FontWeight.w600,
                            ),
                          ),
                          const Spacer(),
                          IconButton(
                            onPressed: _discarding ? null : _confirmDiscard,
                            tooltip: 'Discard (hide from album)',
                            icon: _discarding
                                ? const SizedBox(
                                    width: 16,
                                    height: 16,
                                    child: CircularProgressIndicator(
                                      strokeWidth: 1.5,
                                      color: GFL2Colors.textDim,
                                    ),
                                  )
                                : const Icon(
                                    Icons.delete_outline,
                                    color: GFL2Colors.textDim,
                                    size: 18,
                                  ),
                            padding: EdgeInsets.zero,
                            constraints: const BoxConstraints(
                              minWidth: 32,
                              minHeight: 32,
                            ),
                          ),
                          IconButton(
                            onPressed: () => Navigator.of(context).pop(),
                            tooltip: 'Close',
                            icon: const Icon(
                              Icons.close,
                              color: GFL2Colors.textDim,
                              size: 18,
                            ),
                            padding: EdgeInsets.zero,
                            constraints: const BoxConstraints(
                              minWidth: 32,
                              minHeight: 32,
                            ),
                          ),
                        ],
                      ),
                    ),
                    // Full image
                    Flexible(
                      child: ConstrainedBox(
                        constraints: BoxConstraints(
                          maxHeight: MediaQuery.of(context).size.height * 0.6,
                        ),
                        child: _imageBytes != null
                            ? Image.memory(_imageBytes!, fit: BoxFit.contain)
                            : _imageFailed
                            ? Container(
                                height: 200,
                                color: GFL2Colors.panel,
                                child: Center(
                                  child: Icon(
                                    Icons.broken_image_outlined,
                                    color: GFL2Colors.textDim.withValues(
                                      alpha: 0.3,
                                    ),
                                    size: 48,
                                  ),
                                ),
                              )
                            : Container(
                                height: 200,
                                color: GFL2Colors.panel,
                                child: Center(
                                  child: CircularProgressIndicator(
                                    strokeWidth: 1.5,
                                    color: GFL2Colors.primary.withValues(
                                      alpha: 0.5,
                                    ),
                                  ),
                                ),
                              ),
                      ),
                    ),
                    // Annotation
                    if (widget.memory.annotation != null &&
                        widget.memory.annotation!.isNotEmpty)
                      Padding(
                        padding: const EdgeInsets.fromLTRB(14, 12, 14, 0),
                        child: Text(
                          '"${widget.memory.annotation}"',
                          style: const TextStyle(
                            color: GFL2Colors.textPrimary,
                            fontSize: 13,
                            fontStyle: FontStyle.italic,
                            height: 1.5,
                          ),
                          textAlign: TextAlign.center,
                        ),
                      ),
                    // Download button
                    Padding(
                      padding: const EdgeInsets.fromLTRB(14, 12, 14, 14),
                      child: SizedBox(
                        width: double.infinity,
                        child: TextButton.icon(
                          onPressed: _downloading ? null : _download,
                          style: TextButton.styleFrom(
                            backgroundColor: GFL2Colors.background,
                            padding: const EdgeInsets.symmetric(vertical: 10),
                            shape: RoundedRectangleBorder(
                              borderRadius: BorderRadius.circular(4),
                              side: BorderSide(
                                color: GFL2Colors.border.withValues(alpha: 0.5),
                              ),
                            ),
                          ),
                          icon: _downloading
                              ? const SizedBox(
                                  width: 14,
                                  height: 14,
                                  child: CircularProgressIndicator(
                                    strokeWidth: 1.5,
                                    color: GFL2Colors.primary,
                                  ),
                                )
                              : const Icon(
                                  Icons.download_outlined,
                                  color: GFL2Colors.primary,
                                  size: 16,
                                ),
                          label: Text(
                            _downloading ? 'DOWNLOADING...' : 'DOWNLOAD',
                            style: const TextStyle(
                              color: GFL2Colors.primary,
                              fontSize: 11,
                              fontFamily: 'monospace',
                              letterSpacing: 1.2,
                              fontWeight: FontWeight.w600,
                            ),
                          ),
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
