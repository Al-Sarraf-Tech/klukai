// Her shown heart rate: the CHAT header's ECG and BPM, and the `bpm` the 3D
// stage's HUD gets. Three sources, in priority order:
//
// 1. a surge (`heartbeat_spike`), while its few seconds last;
// 2. her live heart rate (`heartbeat`, sent by the core once per turn), until
//    the next one arrives or it is [kLiveHeartbeatTtl] old;
// 3. her mood's resting rate (`moodVisualFor(mood).bpm`).
//
// Pure (the clock is passed in) so the rules are VM-testable.
import '../theme/mood_visuals.dart';

/// How long a live reading stands without a newer one.
const Duration kLiveHeartbeatTtl = Duration(minutes: 15);

/// The range a live reading is shown in.
const int kLiveBpmMin = 48;
const int kLiveBpmMax = 190;

/// A `heartbeat` frame's `bpm`: an integer, clamped to
/// [kLiveBpmMin]..[kLiveBpmMax]; null when it is anything else (the frame
/// is malformed and ignored).
int? liveBpmFrom(Object? raw) =>
    raw is int ? raw.clamp(kLiveBpmMin, kLiveBpmMax) : null;

/// The heart rate to show for [mood] at [now], given an active surge [spike]
/// and the latest live reading [live] taken at [liveAt].
int shownHeartRate({
  required String mood,
  int? spike,
  int? live,
  DateTime? liveAt,
  required DateTime now,
}) {
  if (spike != null) return spike;
  if (live != null &&
      liveAt != null &&
      now.difference(liveAt) <= kLiveHeartbeatTtl) {
    return live;
  }
  return moodVisualFor(mood).bpm;
}
