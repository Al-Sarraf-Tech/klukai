import 'package:flutter/material.dart';
import '../main.dart';
import '../models/memory.dart';

class MemoryTimelineEntry extends StatelessWidget {
  final Memory memory;
  final String serverUrl;
  final bool isCompact;
  final VoidCallback? onTap;

  const MemoryTimelineEntry({
    super.key,
    required this.memory,
    required this.serverUrl,
    this.isCompact = false,
    this.onTap,
  });

  String get _thumbnailUrl => '$serverUrl/api/memories/${memory.id}/thumbnail';

  Color get _dotColor =>
      memory.keptBy == 'commander' ? GFL2Colors.primary : GFL2Colors.affinity;

  String get _timestamp {
    final d = memory.createdAt;
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
  Widget build(BuildContext context) {
    final thumbW = isCompact ? 70.0 : 90.0;
    final thumbH = isCompact ? 93.0 : 120.0;

    return GestureDetector(
      onTap: onTap,
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
                              if (memory.annotation != null &&
                                  memory.annotation!.isNotEmpty)
                                Text(
                                  '"${memory.annotation}"',
                                  style: const TextStyle(
                                    color: GFL2Colors.textPrimary,
                                    fontSize: 12,
                                    fontStyle: FontStyle.italic,
                                    height: 1.4,
                                  ),
                                  maxLines: isCompact ? 2 : 4,
                                  overflow: TextOverflow.ellipsis,
                                ),
                              if (memory.sceneTags.isNotEmpty) ...[
                                const SizedBox(height: 6),
                                Wrap(
                                  spacing: 4,
                                  runSpacing: 4,
                                  children: memory.sceneTags
                                      .take(isCompact ? 3 : 5)
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
        child: Image.network(
          _thumbnailUrl,
          fit: BoxFit.cover,
          errorBuilder: (context2, error, stack) => Container(
            color: GFL2Colors.panel,
            child: Center(
              child: Icon(
                Icons.image_not_supported_outlined,
                color: GFL2Colors.textDim.withValues(alpha: 0.3),
                size: 20,
              ),
            ),
          ),
          loadingBuilder: (_, child, progress) {
            if (progress == null) return child;
            return Container(
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
          },
        ),
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
    final isKlukai = memory.keptBy != 'commander';
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
