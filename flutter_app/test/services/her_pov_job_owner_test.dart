import 'dart:async';

import 'package:companion_app/services/her_pov_job_owner.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test(
    'a detached pending submission cannot accept an ID or acquire a poll',
    () {
      final owner = HerPovJobOwner();
      final run = owner.begin();
      owner.dispose();
      expect(owner.accept(run, 'server-accepted-original-id'), isFalse);
      expect(owner.isCurrent(run), isFalse);
      expect(owner.claimPoll(), isNull);
      expect(owner.begin, throwsStateError);
    },
  );

  test('a live job admits one poll and releases only that exact claim', () {
    final owner = HerPovJobOwner();
    final run = owner.begin();
    expect(owner.accept(run, 'job-a'), isTrue);
    final first = owner.claimPoll()!;
    expect(first.jobId, 'job-a');
    expect(owner.claimPoll(), isNull);
    expect(owner.ownsPoll(first), isTrue);
    owner.release(first);
    final second = owner.claimPoll()!;
    expect(owner.ownsPoll(first), isFalse);
    expect(owner.ownsPoll(second), isTrue);
    owner.release(first);
    expect(
      owner.claimPoll(),
      isNull,
      reason: 'an old finally block cannot release a newer in-flight poll',
    );
  });

  test('stale A completion cannot finish B or release B polling', () async {
    final owner = HerPovJobOwner();
    final a = owner.begin();
    owner.accept(a, 'job-a');
    final delayedA = owner.claimPoll()!;
    final responseA = Completer<void>();
    final finishedA = responseA.future.then((_) => owner.complete(delayedA));
    final b = owner.begin();
    owner.accept(b, 'job-b');
    final currentB = owner.claimPoll()!;
    expect(owner.isCurrent(a), isFalse);
    expect(owner.ownsPoll(delayedA), isFalse);
    responseA.complete();
    expect(await finishedA, isFalse);
    owner.release(delayedA);
    expect(owner.ownsPoll(currentB), isTrue);
    expect(owner.claimPoll(), isNull);
    expect(owner.complete(currentB), isTrue);
    expect(
      owner.claimPoll(),
      isNull,
      reason: 'terminal B never restarts polling',
    );
  });

  test('late POST acceptance for A cannot replace accepted job B', () {
    final owner = HerPovJobOwner();
    final a = owner.begin();
    final b = owner.begin();
    expect(owner.accept(b, 'job-b'), isTrue);
    expect(owner.accept(a, 'job-a'), isFalse);
    expect(owner.claimPoll()!.jobId, 'job-b');
  });

  test('empty or repeated job acceptance does not invent a poll target', () {
    final owner = HerPovJobOwner();
    final run = owner.begin();
    expect(owner.accept(run, ''), isFalse);
    expect(owner.claimPoll(), isNull);
    expect(owner.accept(run, 'original-id'), isTrue);
    expect(owner.accept(run, 'replacement-id'), isFalse);
    expect(owner.claimPoll()!.jobId, 'original-id');
  });
}
