import 'package:flutter/material.dart';

class MoodIndicator extends StatelessWidget {
  final String mood;

  const MoodIndicator({super.key, required this.mood});

  Color get moodColor {
    return switch (mood) {
      'happy' => const Color(0xFF4ADE80),
      'excited' => const Color(0xFFFBBF24),
      'curious' => const Color(0xFF60A5FA),
      'playful' => const Color(0xFFF472B6),
      'thoughtful' => const Color(0xFF818CF8),
      'concerned' => const Color(0xFFFB923C),
      'tired' => const Color(0xFF94A3B8),
      'annoyed' => const Color(0xFFF87171),
      _ => const Color(0xFF7C3AED), // neutral
    };
  }

  String get moodEmoji {
    return switch (mood) {
      'happy' => ':)',
      'excited' => ':D',
      'curious' => '?',
      'playful' => ';)',
      'thoughtful' => '...',
      'concerned' => ':|',
      'tired' => '-_-',
      'annoyed' => '>:|',
      _ => '',
    };
  }

  @override
  Widget build(BuildContext context) {
    return AnimatedContainer(
      duration: const Duration(milliseconds: 500),
      curve: Curves.easeInOut,
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
      decoration: BoxDecoration(
        color: moodColor.withValues(alpha: 0.15),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: moodColor.withValues(alpha: 0.3)),
      ),
      child: Text(
        mood,
        style: TextStyle(
          color: moodColor,
          fontSize: 12,
          fontWeight: FontWeight.w500,
        ),
      ),
    );
  }
}
