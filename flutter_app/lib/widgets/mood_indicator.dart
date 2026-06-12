import 'package:flutter/material.dart';
import '../theme/mood_visuals.dart';

class MoodIndicator extends StatelessWidget {
  final String mood;

  const MoodIndicator({super.key, required this.mood});

  /// Mood color from the shared single-source-of-truth table (the indicator
  /// previously kept its own diverged copy, leaving 16 moods to fall through
  /// to grey). Unknown moods get [kDefaultMoodVisual]'s glow.
  Color get moodColor => moodVisualFor(mood).glow;

  String get statusText {
    return switch (mood) {
      // Core
      'composed'        => 'COMPOSED',
      'focused'         => 'FOCUSED',
      'prideful'        => 'PRIDEFUL',
      'exasperated'     => 'IRRITATED',
      'protective'      => 'ON GUARD',
      'quietly_pleased' => 'PLEASED',
      'competitive'     => 'COMPETITIVE',
      'tender'          => 'TENDER',
      'longing'         => 'WISTFUL',
      'battle_ready'    => 'BATTLE READY',
      // Romantic
      'flustered'       => 'FLUSTERED',
      'affectionate'    => 'AFFECTIONATE',
      'shy'             => 'SHY',
      'yearning'        => 'YEARNING',
      'devoted'         => 'DEVOTED',
      // Tactical
      'vigilant'        => 'VIGILANT',
      'calculating'     => 'CALCULATING',
      'hunting'         => 'HUNTING',
      'adrenaline'      => 'ADRENALINE',
      // Relaxed
      'content'         => 'CONTENT',
      'playful'         => 'PLAYFUL',
      'drowsy'          => 'DROWSY',
      'amused'          => 'AMUSED',
      'bored'           => 'BORED',
      // Dark
      'melancholic'     => 'MELANCHOLIC',
      'haunted'         => 'HAUNTED',
      'conflicted'      => 'CONFLICTED',
      'guilty'          => 'GUILTY',
      'determined'      => 'DETERMINED',
      // Additional
      'nostalgic'       => 'NOSTALGIC',
      'curious'         => 'CURIOUS',
      'irritated'       => 'IRRITATED',
      'defiant'         => 'DEFIANT',
      'vulnerable'      => 'VULNERABLE',
      _                 => mood.toUpperCase(),
    };
  }

  @override
  Widget build(BuildContext context) {
    return AnimatedContainer(
      duration: const Duration(milliseconds: 800),
      curve: Curves.easeInOut,
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      decoration: BoxDecoration(
        color: moodColor.withValues(alpha: 0.2),
        borderRadius: BorderRadius.circular(3),
        border: Border.all(color: moodColor.withValues(alpha: 0.6), width: 1.5),
        boxShadow: [
          BoxShadow(color: moodColor.withValues(alpha: 0.25), blurRadius: 12, spreadRadius: 2),
          BoxShadow(color: moodColor.withValues(alpha: 0.1), blurRadius: 24, spreadRadius: 4),
        ],
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          AnimatedContainer(
            duration: const Duration(milliseconds: 800),
            width: 5,
            height: 5,
            decoration: BoxDecoration(
              shape: BoxShape.circle,
              color: moodColor,
              boxShadow: [
                BoxShadow(color: moodColor.withValues(alpha: 0.5), blurRadius: 4),
              ],
            ),
          ),
          const SizedBox(width: 5),
          Text(
            statusText,
            style: TextStyle(
              color: moodColor,
              fontSize: 9,
              fontWeight: FontWeight.w700,
              letterSpacing: 1.0,
              fontFamily: 'monospace',
            ),
          ),
        ],
      ),
    );
  }
}
