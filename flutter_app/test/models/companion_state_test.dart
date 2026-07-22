@TestOn('browser')
library;

// Unit tests for the CompanionState immutable model + copyWith semantics.
import 'package:flutter_test/flutter_test.dart';
import 'package:companion_app/models/companion_state.dart';

void main() {
  group('CompanionState defaults', () {
    test('const default constructor yields a cold/disconnected state', () {
      const s = CompanionState();
      expect(s.mood, 'composed');
      expect(s.isConnected, isFalse);
      expect(s.isTyping, isFalse);
      expect(s.currentModel, isNull);
      expect(s.affectionScore, 0);
      expect(s.affectionLevel, 0);
      expect(s.affectionLevelName, 'Cold Assessment');
      expect(s.isInputLocked, isFalse);
      expect(s.inputLockReason, isNull);
    });

    test('explicit construction stores all fields', () {
      const s = CompanionState(
        mood: 'tender',
        isConnected: true,
        isTyping: true,
        currentModel: 'venice',
        affectionScore: 540,
        affectionLevel: 6,
        affectionLevelName: 'Deep Devotion',
        isInputLocked: true,
        inputLockReason: 'cooldown',
      );
      expect(s.mood, 'tender');
      expect(s.isConnected, isTrue);
      expect(s.isTyping, isTrue);
      expect(s.currentModel, 'venice');
      expect(s.affectionScore, 540);
      expect(s.affectionLevel, 6);
      expect(s.affectionLevelName, 'Deep Devotion');
      expect(s.isInputLocked, isTrue);
      expect(s.inputLockReason, 'cooldown');
    });
  });

  group('CompanionState.copyWith', () {
    const base = CompanionState(
      mood: 'composed',
      isConnected: true,
      isTyping: false,
      currentModel: 'venice',
      affectionScore: 100,
      affectionLevel: 3,
      affectionLevelName: 'Guarded Interest',
      isInputLocked: false,
    );

    test('overrides a single field, leaves the rest intact', () {
      final next = base.copyWith(mood: 'focused');
      expect(next.mood, 'focused');
      expect(next.isConnected, isTrue);
      expect(next.affectionScore, 100);
      expect(next.affectionLevel, 3);
      expect(next.affectionLevelName, 'Guarded Interest');
    });

    test('updates connection + typing flags together', () {
      final next = base.copyWith(isConnected: false, isTyping: true);
      expect(next.isConnected, isFalse);
      expect(next.isTyping, isTrue);
      expect(next.mood, 'composed');
    });

    test('advances the affection tier as a group', () {
      final next = base.copyWith(
        affectionScore: 250,
        affectionLevel: 4,
        affectionLevelName: 'Trusted Ally',
      );
      expect(next.affectionScore, 250);
      expect(next.affectionLevel, 4);
      expect(next.affectionLevelName, 'Trusted Ally');
    });

    test('omitted args preserve previous values', () {
      final next = base.copyWith();
      expect(next.mood, base.mood);
      expect(next.isConnected, base.isConnected);
      expect(next.currentModel, base.currentModel);
      expect(next.affectionScore, base.affectionScore);
      expect(next.affectionLevelName, base.affectionLevelName);
    });

    test('inputLockReason is intentionally cleared when unspecified', () {
      // NOTE: copyWith assigns inputLockReason directly (no ?? fallback),
      // so calling copyWith without it resets the reason to null even if it
      // was previously set. This is the documented behavior of the model.
      const locked = CompanionState(
        isInputLocked: true,
        inputLockReason: 'mood_swing',
      );
      final next = locked.copyWith(isTyping: true);
      expect(next.isTyping, isTrue);
      expect(next.isInputLocked, isTrue); // bool still preserved via ??
      expect(next.inputLockReason, isNull); // reason dropped by design
    });

    test('can set a fresh inputLockReason', () {
      final next = base.copyWith(isInputLocked: true, inputLockReason: 'busy');
      expect(next.isInputLocked, isTrue);
      expect(next.inputLockReason, 'busy');
    });
  });
}
