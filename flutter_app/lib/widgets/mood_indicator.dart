import 'package:flutter/material.dart';
import '../main.dart';

class MoodIndicator extends StatelessWidget {
  final String mood;

  const MoodIndicator({super.key, required this.mood});

  Color get moodColor {
    return switch (mood) {
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
      _                 => GFL2Colors.textDim,
    };
  }

  String get statusText {
    return switch (mood) {
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
      _                 => 'COMPOSED',
    };
  }

  @override
  Widget build(BuildContext context) {
    return AnimatedContainer(
      duration: const Duration(milliseconds: 500),
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
          Container(
            width: 5,
            height: 5,
            decoration: BoxDecoration(
              shape: BoxShape.circle,
              color: moodColor,
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
