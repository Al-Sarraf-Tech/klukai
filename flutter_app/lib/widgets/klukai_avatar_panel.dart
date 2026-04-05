import 'package:flutter/material.dart';
import '../main.dart';
import 'klukai_avatar.dart';
import 'speech_bubble.dart';

class KlukaiAvatarPanel extends StatelessWidget {
  final KlukaiAvatarController controller;
  final String modelUrl;
  final String speechText;
  final bool isSpeechStreaming;
  final bool audioEnabled;
  final Color moodGlowColor;
  final VoidCallback onTap;
  final VoidCallback onAudioToggle;
  final VoidCallback onSpeechDismiss;

  const KlukaiAvatarPanel({
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
  Widget build(BuildContext context) {
    return Container(
      decoration: BoxDecoration(
        gradient: LinearGradient(
          begin: Alignment.topCenter,
          end: Alignment.bottomCenter,
          colors: [
            const Color(0xFF0A0D14),
            GFL2Colors.background,
            const Color(0xFF141822),
          ],
        ),
        border: Border(
          right: BorderSide(
            color: GFL2Colors.border.withValues(alpha: 0.4),
          ),
        ),
      ),
      child: Stack(
        children: [
          // Mood glow background
          Positioned.fill(
            child: Center(
              child: AnimatedContainer(
                duration: const Duration(milliseconds: 600),
                width: 200,
                height: 200,
                decoration: BoxDecoration(
                  shape: BoxShape.circle,
                  boxShadow: [
                    BoxShadow(
                      color: moodGlowColor.withValues(alpha: 0.15),
                      blurRadius: 80,
                      spreadRadius: 20,
                    ),
                  ],
                ),
              ),
            ),
          ),
          // 3D model
          Positioned.fill(
            child: Padding(
              padding: const EdgeInsets.only(bottom: 100),
              child: KlukaiAvatar(
                modelUrl: modelUrl,
                controller: controller,
                onTap: onTap,
              ),
            ),
          ),
          // Speech bubble
          Positioned(
            left: 12,
            right: 12,
            bottom: 60,
            child: SpeechBubble(
              text: speechText,
              isStreaming: isSpeechStreaming,
              onDismiss: onSpeechDismiss,
            ),
          ),
          // Audio toggle
          Positioned(
            top: 8,
            right: 8,
            child: IconButton(
              onPressed: onAudioToggle,
              icon: Icon(
                audioEnabled ? Icons.volume_up : Icons.volume_off,
                color: audioEnabled
                    ? GFL2Colors.primary
                    : GFL2Colors.textDim.withValues(alpha: 0.4),
                size: 20,
              ),
              style: IconButton.styleFrom(
                backgroundColor: GFL2Colors.surface.withValues(alpha: 0.6),
                fixedSize: const Size(32, 32),
              ),
            ),
          ),
          // Tap hint
          Positioned(
            bottom: 16,
            left: 0,
            right: 0,
            child: Center(
              child: Text(
                'TAP TO INTERACT',
                style: TextStyle(
                  color: GFL2Colors.textDim.withValues(alpha: 0.3),
                  fontSize: 9,
                  letterSpacing: 2,
                  fontFamily: 'monospace',
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}
