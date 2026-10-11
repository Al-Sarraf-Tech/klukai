// ResumePolicy: when coming back from the background redials. VM-runnable.
import 'package:flutter_test/flutter_test.dart';

import 'package:companion_app/services/resume_policy.dart';

void main() {
  const policy = ResumePolicy();

  test('link down: always redial', () {
    expect(policy.shouldReconnect(connected: false, sinceLastFrame: Duration.zero), isTrue);
  });

  test('link up and recently heard from: leave it alone', () {
    expect(policy.shouldReconnect(connected: true, sinceLastFrame: const Duration(seconds: 5)),
        isFalse);
  });

  test('a brief absence while connected leaves a healthy socket alone', () {
    // The duration is how long the page was HIDDEN, not how long the socket
    // has been quiet: a one-second tab switch after 25 quiet seconds of chat
    // must not redial — the server cancels in-flight work (an image, say)
    // when the old socket closes.
    expect(policy.shouldReconnect(connected: true, sinceLastFrame: const Duration(seconds: 1)),
        isFalse);
    expect(policy.shouldReconnect(connected: true, sinceLastFrame: Duration.zero), isFalse);
  });

  test('link "up" but silent past the threshold: iOS froze it, redial', () {
    expect(policy.shouldReconnect(connected: true, sinceLastFrame: const Duration(seconds: 25)),
        isTrue);
    expect(policy.shouldReconnect(connected: true, sinceLastFrame: const Duration(minutes: 10)),
        isTrue);
  });

  test('threshold is configurable', () {
    const strict = ResumePolicy(staleAfter: Duration(seconds: 5));
    expect(strict.shouldReconnect(connected: true, sinceLastFrame: const Duration(seconds: 6)),
        isTrue);
  });
}
