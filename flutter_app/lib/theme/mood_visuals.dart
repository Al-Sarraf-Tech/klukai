// Canonical per-mood visuals: glow color + heartbeat BPM.
//
// SINGLE SOURCE OF TRUTH for every mood-keyed visual lookup. Both the chat
// screen (portrait glow + heartbeat BPM) and the MoodIndicator chip consume
// this map, so the two can never drift apart again (a previous bug class:
// level/mood-keyed lookups silently falling through to a grey default for
// keys missing from one of two diverged tables).
//
// Unknown moods MUST fall back to [kDefaultMoodVisual] — never crash, never
// render an unstyled hole.
import 'package:flutter/material.dart';

class MoodVisual {
  final Color glow;
  final int bpm;
  const MoodVisual(this.glow, this.bpm);
}

const MoodVisual kDefaultMoodVisual = MoodVisual(Color(0xFF4FC3F7), 70);

/// Visuals for [mood], falling back to [kDefaultMoodVisual] for unknown keys.
MoodVisual moodVisualFor(String mood) =>
    kMoodVisuals[mood] ?? kDefaultMoodVisual;

const Map<String, MoodVisual> kMoodVisuals = {
  // Core — blues and cyans
  'composed': MoodVisual(Color(0xFF4FC3F7), 65), // calm cyan
  'focused': MoodVisual(Color(0xFF3B82F6), 78), // sharp blue
  'prideful': MoodVisual(Color(0xFFE8923E), 80), // proud orange
  'exasperated': MoodVisual(Color(0xFFF59E0B), 90), // frustrated amber
  'protective': MoodVisual(Color(0xFF10B981), 105), // guardian green
  'quietly_pleased': MoodVisual(Color(0xFF6EE7B7), 72), // subtle mint
  'competitive': MoodVisual(Color(0xFFFF6B35), 100), // fierce orange-red
  'tender': MoodVisual(Color(0xFFE88CA5), 75), // soft pink
  'longing': MoodVisual(Color(0xFF818CF8), 80), // wistful indigo
  'battle_ready': MoodVisual(Color(0xFFEF4444), 130), // combat red
  // Romantic — pinks, roses, magentas (each unique)
  'flustered': MoodVisual(Color(0xFFF472B6), 95), // hot pink
  'affectionate': MoodVisual(Color(0xFFFDA4AF), 78), // warm rose
  'shy': MoodVisual(Color(0xFFFFB3C6), 88), // soft blush
  'yearning': MoodVisual(Color(0xFFC084FC), 85), // aching purple
  'devoted': MoodVisual(Color(0xFFFB7185), 82), // deep rose
  'passionate': MoodVisual(Color(0xFFE11D48), 115), // burning crimson
  'jealous': MoodVisual(Color(0xFFB91C1C), 108), // dark jealous red
  'possessive': MoodVisual(Color(0xFFBE123C), 112), // possessive wine
  'smitten': MoodVisual(Color(0xFFFF80AB), 92), // lovesick pink
  'infatuated': MoodVisual(Color(0xFFEC4899), 98), // obsessive magenta
  // Tactical — teals and steel
  'vigilant': MoodVisual(Color(0xFF22D3EE), 95), // alert cyan
  'calculating': MoodVisual(Color(0xFF94A3B8), 88), // cold steel
  'hunting': MoodVisual(Color(0xFFD97706), 110), // predator amber
  'adrenaline': MoodVisual(Color(0xFFEAB308), 145), // rush gold
  // Mission stress — yellows through deep reds
  'scared': MoodVisual(Color(0xFFFACC15), 140), // fear yellow
  'terrified': MoodVisual(Color(0xFFEF4444), 165), // terror red
  'panicked': MoodVisual(Color(0xFFFF2D2D), 180), // panic bright red
  'desperate': MoodVisual(Color(0xFF991B1B), 175), // desperation dark red
  'relieved': MoodVisual(Color(0xFF5EEAD4), 62), // relief teal
  // Relaxed — greens and soft purples
  'content': MoodVisual(Color(0xFF86EFAC), 60), // peaceful green
  'playful': MoodVisual(Color(0xFFA78BFA), 76), // mischief purple
  'drowsy': MoodVisual(Color(0xFF64748B), 55), // sleepy grey
  'amused': MoodVisual(Color(0xFF34D399), 74), // laughing emerald
  'bored': MoodVisual(Color(0xFF78716C), 58), // dull stone
  'excited': MoodVisual(Color(0xFFFB923C), 105), // excited tangerine
  // Dark — deep blues, purples, blacks
  'melancholic': MoodVisual(Color(0xFF6366F1), 68), // sad indigo
  'haunted': MoodVisual(Color(0xFF7C3AED), 78), // ghost violet
  'conflicted': MoodVisual(Color(0xFFD97706), 85), // torn amber
  'guilty': MoodVisual(Color(0xFF78350F), 82), // guilt brown
  'determined': MoodVisual(Color(0xFFF97316), 95), // resolute orange
  'grieving': MoodVisual(Color(0xFF312E81), 72), // mourning navy
  'furious': MoodVisual(Color(0xFF7F1D1D), 120), // cold fury maroon
  // Additional — each distinct
  'nostalgic': MoodVisual(Color(0xFF8B5CF6), 70), // memory violet
  'curious': MoodVisual(Color(0xFF06B6D4), 72), // inquisitive cyan
  'irritated': MoodVisual(Color(0xFFEA580C), 92), // annoyed burnt orange
  'defiant': MoodVisual(Color(0xFFDC2626), 98), // defiance red
  'vulnerable': MoodVisual(Color(0xFFDDD6FE), 88), // exposed lavender
  'grateful': MoodVisual(Color(0xFF2DD4BF), 73), // thankful turquoise
  'worried': MoodVisual(Color(0xFFFCD34D), 88), // anxious yellow
  'embarrassed': MoodVisual(Color(0xFFFF6B9D), 96), // mortified coral
};
