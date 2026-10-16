import 'dart:convert';
import 'dart:typed_data';
import 'dart:ui' as ui;
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import '../theme/gfl2_colors.dart';
import 'artwork_download.dart';

/// Full composition for conversation and completed memories. Gallery thumbnails
/// deliberately use their own separate presentation.
class ArtworkView extends StatefulWidget {
  const ArtworkView({super.key, required this.base64Data})
    : url = null,
      headers = null;
  const ArtworkView.network({super.key, required this.url, this.headers})
    : base64Data = null;
  final String? base64Data, url;
  final Map<String, String>? headers;

  @override
  State<ArtworkView> createState() => _ArtworkViewState();
}

class _ArtworkViewState extends State<ArtworkView> {
  Uint8List? _bytes;
  double _ratio = 1;
  bool _failed = false;
  int _generation = 0;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void didUpdateWidget(ArtworkView oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.base64Data != widget.base64Data ||
        oldWidget.url != widget.url) {
      _load();
    }
  }

  Future<void> _load() async {
    final generation = ++_generation;
    _bytes = null;
    _failed = false;
    try {
      Uint8List bytes;
      if (widget.base64Data != null) {
        bytes = base64Decode(widget.base64Data!);
      } else {
        final response = await http
            .get(Uri.parse(widget.url!), headers: widget.headers)
            .timeout(const Duration(seconds: 20));
        if (response.statusCode != 200) throw StateError('Artwork unavailable');
        bytes = response.bodyBytes;
      }
      if (!mounted || generation != _generation) return;
      setState(() => _bytes = bytes);
      final codec = await ui.instantiateImageCodec(bytes);
      final frame = await codec.getNextFrame();
      final ratio = frame.image.width / frame.image.height;
      frame.image.dispose();
      codec.dispose();
      if (!mounted || generation != _generation) return;
      setState(() {
        _bytes = bytes;
        _ratio = ratio;
      });
    } catch (_) {
      if (mounted && generation == _generation) setState(() => _failed = true);
    }
  }

  (String, String)? get _format {
    final b = _bytes;
    if (b == null) return null;
    if (b.length >= 8 &&
        b[0] == 137 &&
        b[1] == 80 &&
        b[2] == 78 &&
        b[3] == 71) {
      return ('image/png', 'png');
    }
    if (b.length >= 3 && b[0] == 255 && b[1] == 216 && b[2] == 255) {
      return ('image/jpeg', 'jpg');
    }
    if (b.length >= 6 &&
        ascii.decode(b.sublist(0, 3), allowInvalid: true) == 'GIF') {
      return ('image/gif', 'gif');
    }
    if (b.length >= 12 &&
        ascii.decode(b.sublist(0, 4), allowInvalid: true) == 'RIFF' &&
        ascii.decode(b.sublist(8, 12), allowInvalid: true) == 'WEBP') {
      return ('image/webp', 'webp');
    }
    return null;
  }

  void _download() {
    final format = _format;
    if (format != null) downloadArtwork(_bytes!, format.$1, format.$2);
  }

  void _fullView() => showDialog<void>(
    context: context,
    builder: (_) => _ArtworkDialog(
      bytes: _bytes!,
      onDownload: _format == null ? null : _download,
    ),
  );

  @override
  Widget build(BuildContext context) {
    if (_failed) {
      return const Padding(
        padding: EdgeInsets.all(16),
        child: Text(
          'Artwork unavailable',
          style: TextStyle(color: GFL2Colors.textMuted),
        ),
      );
    }
    if (_bytes == null) {
      return const SizedBox(
        height: 160,
        child: Center(
          child: CircularProgressIndicator(semanticsLabel: 'Loading artwork'),
        ),
      );
    }
    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Flexible(
          child: LayoutBuilder(
            builder: (context, constraints) {
              final width = constraints.maxWidth;
              final height = (width / _ratio).clamp(
                0.0,
                constraints.hasBoundedHeight ? constraints.maxHeight : 480.0,
              );
              return SizedBox(
                height: height,
                child: Image.memory(
                  _bytes!,
                  fit: BoxFit.contain,
                  width: width,
                  semanticLabel: 'Klukai artwork, full composition',
                  frameBuilder: (context, child, frame, synchronous) =>
                      synchronous || frame != null
                      ? child
                      : Stack(
                          fit: StackFit.expand,
                          children: [
                            child,
                            const Center(
                              child: CircularProgressIndicator(
                                semanticsLabel: 'Loading artwork',
                              ),
                            ),
                          ],
                        ),
                  errorBuilder: (_, _, _) => const Text('Artwork unavailable'),
                ),
              );
            },
          ),
        ),
        Wrap(
          alignment: WrapAlignment.end,
          children: [
            IconButton(
              tooltip: 'View full artwork',
              onPressed: _fullView,
              icon: const Icon(Icons.open_in_full),
              color: GFL2Colors.primary,
            ),
            IconButton(
              tooltip: 'Download artwork',
              onPressed: _format == null ? null : _download,
              icon: const Icon(Icons.download),
              color: GFL2Colors.primary,
            ),
          ],
        ),
      ],
    );
  }
}

class _ArtworkDialog extends StatefulWidget {
  const _ArtworkDialog({required this.bytes, this.onDownload});
  final Uint8List bytes;
  final VoidCallback? onDownload;
  @override
  State<_ArtworkDialog> createState() => _ArtworkDialogState();
}

class _ArtworkDialogState extends State<_ArtworkDialog> {
  final _transform = TransformationController();
  double _zoom = 1;
  @override
  void dispose() {
    _transform.dispose();
    super.dispose();
  }

  void _setZoom(double zoom) {
    _zoom = zoom.clamp(1.0, 4.0);
    _transform.value = Matrix4.diagonal3Values(_zoom, _zoom, 1);
  }

  @override
  Widget build(BuildContext context) => Dialog(
    backgroundColor: GFL2Colors.background,
    insetPadding: const EdgeInsets.all(12),
    child: SizedBox(
      width: MediaQuery.sizeOf(context).width,
      height: MediaQuery.sizeOf(context).height - 48,
      child: Column(
        children: [
          Wrap(
            alignment: WrapAlignment.end,
            children: [
              IconButton(
                tooltip: 'Zoom in',
                onPressed: () => _setZoom(_zoom + .5),
                icon: const Icon(Icons.zoom_in),
                color: GFL2Colors.primary,
              ),
              IconButton(
                tooltip: 'Zoom out',
                onPressed: () => _setZoom(_zoom - .5),
                icon: const Icon(Icons.zoom_out),
                color: GFL2Colors.primary,
              ),
              IconButton(
                tooltip: 'Fit full artwork',
                onPressed: () => _setZoom(1),
                icon: const Icon(Icons.fit_screen),
                color: GFL2Colors.primary,
              ),
              IconButton(
                tooltip: 'Download artwork',
                onPressed: widget.onDownload,
                icon: const Icon(Icons.download),
                color: GFL2Colors.primary,
              ),
              IconButton(
                tooltip: 'Close full artwork',
                onPressed: () => Navigator.pop(context),
                icon: const Icon(Icons.close),
                color: GFL2Colors.primary,
              ),
            ],
          ),
          Expanded(
            child: InteractiveViewer(
              transformationController: _transform,
              minScale: 1,
              maxScale: 4,
              child: Image.memory(
                widget.bytes,
                width: double.infinity,
                height: double.infinity,
                fit: BoxFit.contain,
                semanticLabel:
                    'Full artwork. Use zoom controls to inspect details.',
              ),
            ),
          ),
        ],
      ),
    ),
  );
}
