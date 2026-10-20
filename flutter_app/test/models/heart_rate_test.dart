// Her shown heart rate (lib/models/heart_rate.dart): which reading wins, when
// a live reading goes stale, and how a `heartbeat` frame's bpm is read.
// Pure: VM-runnable, with the clock passed in.
import 'package:flutter_test/flutter_test.dart';
import 'package:companion_app/models/heart_rate.dart';
import 'package:companion_app/theme/mood_visuals.dart';

void main() {
  final t0 = DateTime.utc(2026, 10, 19, 22);

  group('liveBpmFrom (a heartbeat frame\'s bpm)', () {
    test('an integer in range is taken as is', () {
      expect(liveBpmFrom(48), 48);
      expect(liveBpmFrom(96), 96);
      expect(liveBpmFrom(190), 190);
    });

    test('an integer out of range is clamped to 48..190', () {
      expect(liveBpmFrom(12), 48);
      expect(liveBpmFrom(0), 48);
      expect(liveBpmFrom(-5), 48);
      expect(liveBpmFrom(191), 190);
      expect(liveBpmFrom(400), 190);
    });

    test('anything but an integer is malformed (null)', () {
      for (final bad in <Object?>[
        null,
        '90',
        90.5,
        true,
        <int>[90],
        {},
      ]) {
        expect(liveBpmFrom(bad), isNull, reason: '$bad');
      }
    });
  });

  group('shownHeartRate priority', () {
    test('no surge and no live reading: the mood\'s resting rate', () {
      expect(shownHeartRate(mood: 'composed', now: t0), 65);
      expect(shownHeartRate(mood: 'breathless', now: t0), 140);
      expect(
        shownHeartRate(mood: 'not_a_mood', now: t0),
        kDefaultMoodVisual.bpm,
      );
    });

    test('a fresh live reading beats the mood\'s resting rate', () {
      expect(
        shownHeartRate(mood: 'composed', live: 101, liveAt: t0, now: t0),
        101,
      );
    });

    test('a surge beats a fresh live reading', () {
      expect(
        shownHeartRate(
          mood: 'composed',
          spike: 170,
          live: 101,
          liveAt: t0,
          now: t0,
        ),
        170,
      );
      expect(shownHeartRate(mood: 'composed', spike: 170, now: t0), 170);
    });

    test('a live reading holds for 15 minutes, then the mood returns', () {
      int at(Duration later) => shownHeartRate(
        mood: 'serene',
        live: 101,
        liveAt: t0,
        now: t0.add(later),
      );
      expect(kLiveHeartbeatTtl, const Duration(minutes: 15));
      expect(at(const Duration(minutes: 14, seconds: 59)), 101);
      expect(at(kLiveHeartbeatTtl), 101);
      expect(at(kLiveHeartbeatTtl + const Duration(milliseconds: 1)), 60);
      expect(at(const Duration(hours: 3)), 60);
    });

    test('a reading without a time, or a time without one, is not live', () {
      expect(shownHeartRate(mood: 'cozy', live: 101, now: t0), 62);
      expect(shownHeartRate(mood: 'cozy', liveAt: t0, now: t0), 62);
    });

    test('a clock set back a little does not drop a fresh reading', () {
      expect(
        shownHeartRate(
          mood: 'cozy',
          live: 101,
          liveAt: t0,
          now: t0.subtract(const Duration(minutes: 2)),
        ),
        101,
      );
    });
  });
}
