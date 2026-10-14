import 'dart:async';
import 'dart:convert';

import 'package:companion_app/services/tts_request.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

void main() {
  test('full replies are chunked without losing their final words', () {
    final text =
        '${'The hangar is quiet tonight. ' * 30}Final report complete.';
    final chunks = chunkForTts(text);
    expect(chunks.length, greaterThan(1));
    expect(chunks.every((part) => part.length <= 500), isTrue);
    expect(chunks.join(' '), text.trim());
    expect(chunkForTts('Dr. Smith is here. Ready!'), [
      'Dr. Smith is here.',
      'Ready!',
    ]);
  });

  test('queued voice is polled and completed audio is returned', () async {
    final calls = <String>[];
    var polls = 0;
    final client = MockClient((request) async {
      calls.add('${request.method} ${request.url.path}');
      expect(request.headers['Authorization'], 'Bearer test-token');
      if (request.method == 'POST') {
        return http.Response(
          jsonEncode({'job_id': 'test-job', 'status': 'queued'}),
          202,
        );
      }
      if (request.method == 'DELETE') return http.Response('{}', 200);
      polls++;
      return http.Response(
        jsonEncode(
          polls == 1
              ? {'status': 'running'}
              : {'status': 'completed', 'audio': 'YXVkaW8='},
        ),
        200,
      );
    });
    final request = TtsAudioRequest(
      client: client,
      baseUrl: 'http://test',
      token: 'test-token',
      pollInterval: Duration.zero,
    );
    expect(await request.run('Hello.'), 'YXVkaW8=');
    expect(calls, [
      'POST /api/tts',
      'GET /api/tts/jobs/test-job',
      'GET /api/tts/jobs/test-job',
      'DELETE /api/tts/jobs/test-job',
    ]);
  });

  test(
    'voice cancellation aborts a stalled poll and deletes its job',
    () async {
      final polling = Completer<void>();
      var deleted = false;
      final client = MockClient.streaming((request, body) async {
        if (request.method == 'POST') {
          return http.StreamedResponse(
            Stream.value(
              utf8.encode('{"job_id":"cancel-me","status":"queued"}'),
            ),
            202,
          );
        }
        if (request.method == 'DELETE') {
          deleted = true;
          return http.StreamedResponse(Stream.value(utf8.encode('{}')), 200);
        }
        polling.complete();
        await (request as http.AbortableRequest).abortTrigger;
        throw http.RequestAbortedException(request.url);
      });
      final request = TtsAudioRequest(
        client: client,
        baseUrl: 'http://test',
        token: 'test-token',
        pollInterval: Duration.zero,
      );
      final pending = request.run('Hello.');
      final failure = expectLater(
        pending,
        throwsA(isA<http.RequestAbortedException>()),
      );
      await polling.future;
      await request.cancel();
      await failure;
      expect(deleted, isTrue);
    },
  );

  test('a hung voice request is bounded by its total deadline', () async {
    var aborted = false;
    final client = MockClient.streaming((request, body) async {
      await (request as http.AbortableRequest).abortTrigger;
      aborted = true;
      throw http.RequestAbortedException(request.url);
    });
    final request = TtsAudioRequest(
      client: client,
      baseUrl: 'http://test',
      token: 'test-token',
      deadline: const Duration(milliseconds: 10),
    );
    await expectLater(
      request.run('Hello.'),
      throwsA(isA<http.RequestAbortedException>()),
    );
    expect(aborted, isTrue);
  });

  test('her lines are read as English text in her Japanese voice', () {
    // "ja" makes the voice service 500 (no cutlet) — never send it.
    expect(ttsRequestBody('Report.')['language'], 'en');
  });

  test('long lines are capped at 500 characters', () {
    final body = ttsRequestBody('a' * 900);
    expect(body['text']!.length, 500);
    expect(ttsRequestBody('short')['text'], 'short');
  });

  test('the request body carries the sentence-clipped text', () {
    const sentence = 'The hangar is quiet tonight. ';
    final text = ttsRequestBody(sentence * 30)['text']!;
    expect(text, endsWith('tonight.'));
    expect(text.length, lessThanOrEqualTo(kTtsMaxChars));
  });

  group('clipForTts', () {
    test('text within the limit is untouched', () {
      expect(clipForTts('Report.'), 'Report.');
      expect(clipForTts('a' * kTtsMaxChars).length, kTtsMaxChars);
    });

    test('a long line stops at its last sentence end, not mid-word', () {
      const sentence = 'The hangar is quiet tonight. '; // 29 chars
      final text = sentence * 30; // 870 chars
      final clipped = clipForTts(text);
      expect(clipped.length, lessThanOrEqualTo(kTtsMaxChars));
      expect(clipped, endsWith('tonight.'));
      expect(text.startsWith(clipped), isTrue);
      // The sentence after the cut would have crossed the limit.
      expect(clipped.length + sentence.length, greaterThan(kTtsMaxChars));
    });

    test('"!" and "?" end a sentence too, before a space or a newline', () {
      expect(
        clipForTts('Hold position! Now move out quietly please.', limit: 30),
        'Hold position!',
      );
      expect(
        clipForTts('Are you ready?\nMove out on my mark.', limit: 25),
        'Are you ready?',
      );
      expect(
        clipForTts(
          'Hold here.\nI will scout ahead and report back.',
          limit: 25,
        ),
        'Hold here.',
      );
    });

    test('a sentence ending exactly at the limit is kept whole', () {
      expect(
        clipForTts('Confirmed. Then more words follow here.', limit: 10),
        'Confirmed.',
      );
    });

    test(
      'falls back to the hard cut when the only sentence end comes too early',
      () {
        // One short sentence then a wall of text: clipping at "Yes." would drop
        // nearly everything she said, so cut hard at the limit instead.
        final clipped = clipForTts('Yes. ${'a' * 30}', limit: 20);
        expect(clipped.length, 20);
        expect(clipped, 'Yes. ${'a' * 15}');
      },
    );

    test('no sentence end at all: plain hard cut', () {
      expect(clipForTts('a' * 900).length, kTtsMaxChars);
    });
  });
}
