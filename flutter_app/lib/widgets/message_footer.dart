import 'package:flutter/material.dart';

import '../theme/hud.dart';

/// Where a tap-to-play voice line is: ready, synthesizing, or speaking.
enum VoicePlayState { idle, loading, playing }

/// `▶ VOICE`: her line, spoken only when he taps. A 44px-tall button that
/// shows a spinner while the voice is synthesized and stays disabled while
/// it plays, so one tap is one playback.
class HudVoiceButton extends StatelessWidget {
  const HudVoiceButton({
    super.key,
    required this.state,
    required this.onTap,
    this.enabled = true,
  });

  final VoicePlayState state;
  final VoidCallback? onTap;
  final bool enabled;

  String get tooltip => switch (state) {
    VoicePlayState.idle => 'Replay Klukai voice',
    VoicePlayState.loading => 'Preparing Klukai voice',
    VoicePlayState.playing => 'Playing Klukai voice',
  };

  @override
  Widget build(BuildContext context) {
    final active = enabled && state == VoicePlayState.idle && onTap != null;
    final color = state == VoicePlayState.idle && !enabled
        ? HudColors.textMuted
        : HudColors.cyan;
    return Tooltip(
      message: tooltip,
      child: Semantics(
        button: true,
        enabled: active,
        label: tooltip,
        excludeSemantics: true,
        child: InkWell(
          key: const Key('voice-play'),
          onTap: active ? onTap : null,
          child: ConstrainedBox(
            constraints: const BoxConstraints(minHeight: 44, minWidth: 64),
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 8),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                mainAxisAlignment: MainAxisAlignment.end,
                children: [
                  if (state == VoicePlayState.loading)
                    const SizedBox(
                      width: 10,
                      height: 10,
                      child: CircularProgressIndicator(
                        strokeWidth: 1.5,
                        color: HudColors.cyan,
                      ),
                    )
                  else
                    Text(
                      state == VoicePlayState.playing ? '▮▮' : '▶',
                      style: HudText.label(size: 10, color: color),
                    ),
                  const SizedBox(width: 6),
                  Text(
                    state == VoicePlayState.loading ? 'SYNTH' : 'VOICE',
                    style: HudText.label(size: 10, color: color),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}

/// `HH:MM` and, on his lines, the sent/read tick. Small and quiet.
class MessageFooter extends StatelessWidget {
  const MessageFooter({
    super.key,
    required this.createdAt,
    required this.isUser,
    required this.status,
  });
  final DateTime createdAt;
  final bool isUser;
  final String status;

  static String clock(DateTime t) =>
      '${t.hour.toString().padLeft(2, '0')}:${t.minute.toString().padLeft(2, '0')}';

  @override
  Widget build(BuildContext context) => Row(
    mainAxisSize: MainAxisSize.min,
    children: [
      Text(
        clock(createdAt),
        style: HudText.label(size: 9.5, color: HudColors.textMuted),
      ),
      if (isUser) ...[
        const SizedBox(width: 4),
        Icon(
          status == 'read' ? Icons.done_all : Icons.done,
          size: 13,
          semanticLabel: status == 'read' ? 'Read' : 'Sent',
          color: HudColors.violetA(0.8),
        ),
      ],
    ],
  );
}
