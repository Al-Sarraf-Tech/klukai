import 'package:flutter/material.dart';
import '../main.dart';

class MoodIndicator extends StatelessWidget {
  final String mood;

  const MoodIndicator({super.key, required this.mood});

  Color get moodColor {
    return switch (mood) {
      // Core
      'composed'        => GFL2Colors.textDim,
      'focused'         => GFL2Colors.primary,
      'prideful'        => GFL2Colors.accent,
      'exasperated'     => const Color(0xFFF59E0B),
      'protective'      => GFL2Colors.success,
      'quietly_pleased' => const Color(0xFF6EE7B7),
      'competitive'     => GFL2Colors.danger,
      'tender'          => GFL2Colors.affinity,
      'longing'         => const Color(0xFF818CF8),
      'battle_ready'    => GFL2Colors.danger,
      // Romantic
      'flustered'       => const Color(0xFFF472B6),
      'affectionate'    => const Color(0xFFFDA4AF),
      'shy'             => const Color(0xFFF9A8D4),
      'yearning'        => const Color(0xFFC084FC),
      'devoted'         => const Color(0xFFFB7185),
      // Tactical
      'vigilant'        => const Color(0xFF22D3EE),
      'calculating'     => const Color(0xFF94A3B8),
      'hunting'         => const Color(0xFFEF4444),
      'adrenaline'      => const Color(0xFFFBBF24),
      // Relaxed
      'content'         => const Color(0xFF86EFAC),
      'playful'         => const Color(0xFFA78BFA),
      'drowsy'          => const Color(0xFF64748B),
      'amused'          => const Color(0xFF34D399),
      'bored'           => const Color(0xFF475569),
      // Dark
      'melancholic'     => const Color(0xFF6366F1),
      'haunted'         => const Color(0xFF7C3AED),
      'conflicted'      => const Color(0xFFD97706),
      'guilty'          => const Color(0xFF9CA3AF),
      'determined'      => const Color(0xFFF97316),
      // Additional
      'nostalgic'       => const Color(0xFFA5B4FC),
      'curious'         => const Color(0xFF38BDF8),
      'irritated'       => const Color(0xFFFB923C),
      'defiant'         => const Color(0xFFE11D48),
      'vulnerable'      => const Color(0xFFDDD6FE),
      _                 => GFL2Colors.textDim,
    };
  }

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
        color: moodColor.withValues(alpha: 0.1),
        borderRadius: BorderRadius.circular(2),
        border: Border.all(color: moodColor.withValues(alpha: 0.25), width: 1),
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
