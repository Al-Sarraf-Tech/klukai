import 'package:flutter/material.dart';
import '../main.dart';
import 'klukai_avatar.dart';
import 'speech_bubble.dart';

class KlukaiAvatarStrip extends StatefulWidget {
  final KlukaiAvatarController controller;
  final String modelUrl;
  final String speechText;
  final bool isSpeechStreaming;
  final bool audioEnabled;
  final Color moodGlowColor;
  final VoidCallback onTap;
  final VoidCallback onAudioToggle;
  final VoidCallback onSpeechDismiss;

  const KlukaiAvatarStrip({
    super.key,
    required this.controller,
    required this.modelUrl,
    required this.speechText,
    required this.isSpeechStreaming,
    required this.audioEnabled,
    required this.moodGlowColor,
    required this.onTap,
    required this.onAudioToggle,
    required this.onSpeechDismiss,
  });

  @override
  State<KlukaiAvatarStrip> createState() => _KlukaiAvatarStripState();
}

class _KlukaiAvatarStripState extends State<KlukaiAvatarStrip> {
  bool _collapsed = false;

  @override
  Widget build(BuildContext context) {
    if (_collapsed) {
      return GestureDetector(
        onTap: () => setState(() => _collapsed = false),
        child: AnimatedContainer(
          duration: const Duration(milliseconds: 300),
          height: 4,
          decoration: BoxDecoration(
            gradient: LinearGradient(
              colors: [
                widget.moodGlowColor.withValues(alpha: 0.6),
                widget.moodGlowColor.withValues(alpha: 0.1),
              ],
            ),
          ),
        ),
      );
    }

    return AnimatedContainer(
      duration: const Duration(milliseconds: 300),
      height: 120,
      decoration: BoxDecoration(
        gradient: LinearGradient(
          colors: [const Color(0xFF0A0D14), GFL2Colors.background],
        ),
        border: Border(
          bottom: BorderSide(
            color: GFL2Colors.border.withValues(alpha: 0.4),
          ),
        ),
      ),
      child: Row(
        children: [
          // 3D model compact view
          SizedBox(
            width: 90,
            child: Stack(
              children: [
                KlukaiAvatar(
                  modelUrl: widget.modelUrl,
                  controller: widget.controller,
                  onTap: widget.onTap,
                ),
                Positioned.fill(
                  child: IgnorePointer(
                    child: Container(
                      decoration: BoxDecoration(
                        boxShadow: [
                          BoxShadow(
                            color: widget.moodGlowColor.withValues(alpha: 0.1),
                            blurRadius: 20,
                          ),
                        ],
                      ),
                    ),
                  ),
                ),
              ],
            ),
          ),
          // Speech bubble
          Expanded(
            child: Padding(
              padding: const EdgeInsets.all(8),
              child: SpeechBubble(
                text: widget.speechText,
                isStreaming: widget.isSpeechStreaming,
                onDismiss: widget.onSpeechDismiss,
              ),
            ),
          ),
          // Controls
          Column(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              IconButton(
                onPressed: widget.onAudioToggle,
                icon: Icon(
                  widget.audioEnabled ? Icons.volume_up : Icons.volume_off,
                  color: widget.audioEnabled
                      ? GFL2Colors.primary
                      : GFL2Colors.textDim.withValues(alpha: 0.4),
                  size: 18,
                ),
                iconSize: 18,
                constraints: const BoxConstraints(minWidth: 32, minHeight: 32),
              ),
              IconButton(
                onPressed: () => setState(() => _collapsed = true),
                icon: Icon(
                  Icons.keyboard_arrow_up,
                  color: GFL2Colors.textDim.withValues(alpha: 0.4),
                  size: 18,
                ),
                iconSize: 18,
                constraints: const BoxConstraints(minWidth: 32, minHeight: 32),
              ),
            ],
          ),
        ],
      ),
    );
  }
}
