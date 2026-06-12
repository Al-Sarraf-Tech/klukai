import 'dart:typed_data';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import '../main.dart';
import '../models/memory.dart';

class MemoryTimelineEntry extends StatefulWidget {
  final Memory memory;
  final String serverUrl;
  final String authToken;
  final bool isCompact;
  final VoidCallback? onTap;

  const MemoryTimelineEntry({
    super.key,
    required this.memory,
    required this.serverUrl,
    required this.authToken,
    this.isCompact = false,
    this.onTap,
  });

  @override
  State<MemoryTimelineEntry> createState() => _MemoryTimelineEntryState();
}

class _MemoryTimelineEntryState extends State<MemoryTimelineEntry> {
  Uint8List? _thumbBytes;
  bool _thumbFailed = false;

  String get _thumbnailUrl =>
      '${widget.serverUrl}/api/memories/${widget.memory.id}/thumbnail';

  Color get _dotColor =>
      widget.memory.keptBy == 'commander' ? GFL2Colors.primary : GFL2Colors.affinity;

  String get _timestamp {
    final d = widget.memory.createdAt;
    final month = _monthAbbr(d.month);
    final hour = d.hour.toString().padLeft(2, '0');
    final min = d.minute.toString().padLeft(2, '0');
    return '$month ${d.day} // $hour:$min';
  }

  String _monthAbbr(int m) {
    const months = [
      'JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN',
      'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'
    ];
    return months[(m - 1).clamp(0, 11)];
  }

  @override
  void initState() {
    super.initState();
    _loadThumbnail();
  }

  @override
  void didUpdateWidget(MemoryTimelineEntry oldWidget) {
    super.didUpdateWidget(oldWidget);
    // The framework may reuse this State for a different memory (e.g. on a
    // filter switch). Drop the stale thumbnail and fetch the right one.
    if (oldWidget.memory.id != widget.memory.id) {
      setState(() {
        _thumbBytes = null;
        _thumbFailed = false;
      });
      _loadThumbnail();
    }
  }

  Future<void> _loadThumbnail() async {
    final requestedId = widget.memory.id;
    try {
      final response = await http.get(
        Uri.parse(_thumbnailUrl),
        headers: {'Authorization': 'Bearer ${widget.authToken}'},
      );
      // The entry may have been recycled onto another memory while this
      // request was in flight — never paint a stale thumbnail.
      if (!mounted || widget.memory.id != requestedId) return;
      if (response.statusCode == 200) {
        setState(() => _thumbBytes = response.bodyBytes);
      } else {
        setState(() => _thumbFailed = true);
      }
    } catch (_) {
      if (mounted && widget.memory.id == requestedId) {
        setState(() => _thumbFailed = true);
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final thumbW = widget.isCompact ? 70.0 : 90.0;
    final thumbH = widget.isCompact ? 93.0 : 120.0;

    return GestureDetector(
      onTap: widget.onTap,
      child: IntrinsicHeight(
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            // Timeline gutter: dot + vertical line
            SizedBox(
              width: 24,
              child: Column(
                children: [
                  const SizedBox(height: 4),
                  Container(
                    width: 10,
                    height: 10,
                    decoration: BoxDecoration(
                      shape: BoxShape.circle,
                      color: _dotColor,
                      boxShadow: [
                        BoxShadow(
                          color: _dotColor.withValues(alpha: 0.5),
                          blurRadius: 6,
                          spreadRadius: 1,
                        ),
                      ],
                    ),
                  ),
                  Expanded(
                    child: Center(
                      child: Container(
                        width: 1,
                        color: GFL2Colors.border.withValues(alpha: 0.4),
                      ),
                    ),
                  ),
                ],
              ),
            ),
            const SizedBox(width: 8),
            // Content
            Expanded(
              child: Padding(
                padding: const EdgeInsets.only(bottom: 16),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    // Timestamp
                    Text(
                      _timestamp,
                      style: TextStyle(
                        color: GFL2Colors.primary.withValues(alpha: 0.7),
                        fontSize: 10,
                        fontFamily: 'monospace',
                        fontWeight: FontWeight.w600,
                        letterSpacing: 1.2,
                      ),
                    ),
                    const SizedBox(height: 8),
                    // Thumbnail + annotation row
                    Row(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        // Thumbnail
                        _buildThumbnail(thumbW, thumbH),
                        const SizedBox(width: 12),
                        // Annotation + tags + badge
                        Expanded(
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              if (widget.memory.annotation != null &&
                                  widget.memory.annotation!.isNotEmpty)
                                Text(
                                  '"${widget.memory.annotation}"',
                                  style: const TextStyle(
                                    color: GFL2Colors.textPrimary,
                                    fontSize: 12,
                                    fontStyle: FontStyle.italic,
                                    height: 1.4,
                                  ),
                                  maxLines: widget.isCompact ? 2 : 4,
                                  overflow: TextOverflow.ellipsis,
                                ),
                              if (widget.memory.sceneTags.isNotEmpty) ...[
                                const SizedBox(height: 6),
                                Wrap(
                                  spacing: 4,
                                  runSpacing: 4,
                                  children: widget.memory.sceneTags
                                      .take(widget.isCompact ? 3 : 5)
                                      .map(_buildTag)
                                      .toList(),
                                ),
                              ],
                              const SizedBox(height: 6),
                              _buildSavedByBadge(),
                            ],
                          ),
                        ),
                      ],
                    ),
                  ],
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildThumbnail(double w, double h) {
    Widget content;
    if (_thumbBytes != null) {
      content = Image.memory(_thumbBytes!, fit: BoxFit.cover);
    } else if (_thumbFailed) {
      content = Container(
        color: GFL2Colors.panel,
        child: Center(
          child: Icon(
            Icons.image_not_supported_outlined,
            color: GFL2Colors.textDim.withValues(alpha: 0.3),
            size: 20,
          ),
        ),
      );
    } else {
      content = Container(
        color: GFL2Colors.panel,
        child: Center(
          child: SizedBox(
            width: 16,
            height: 16,
            child: CircularProgressIndicator(
              strokeWidth: 1.5,
              color: GFL2Colors.primary.withValues(alpha: 0.4),
            ),
          ),
        ),
      );
    }

    return Container(
      width: w,
      height: h,
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(3),
        border: Border.all(
          color: GFL2Colors.border.withValues(alpha: 0.5),
        ),
        color: GFL2Colors.panel,
      ),
      child: ClipRRect(
        borderRadius: BorderRadius.circular(2),
        child: content,
      ),
    );
  }

  Widget _buildTag(String tag) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
      decoration: BoxDecoration(
        color: GFL2Colors.background,
        borderRadius: BorderRadius.circular(3),
        border: Border.all(
          color: GFL2Colors.primary.withValues(alpha: 0.25),
        ),
      ),
      child: Text(
        tag,
        style: TextStyle(
          color: GFL2Colors.primary.withValues(alpha: 0.75),
          fontSize: 9,
          fontFamily: 'monospace',
          letterSpacing: 0.6,
        ),
      ),
    );
  }

  Widget _buildSavedByBadge() {
    final isKlukai = widget.memory.keptBy != 'commander';
    final label = isKlukai ? 'SAVED BY KLUKAI' : 'SAVED BY COMMANDER';
    final color = isKlukai ? GFL2Colors.affinity : GFL2Colors.primary;

    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Container(
          width: 5,
          height: 5,
          decoration: BoxDecoration(
            shape: BoxShape.circle,
            color: color.withValues(alpha: 0.7),
          ),
        ),
        const SizedBox(width: 4),
        Text(
          label,
          style: TextStyle(
            color: color.withValues(alpha: 0.6),
            fontSize: 9,
            fontFamily: 'monospace',
            letterSpacing: 0.8,
            fontWeight: FontWeight.w600,
          ),
        ),
      ],
    );
  }
}
