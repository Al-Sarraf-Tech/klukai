// Model tests for Today's Outfit / Her Day. Pure Dart, no package:web, so
// these run on the VM:  flutter test test/models/her_day_test.dart
import 'package:flutter_test/flutter_test.dart';

import 'package:companion_app/models/her_day.dart';

import '../support/her_day_fixtures.dart';

void main() {
  group('OutfitInfo', () {
    test('parses the contract shape', () {
      final o = OutfitInfo.fromJson(outfitJson());
      expect(o.id, 'speed_star');
      expect(o.name, 'Speed Star');
      expect(o.blurb, 'Silver-white rider suit');
      expect(o.category, 'rider');
      expect(o.source, 'auto');
      expect(o.reason, 'Off-duty plans.');
      expect(o.canon, isTrue);
      expect(o.levelOk, isTrue);
    });

    test('missing and null fields fall back to defaults', () {
      final o = OutfitInfo.fromJson({
        'id': 'hangar_coveralls',
        'name': null,
        'reason': 7,
        'level_ok': 'yes',
      });
      expect(o.name, 'Hangar Coveralls'); // derived from the id
      expect(o.blurb, '');
      expect(o.source, 'auto');
      expect(o.reason, '');
      expect(o.canon, isFalse);
      expect(o.levelOk, isTrue);
    });

    test('sourceLine names who chose it', () {
      expect(OutfitInfo.fromJson(outfitJson()).sourceLine, 'Off-duty plans.');
      expect(OutfitInfo.fromJson(outfitJson(source: 'commander', reason: '')).sourceLine,
          'Your request.');
      expect(OutfitInfo.fromJson(outfitJson(source: 'roster', reason: '')).sourceLine,
          'On duty kit.');
      expect(OutfitInfo.fromJson(outfitJson(reason: '')).sourceLine, '');
      expect(OutfitInfo.fromJson(outfitJson(source: 'commander')).requestedByCommander, isTrue);
    });
  });

  group('Wardrobe', () {
    test('groups canon and everyday kit', () {
      final w = Wardrobe.fromJson(outfitsJson());
      expect(w.outfits, hasLength(5));
      expect(w.canon.map((o) => o.id),
          ['blazing_star', 'speed_star', 'immaculate_service']);
      expect(w.everyday.map((o) => o.id), ['hangar_coveralls', 'his_jacket']);
      expect(w.today!.id, 'speed_star');
      final maid = w.outfits.firstWhere((o) => o.id == 'immaculate_service');
      expect(maid.unlocked, isFalse);
      expect(maid.unlockLevel, 5);
      expect(w.outfits.firstWhere((o) => o.id == 'speed_star').current, isTrue);
    });

    test('tolerates junk entries, a missing today, and an empty body', () {
      final w = Wardrobe.fromJson({
        'outfits': ['nope', {'name': 'no id'}, {'id': 'x_y', 'unlock_level': 2.0}],
        'today': 'not a map',
      });
      expect(w.outfits.map((o) => o.id), ['x_y']);
      expect(w.outfits.single.name, 'X Y');
      expect(w.outfits.single.unlockLevel, 2);
      expect(w.outfits.single.source, 'original');
      expect(w.today, isNull);
      expect(Wardrobe.fromJson(const {}).outfits, isEmpty);
    });
  });

  group('HerDay', () {
    test('parses status, schedule, outfit and weather', () {
      final d = HerDay.fromJson(herDayJson());
      expect(d.date, '2026-10-04');
      expect(d.status!.label, 'Hangar · tuning the suspension');
      expect(d.status!.override, isNull);
      expect(d.schedule, hasLength(4));
      expect(d.schedule.where((b) => b.current).single.location, 'Hangar');
      expect(d.outfit!.name, 'Speed Star');
      expect(d.weather!.tempC, 14.2);
      expect(d.weather!.label, '14°C · clear');
      expect(d.hasStatusLine, isTrue);
    });

    test('override is kept; empty override is null', () {
      expect(HerDay.fromJson(herDayJson(override: 'gaming')).status!.override, 'gaming');
      expect(HerDay.fromJson(herDayJson(override: '')).status!.override, isNull);
    });

    test('label falls back to location · activity, or just the location', () {
      final s = HerDayStatus.fromJson({'location': 'Range', 'activity': 'drills'});
      expect(s.label, 'Range · drills');
      expect(HerDayStatus.fromJson({'location': 'Off duty', 'activity': ''}).label, 'Off duty');
      expect(HerDayStatus.fromJson({'activity': 'thinking'}).label, 'thinking');
    });

    test('null weather and missing sections', () {
      final d = HerDay.fromJson({'date': '2026-10-04', 'weather': null});
      expect(d.weather, isNull);
      expect(d.outfit, isNull);
      expect(d.status, isNull);
      expect(d.schedule, isEmpty);
      expect(d.hasStatusLine, isFalse);
    });

    test('withOutfit swaps only the outfit', () {
      final d = HerDay.fromJson(herDayJson());
      final next = d.withOutfit(OutfitInfo.fromJson(outfitJson(
          id: 'immaculate_service', name: 'Immaculate Service', source: 'commander')));
      expect(next.outfit!.name, 'Immaculate Service');
      expect(next.status!.label, d.status!.label);
      expect(next.schedule, same(d.schedule));
      expect(next.date, d.date);
    });

    test('weather labels with partial data', () {
      expect(HerDayWeather.fromJson({'temp_c': 3}).label, '3°C');
      expect(HerDayWeather.fromJson({'condition': 'rain'}).label, 'rain');
      expect(HerDayWeather.fromJson(const {}).label, '');
    });
  });

  group('ScheduleBlock', () {
    test('time range and line', () {
      final b = ScheduleBlock.fromJson(const {
        'start': '0500', 'end': '0800', 'location': 'Range', 'activity': 'drills',
      });
      expect(b.timeRange, '0500–0800');
      expect(b.line, 'Range · drills');
      expect(b.current, isFalse);
    });

    test('private block shows only Off duty', () {
      final b = ScheduleBlock.fromJson(const {
        'start': '1700', 'end': '2100', 'location': 'Off duty', 'activity': '',
      });
      expect(b.line, 'Off duty');
      expect(ScheduleBlock.fromJson(const {'start': '0000'}).timeRange, '0000');
    });
  });

  group('WardrobeLogEntry', () {
    test('parses history newest first, as served', () {
      final log = WardrobeLogEntry.listFromJson(logJson());
      expect(log.map((e) => e.day), ['2026-10-03', '2026-10-02']);
      expect(log.first.shortDay, 'OCT 03');
      expect(log.first.requested, isFalse);
      expect(log.last.requested, isTrue);
    });

    test('defaults and an unparseable day', () {
      final e = WardrobeLogEntry.fromJson({'day': 'yesterday', 'outfit_id': 'speed_star'});
      expect(e.name, 'Speed Star');
      expect(e.reason, '');
      expect(e.shortDay, 'yesterday');
      expect(WardrobeLogEntry.listFromJson({'history': null}), isEmpty);
    });
  });
}
