/// The HUD drawn over her 3D stage: status tags on the right, the tracking
/// readout on the left, and the dialogue panel with her latest line.
/// Stateless and VM-testable; the chat screen feeds them its state.
library;

import 'package:flutter/material.dart';

import '../theme/hud.dart';
import 'message_footer.dart';

/// Wraps the one tappable tag so it can catch its tap over the 3D stage
/// (the chat screen passes HudPointerShield, which only builds in a browser).
typedef StageTagShield = Widget Function(Widget tag);

/// AFFINITY / MOOD / HEART / WEARING, stacked on the right edge.
///
/// Over the stage only WEARING is a control (with [onTap], it opens her
/// day); it alone is shielded, on its own box. The other tags are read-only
/// and every pointer on them goes through to her, so she can be dragged and
/// pinched there. (The browser snaps a touch to a shield a few pixels away,
/// so separately shielded tags would still wall off the whole column.)
class StageStatusTags extends StatelessWidget {
  const StageStatusTags({
    super.key,
    required this.level,
    required this.mood,
    required this.bpm,
    this.wearing = '',
    this.spiking = false,
    this.onTap,
    this.shield,
  });

  final int level;
  final String mood;
  final int bpm;
  final String wearing;
  final bool spiking;

  /// A tap on the WEARING tag (the chat screen opens her day).
  final VoidCallback? onTap;

  /// Wraps the WEARING tag's box when [onTap] is set.
  final StageTagShield? shield;

  @override
  Widget build(BuildContext context) => ConstrainedBox(
    constraints: const BoxConstraints(maxWidth: 240),
    child: Column(
      key: const Key('stage-tags'),
      crossAxisAlignment: CrossAxisAlignment.end,
      mainAxisSize: MainAxisSize.min,
      children: [
        _Tag(label: 'AFFINITY', value: 'LV$level'),
        _Tag(label: 'MOOD', value: mood),
        _Tag(
          label: 'HEART',
          value: '$bpm bpm',
          valueColor: spiking ? HudColors.danger : HudColors.text,
        ),
        if (wearing.isNotEmpty)
          _Tag(label: 'WEARING', value: wearing, onTap: onTap, shield: shield),
      ],
    ),
  );
}

class _Tag extends StatelessWidget {
  const _Tag({
    required this.label,
    required this.value,
    this.valueColor = HudColors.text,
    this.onTap,
    this.shield,
  });
  final String label;
  final String value;
  final Color valueColor;
  final VoidCallback? onTap;
  final StageTagShield? shield;

  @override
  Widget build(BuildContext context) {
    Widget box = Semantics(
      label: '$label $value',
      button: onTap != null,
      excludeSemantics: true,
      child: Container(
        key: Key('stage-tag-${label.toLowerCase()}'),
        padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 5),
        decoration: BoxDecoration(
          color: HudColors.ground.withValues(alpha: 0.6),
          border: Border.all(color: HudColors.cyanA(0.35)),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(label, style: HudText.label(size: 10)),
            const SizedBox(width: 6),
            Flexible(
              child: Text(
                value,
                maxLines: 1,
                softWrap: false,
                overflow: TextOverflow.ellipsis,
                style: HudText.body(
                  size: 13,
                  weight: FontWeight.w600,
                  color: valueColor,
                ),
              ),
            ),
          ],
        ),
      ),
    );
    final tap = onTap;
    box = tap == null
        ? IgnorePointer(child: box)
        : GestureDetector(
            behavior: HitTestBehavior.opaque,
            onTap: tap,
            child: shield?.call(box) ?? box,
          );
    // The gap under the box stays unshielded.
    return Padding(padding: const EdgeInsets.only(bottom: 6), child: box);
  }
}

/// `◢ TRACKING` lines: what the stage is actually doing. Decorative to
/// pointers (taps go through to her), read by screen readers.
class TrackingReadout extends StatelessWidget {
  const TrackingReadout({super.key, required this.lines});
  final List<String> lines;

  @override
  Widget build(BuildContext context) => IgnorePointer(
    child: Semantics(
      liveRegion: true,
      label: lines.join(', '),
      excludeSemantics: true,
      child: Column(
        key: const Key('stage-tracking'),
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          for (final line in lines)
            Padding(
              padding: const EdgeInsets.only(bottom: 4),
              child: Text(
                '◢ $line',
                style: HudText.label(size: 9.5, color: HudColors.cyanA(0.8)),
              ),
            ),
        ],
      ),
    ),
  );
}

/// Her latest line over the stage, with `▶ VOICE` (spoken by the avatar,
/// lip-synced, only on a tap). While she is still thinking it shows her
/// status instead.
class DialoguePanel extends StatelessWidget {
  const DialoguePanel({
    super.key,
    required this.line,
    required this.voiceState,
    required this.onVoice,
    this.status,
    this.streaming = false,
    this.maxTextHeight = 120,
  });

  /// Her latest finished (or streaming) line; empty before she has spoken.
  final String line;

  /// A thinking/warm-up status to show while she has no reply yet.
  final String? status;
  final bool streaming;
  final VoicePlayState voiceState;
  final VoidCallback? onVoice;
  final double maxTextHeight;

  static const _shape = CutCornerBorder(
    cut: 14,
    cutTopLeft: false,
    cutTopRight: true,
    cutBottomRight: false,
    cutBottomLeft: true,
    side: BorderSide(color: Color(0x734FD1FF)),
  );

  @override
  Widget build(BuildContext context) {
    final waiting = status != null && !streaming;
    final canSpeak = line.trim().isNotEmpty && !streaming && !waiting;
    return DecoratedBox(
      key: const Key('stage-dialogue'),
      decoration: ShapeDecoration(
        color: HudColors.ground.withValues(alpha: 0.78),
        shape: _shape,
      ),
      child: Padding(
        padding: const EdgeInsets.fromLTRB(14, 0, 6, 12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          mainAxisSize: MainAxisSize.min,
          children: [
            SizedBox(
              height: 44,
              child: Row(
                children: [
                  Text(
                    '▸ KLUKAI',
                    style: HudText.label(size: 10, color: HudColors.cyan),
                  ),
                  const Spacer(),
                  if (canSpeak)
                    HudVoiceButton(state: voiceState, onTap: onVoice),
                ],
              ),
            ),
            Padding(
              padding: const EdgeInsets.only(right: 8),
              child: waiting ? _waiting() : _line(),
            ),
          ],
        ),
      ),
    );
  }

  Widget _line() {
    final text = line.trim();
    return ConstrainedBox(
      constraints: BoxConstraints(maxHeight: maxTextHeight),
      child: SingleChildScrollView(
        reverse: streaming,
        child: Text(
          text.isEmpty ? 'Awaiting orders, Commander.' : text,
          key: const Key('stage-dialogue-line'),
          style: text.isEmpty
              ? HudText.body(size: 16, color: HudColors.textMuted)
              : HudText.body(size: 17),
        ),
      ),
    );
  }

  Widget _waiting() => Column(
    crossAxisAlignment: CrossAxisAlignment.stretch,
    mainAxisSize: MainAxisSize.min,
    children: [
      Text(
        status!.toUpperCase(),
        maxLines: 2,
        overflow: TextOverflow.ellipsis,
        style: HudText.label(size: 10.5, color: HudColors.cyanA(0.8)),
      ),
      const SizedBox(height: 8),
      LinearProgressIndicator(
        minHeight: 2,
        backgroundColor: HudColors.cyanA(0.12),
        valueColor: AlwaysStoppedAnimation<Color>(HudColors.cyanA(0.7)),
      ),
    ],
  );
}
