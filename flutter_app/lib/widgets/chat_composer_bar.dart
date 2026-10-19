import 'package:flutter/material.dart';

import '../services/voice_settings_service.dart';
import '../theme/hud.dart';

/// What the picture button sends: an ordinary chat line, in his words, that
/// her image tool recognizes ("take a picture", "show me").
const kPictureRequest = 'Take a picture for me — show me where you are.';

/// The HUD composer: ask for a picture, `> TRANSMIT…`, her voice language
/// (EN | JP) and SEND. Shared by the chat view and the 3D view.
///
/// Stateless and VM-testable. [disabled] (link down or input locked) turns
/// off the picture and send buttons; [locked] makes the field read-only and
/// shows [lockReason] as its hint. [onLanguageChanged] is null while a
/// language change is being saved.
class ChatComposerBar extends StatelessWidget {
  const ChatComposerBar({
    super.key,
    required this.controller,
    required this.focusNode,
    required this.language,
    required this.onLanguageChanged,
    required this.onSend,
    required this.onPicture,
    this.disabled = false,
    this.locked = false,
    this.lockReason,
    this.lockColor = HudColors.violet,
    this.leading,
  });

  final TextEditingController controller;
  final FocusNode focusNode;
  final VoiceLanguage language;
  final ValueChanged<VoiceLanguage>? onLanguageChanged;
  final VoidCallback onSend;
  final VoidCallback onPicture;
  final bool disabled;
  final bool locked;
  final String? lockReason;
  final Color lockColor;

  /// Extra control before the field (the push-to-talk button, when voice
  /// input is switched on).
  final Widget? leading;

  @override
  Widget build(BuildContext context) {
    return Padding(
      key: const Key('chat-composer'),
      padding: const EdgeInsets.fromLTRB(12, 6, 12, 8),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.end,
        children: [
          HudSquareButton(
            key: const Key('composer-picture'),
            tooltip: 'Ask for a picture',
            onTap: disabled ? null : onPicture,
            child: const Icon(Icons.photo_camera_outlined, size: 20),
          ),
          if (leading != null) ...[const SizedBox(width: 6), leading!],
          const SizedBox(width: 6),
          Expanded(child: _field()),
          const SizedBox(width: 6),
          HudSegmented<VoiceLanguage>(
            key: const Key('composer-language'),
            semanticLabel: 'Her voice language',
            options: {for (final l in VoiceLanguage.values) l: l.label},
            value: language,
            onChanged: onLanguageChanged,
          ),
          const SizedBox(width: 6),
          HudSquareButton(
            key: const Key('composer-send'),
            tooltip: 'Send',
            onTap: disabled ? null : onSend,
            padding: const EdgeInsets.symmetric(horizontal: 12),
            child: Text(
              'SEND',
              style: HudText.label(
                size: 11,
                color: disabled ? HudColors.textMuted : HudColors.cyan,
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _field() {
    final hint = locked ? '// ${lockReason ?? 'STANDBY'}…' : '> TRANSMIT…';
    OutlineInputBorder edge(Color color) => OutlineInputBorder(
      borderRadius: BorderRadius.zero,
      borderSide: BorderSide(color: color),
    );
    return TextField(
      key: const Key('composer-input'),
      controller: controller,
      focusNode: focusNode,
      readOnly: locked,
      maxLines: 4,
      minLines: 1,
      textInputAction: TextInputAction.send,
      onSubmitted: disabled ? null : (_) => onSend(),
      cursorColor: HudColors.cyan,
      style: HudText.body(
        size: 15,
        color: locked ? HudColors.textMuted : HudColors.text,
      ),
      decoration: InputDecoration(
        hintText: hint,
        hintMaxLines: 1,
        hintStyle: HudText.label(
          size: 12,
          color: locked
              ? lockColor.withValues(alpha: 0.7)
              : HudColors.textMuted,
        ),
        isDense: true,
        filled: true,
        fillColor: HudColors.ground.withValues(alpha: 0.85),
        contentPadding: const EdgeInsets.symmetric(
          horizontal: 12,
          vertical: 13,
        ),
        border: edge(HudColors.cyanA(0.35)),
        enabledBorder: edge(HudColors.cyanA(0.35)),
        disabledBorder: edge(HudColors.cyanA(0.2)),
        focusedBorder: edge(HudColors.cyan),
      ),
    );
  }
}

/// A 44×44 (at least) cut-corner HUD button with a cyan edge.
class HudSquareButton extends StatelessWidget {
  const HudSquareButton({
    super.key,
    required this.tooltip,
    required this.onTap,
    required this.child,
    this.padding = EdgeInsets.zero,
  });

  final String tooltip;
  final VoidCallback? onTap;
  final Widget child;
  final EdgeInsetsGeometry padding;

  @override
  Widget build(BuildContext context) {
    final enabled = onTap != null;
    final shape = CutCornerBorder(
      cut: 8,
      side: BorderSide(
        color: enabled ? HudColors.cyanA(0.5) : HudColors.cyanA(0.2),
      ),
    );
    return Tooltip(
      message: tooltip,
      child: Semantics(
        button: true,
        enabled: enabled,
        label: tooltip,
        excludeSemantics: true,
        child: Material(
          type: MaterialType.transparency,
          child: InkWell(
            onTap: onTap,
            customBorder: shape,
            child: Ink(
              decoration: ShapeDecoration(
                color: HudColors.ground.withValues(alpha: 0.85),
                shape: shape,
              ),
              child: ConstrainedBox(
                constraints: const BoxConstraints(minWidth: 44, minHeight: 44),
                child: Padding(
                  padding: padding,
                  child: Center(
                    widthFactor: 1,
                    heightFactor: 1,
                    child: IconTheme.merge(
                      data: IconThemeData(
                        color: enabled ? HudColors.cyan : HudColors.textMuted,
                      ),
                      child: child,
                    ),
                  ),
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
