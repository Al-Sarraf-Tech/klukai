import 'package:flutter_test/flutter_test.dart';
import 'package:companion_app/services/speech_capture.dart';

void main() {
  test('a second capture is refused until the first epoch ends', () {
    final speech = SpeechCapture();
    final first = speech.begin();
    expect(first, isNotNull);
    expect(speech.begin(), isNull);
    speech.fail(first!, 'permission');
    expect(speech.phase, SpeechPhase.error);
    expect(speech.textAllowed, isTrue);
    expect(speech.begin(), isNotNull);
  });

  test('a late result after cancel or dispose does not revive the capture', () {
    final speech = SpeechCapture();
    final epoch = speech.begin()!;
    speech.markRecording(epoch);
    speech.markTranscribing(epoch);
    speech.cancel();
    speech.succeed(epoch);
    speech.fail(epoch, 'stt');
    expect(speech.phase, SpeechPhase.idle);
    expect(speech.isCurrent(epoch), isFalse);

    final next = speech.begin()!;
    speech.dispose();
    speech.markRecording(next);
    expect(speech.isCurrent(next), isFalse);
    expect(speech.textAllowed, isFalse);
  });

  test('a transcript never replaces a typed draft', () {
    expect(SpeechCapture.draftAfterTranscript('keep this draft', 'spoken line'), 'keep this draft');
    expect(SpeechCapture.draftAfterTranscript('', '  spoken line  '), '');
    expect(SpeechCapture.voiceUtterance('  spoken line  '), 'spoken line');
  });
}
