/// Push-to-talk lifecycle. Chat owns one of these; tests drive the same object.
///
/// Text chat is never blocked by a voice phase. A transcript is a separate
/// utterance and must not replace a typed draft.
enum SpeechPhase { idle, requestingPermission, recording, transcribing, error }

class SpeechCapture {
  SpeechPhase phase = SpeechPhase.idle;
  String? status;
  int _epoch = 0;
  bool _disposed = false;

  int get epoch => _epoch;

  /// Voice being down, recording, or transcribing still leaves the composer usable.
  bool get textAllowed => !_disposed;

  bool get captureOpen =>
      !_disposed && (phase == SpeechPhase.idle || phase == SpeechPhase.error);

  bool isCurrent(int epoch) => !_disposed && epoch == _epoch;

  /// Starts a capture. Returns null when one is already in flight.
  int? begin() {
    if (!captureOpen) return null;
    _epoch += 1;
    phase = SpeechPhase.requestingPermission;
    status = null;
    return _epoch;
  }

  void markRecording(int epoch) {
    if (!isCurrent(epoch)) return;
    phase = SpeechPhase.recording;
    status = null;
  }

  void markTranscribing(int epoch) {
    if (!isCurrent(epoch)) return;
    phase = SpeechPhase.transcribing;
    status = null;
  }

  void fail(int epoch, String reason) {
    if (!isCurrent(epoch)) return;
    phase = SpeechPhase.error;
    status = reason;
  }

  void succeed(int epoch) {
    if (!isCurrent(epoch)) return;
    phase = SpeechPhase.idle;
    status = null;
  }

  void cancel() {
    if (_disposed) return;
    _epoch += 1;
    phase = SpeechPhase.idle;
    status = null;
  }

  void dispose() {
    _disposed = true;
    _epoch += 1;
    phase = SpeechPhase.idle;
    status = null;
  }

  /// The composer draft after a transcript arrives. Unrelated typed text stays.
  static String draftAfterTranscript(String draft, String transcript) => draft;

  static String voiceUtterance(String transcript) => transcript.trim();
}

/// Recording bridge. Production uses the page's audioRecorder; tests supply a fake.
class SpeechBridge {
  final Future<bool> Function() start;
  final Future<String?> Function() stop;

  const SpeechBridge({required this.start, required this.stop});
}
