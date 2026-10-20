// Tests for the shared mood-visuals table (lib/theme/mood_visuals.dart) —
// the single source of truth for mood -> glow color + BPM lookups.
//
// Past bug class: two diverged mood tables (chat screen vs MoodIndicator)
// left 16 moods falling through to grey in one of them. These tests pin the
// shared table's contract: every key has a sane visual, unknown keys get the
// default fallback, and the entries the UI tests rely on stay stable.
//
// No package:web imports — runs on the VM and under chrome.
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:companion_app/theme/mood_visuals.dart';

/// The fifteen moods added with her live heart rate, and the resting BPM the
/// core contract gives each.
const kNewMoodBpm = <String, int>{
  'serene': 60,
  'cozy': 62,
  'wistful': 66,
  'lonely': 70,
  'stern': 70,
  'smug': 74,
  'hopeful': 76,
  'sulking': 78,
  'admiring': 80,
  'teasing': 82,
  'restless': 88,
  'heartbroken': 92,
  'giddy': 104,
  'overwhelmed': 118,
  'breathless': 140,
};

void main() {
  group('kMoodVisuals table integrity', () {
    test('contains the full mood roster (no mood left behind)', () {
      final expectedMoods = [
        // Core
        'composed', 'focused', 'prideful', 'exasperated', 'protective',
        'quietly_pleased', 'competitive', 'tender', 'longing', 'battle_ready',
        // Romantic
        'flustered', 'affectionate', 'shy', 'yearning', 'devoted',
        'passionate', 'jealous', 'possessive', 'smitten', 'infatuated',
        // Tactical
        'vigilant', 'calculating', 'hunting', 'adrenaline',
        // Mission stress
        'scared', 'terrified', 'panicked', 'desperate', 'relieved',
        // Relaxed
        'content', 'playful', 'drowsy', 'amused', 'bored', 'excited',
        // Dark
        'melancholic', 'haunted', 'conflicted', 'guilty', 'determined',
        'grieving', 'furious',
        // Additional
        'nostalgic', 'curious', 'irritated', 'defiant', 'vulnerable',
        'grateful', 'worried', 'embarrassed',
        // Live heart rate (2026-10-19)
        ...kNewMoodBpm.keys,
      ];
      for (final mood in expectedMoods) {
        expect(
          kMoodVisuals.containsKey(mood),
          isTrue,
          reason: 'mood "$mood" missing from kMoodVisuals',
        );
      }
      expect(kMoodVisuals.length, expectedMoods.length);
    });

    test('every entry has a plausible BPM (40..200)', () {
      kMoodVisuals.forEach((mood, visual) {
        expect(
          visual.bpm,
          inInclusiveRange(40, 200),
          reason: 'mood "$mood" has implausible BPM ${visual.bpm}',
        );
      });
    });

    test('pins entries the widget layer depends on', () {
      expect(kMoodVisuals['composed'], isNotNull);
      expect(kMoodVisuals['composed']!.bpm, 65);
      expect(kMoodVisuals['passionate']!.bpm, 115);
      expect(kMoodVisuals['flustered']!.glow, const Color(0xFFF472B6));
      expect(kMoodVisuals['battle_ready']!.bpm, 130);
    });

    test('the fifteen new moods rest at the contract BPM', () {
      expect(kNewMoodBpm, hasLength(15));
      kNewMoodBpm.forEach((mood, bpm) {
        expect(
          moodVisualFor(mood),
          isNot(same(kDefaultMoodVisual)),
          reason: mood,
        );
        expect(kMoodVisuals[mood]!.bpm, bpm, reason: mood);
      });
    });

    test('each new mood glows in a colour no other mood uses', () {
      for (final mood in kNewMoodBpm.keys) {
        final glow = kMoodVisuals[mood]!.glow;
        final sharing = kMoodVisuals.entries
            .where((e) => e.key != mood && e.value.glow == glow)
            .map((e) => e.key)
            .toList();
        expect(sharing, isEmpty, reason: '$mood shares its glow with $sharing');
        expect(glow, isNot(kDefaultMoodVisual.glow), reason: mood);
        expect(glow.a, 1.0, reason: '$mood glow must be opaque');
      }
    });
  });

  group('moodVisualFor fallback', () {
    test('returns the table entry for a known mood', () {
      expect(moodVisualFor('tender'), same(kMoodVisuals['tender']));
    });

    test(
      'unknown mood falls back to the default visual (never grey-holes)',
      () {
        expect(moodVisualFor('totally_new_mood'), same(kDefaultMoodVisual));
        expect(moodVisualFor(''), same(kDefaultMoodVisual));
      },
    );

    test('the default visual is itself sane', () {
      expect(kDefaultMoodVisual.bpm, 70);
      expect(kDefaultMoodVisual.glow, const Color(0xFF4FC3F7));
    });
  });
}
