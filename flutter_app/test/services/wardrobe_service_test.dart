// WardrobeService against package:http/testing MockClient. The token is
// injected, so nothing touches localStorage and this runs on the VM:
//   flutter test test/services/wardrobe_service_test.dart
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';

import 'package:companion_app/services/wardrobe_service.dart';

import '../support/her_day_fixtures.dart';

const _base = 'https://klukai.example.cc';

/// Raw UTF-8 bytes with NO content-type, the worst case: the service must not
/// rely on a charset header to keep `·` and `—` intact.
http.Response _json(Object body, [int status = 200]) =>
    http.Response.bytes(utf8.encode(jsonEncode(body)), status);

WardrobeService _svc(MockClientHandler handler) =>
    WardrobeService(serverUrl: _base, client: MockClient(handler), token: () => 'tok-123');

void _expectAuth(http.BaseRequest req) {
  expect(req.headers['Authorization'], 'Bearer tok-123');
  expect(req.headers['Content-Type'], startsWith('application/json'));
}

void main() {
  group('WardrobeService', () {
    test('fetchOutfits GETs /api/outfits and decodes it', () async {
      final svc = _svc((req) async {
        expect(req.method, 'GET');
        expect(req.url.toString(), '$_base/api/outfits');
        _expectAuth(req);
        return _json(outfitsJson(), 200);
      });
      final w = await svc.fetchOutfits();
      expect(w.canon, hasLength(3));
      expect(w.everyday, hasLength(2));
      expect(w.today!.reason, 'Off-duty plans.');
    });

    test('fetchHerDay GETs /api/her-day and decodes it', () async {
      final svc = _svc((req) async {
        expect(req.url.path, '/api/her-day');
        _expectAuth(req);
        return _json(herDayJson(override: 'mission'), 200);
      });
      final d = await svc.fetchHerDay();
      expect(d.status!.override, 'mission');
      expect(d.schedule, hasLength(4));
    });

    test('fetchLog passes days and decodes the history', () async {
      final svc = _svc((req) async {
        expect(req.url.path, '/api/wardrobe/log');
        expect(req.url.queryParameters, {'days': '14'});
        _expectAuth(req);
        return _json(logJson(), 200);
      });
      final log = await svc.fetchLog(days: 14);
      expect(log.map((e) => e.outfitId), ['speed_star', 'immaculate_service']);
    });

    test('fetchLog defaults to 30 days', () async {
      final svc = _svc((req) async {
        expect(req.url.queryParameters['days'], '30');
        return _json({'history': []}, 200);
      });
      expect(await svc.fetchLog(), isEmpty);
    });

    test('setCostume POSTs the id and returns the confirmed costume', () async {
      final svc = _svc((req) async {
        expect(req.method, 'POST');
        expect(req.url.path, '/api/costume');
        _expectAuth(req);
        expect(jsonDecode(req.body), {'costume': 'speed_star'});
        return _json({'costume': 'speed_star'}, 200);
      });
      expect(await svc.setCostume('speed_star'), 'speed_star');
    });

    test('setCostume falls back to the requested id on an odd body', () async {
      final svc = _svc((_) async => _json(['ok'], 200));
      expect(await svc.setCostume('hangar_coveralls'), 'hangar_coveralls');
    });

    test('maps 403 to locked, 400 to unknown, 401 to auth expired', () async {
      Future<WardrobeServiceException> statusOf(int code) async {
        final svc = _svc((_) async => http.Response('{"error":"x"}', code));
        try {
          await svc.setCostume('immaculate_service');
        } on WardrobeServiceException catch (e) {
          return e;
        }
        fail('expected WardrobeServiceException for $code');
      }

      final locked = await statusOf(403);
      expect(locked.isLocked, isTrue);
      expect(locked.isUnknown, isFalse);
      expect((await statusOf(400)).isUnknown, isTrue);
      final expired = await statusOf(401);
      expect(expired.isAuthExpired, isTrue);
      expect(expired.toString(), 'WardrobeServiceException: HTTP 401');
    });

    test('GET errors throw with the status code', () async {
      final svc = _svc((_) async => http.Response('nope', 503));
      await expectLater(
        svc.fetchHerDay(),
        throwsA(isA<WardrobeServiceException>().having((e) => e.statusCode, 'status', 503)),
      );
      await expectLater(svc.fetchOutfits(), throwsA(isA<WardrobeServiceException>()));
      await expectLater(svc.fetchLog(), throwsA(isA<WardrobeServiceException>()));
    });

    test('non-ASCII labels survive without a charset header', () async {
      final svc = _svc((_) async => _json(herDayJson()));
      final d = await svc.fetchHerDay();
      expect(d.status!.label, 'Hangar \u00B7 tuning the suspension');
      expect(d.schedule.first.activity, "reports at 0200 \u2014 she wasn't sleeping anyway");
    });

    test('a non-object body decodes to empty defaults', () async {
      final svc = _svc((_) async => http.Response('[]', 200));
      final d = await svc.fetchHerDay();
      expect(d.status, isNull);
      expect(d.hasStatusLine, isFalse);
    });

    test('default token reader works off-browser (empty bearer)', () async {
      final svc = WardrobeService(
        serverUrl: _base,
        client: MockClient((req) async {
          expect(req.headers['Authorization'], 'Bearer ');
          return _json(herDayJson(), 200);
        }),
      );
      expect((await svc.fetchHerDay()).date, '2026-10-04');
    });
  });
}
