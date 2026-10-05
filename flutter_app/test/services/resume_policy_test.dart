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
