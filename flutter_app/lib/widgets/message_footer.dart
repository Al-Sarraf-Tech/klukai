import 'package:flutter/material.dart';
import '../theme/gfl2_colors.dart';

/// The same native controls follow every completed message.
class MessageFooter extends StatelessWidget {
  const MessageFooter({
    super.key,
    required this.createdAt,
    required this.isUser,
    required this.status,
    required this.loading,
    required this.playing,
    required this.onReplay,
  });
  final DateTime createdAt;
  final bool isUser, loading, playing;
  final String status;
  final VoidCallback onReplay;

  @override
  Widget build(BuildContext context) => Row(
    children: [
      if (!isUser)
        IconButton(
          tooltip: loading
              ? 'Preparing Klukai voice'
              : playing
              ? 'Playing Klukai voice'
              : 'Replay Klukai voice',
          onPressed: loading || playing ? null : onReplay,
          color: GFL2Colors.primary,
          icon: loading
              ? const SizedBox(
                  width: 16,
                  height: 16,
                  child: CircularProgressIndicator(strokeWidth: 2),
                )
              : Icon(
                  playing ? Icons.volume_up : Icons.volume_up_outlined,
                  size: 20,
                ),
        ),
      const Spacer(),
      Text(
        '${createdAt.hour.toString().padLeft(2, '0')}:${createdAt.minute.toString().padLeft(2, '0')}',
        style: const TextStyle(
          color: GFL2Colors.textMuted,
          fontSize: 12,
          fontFamily: 'monospace',
        ),
      ),
      if (isUser) ...[
        const SizedBox(width: 4),
        Icon(
          status == 'read' ? Icons.done_all : Icons.done,
          size: 16,
          semanticLabel: status == 'read' ? 'Read' : 'Sent',
          color: GFL2Colors.primary,
        ),
      ],
    ],
  );
}
