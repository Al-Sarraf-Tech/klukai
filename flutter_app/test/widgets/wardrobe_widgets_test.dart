// Widget tests for the TODAY card, the grouped wardrobe, and the worn log.
// VM-runnable:  flutter test test/widgets/wardrobe_widgets_test.dart
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:companion_app/models/her_day.dart';
import 'package:companion_app/widgets/wardrobe_widgets.dart';

import '../support/her_day_fixtures.dart';

Widget _wrap(Widget child) =>
    MaterialApp(home: Scaffold(body: SingleChildScrollView(child: child)));

void main() {
  group('TodayOutfitCard', () {
    testWidgets('auto: her reason', (tester) async {
      await tester.pumpWidget(_wrap(TodayOutfitCard(outfit: OutfitInfo.fromJson(outfitJson()))));
      expect(find.text('SPEED STAR'), findsOneWidget);
      expect(find.text('Silver-white rider suit'), findsOneWidget);
      expect(tester.widget<Text>(find.byKey(const Key('today-reason'))).data, 'Off-duty plans.');
    });

    testWidgets('commander: "Your request."', (tester) async {
      await tester.pumpWidget(_wrap(TodayOutfitCard(
          outfit: OutfitInfo.fromJson(outfitJson(source: 'commander', reason: '')))));
      expect(tester.widget<Text>(find.byKey(const Key('today-reason'))).data, 'Your request.');
    });

    testWidgets('roster: "On duty kit."', (tester) async {
      await tester.pumpWidget(_wrap(TodayOutfitCard(
          outfit: OutfitInfo.fromJson(outfitJson(
              id: 'hangar_coveralls', name: 'Hangar Coveralls', source: 'roster', reason: '')))));
      expect(find.text('HANGAR COVERALLS'), findsOneWidget);
      expect(tester.widget<Text>(find.byKey(const Key('today-reason'))).data, 'On duty kit.');
    });

    testWidgets('auto with no reason and no blurb shows just the name', (tester) async {
      await tester.pumpWidget(_wrap(TodayOutfitCard(
          outfit: const OutfitInfo(id: 'blazing_star', name: 'Blazing Star'))));
      expect(find.text('BLAZING STAR'), findsOneWidget);
      expect(find.byKey(const Key('today-reason')), findsNothing);
    });
  });

  group('WardrobeGrid', () {
    testWidgets('groups CANON and EVERYDAY KIT; locks show LV n', (tester) async {
      await tester.pumpWidget(_wrap(WardrobeGrid(wardrobe: Wardrobe.fromJson(outfitsJson()))));
      expect(find.text('CANON'), findsOneWidget);
      expect(find.text('EVERYDAY KIT'), findsOneWidget);

      Finder inGroup(String id) => find.byKey(Key('wardrobe-item-$id'));
      final canonY = tester.getTopLeft(find.text('CANON')).dy;
      final everydayY = tester.getTopLeft(find.text('EVERYDAY KIT')).dy;
      expect(tester.getTopLeft(inGroup('blazing_star')).dy, inExclusiveRange(canonY, everydayY));
      expect(tester.getTopLeft(inGroup('hangar_coveralls')).dy, greaterThan(everydayY));

      expect(find.byKey(const Key('wardrobe-lock-immaculate_service')), findsOneWidget);
      expect(find.byKey(const Key('wardrobe-lock-his_jacket')), findsOneWidget);
      expect(find.byKey(const Key('wardrobe-lock-speed_star')), findsNothing);
      expect(find.text('LV 5'), findsOneWidget);
      expect(find.text('LV 6'), findsOneWidget);
    });

    testWidgets('tap: unlocked selects, locked goes to onLockedTap', (tester) async {
      final selected = <String>[];
      final locked = <String>[];
      await tester.pumpWidget(_wrap(WardrobeGrid(
        wardrobe: Wardrobe.fromJson(outfitsJson()),
        onSelect: (o) => selected.add(o.id),
        onLockedTap: (o) => locked.add(o.id),
      )));
      await tester.tap(find.byKey(const Key('wardrobe-item-hangar_coveralls')));
      await tester.tap(find.byKey(const Key('wardrobe-item-immaculate_service')));
      expect(selected, ['hangar_coveralls']);
      expect(locked, ['immaculate_service']);
    });

    testWidgets('no callbacks: taps are inert, and an empty group is omitted', (tester) async {
      final onlyEveryday = Wardrobe.fromJson({
        'outfits': [
          {'id': 'range_kit', 'name': 'Range Kit', 'source': 'original', 'unlocked': true},
          {'id': 'his_jacket', 'name': 'His Jacket', 'source': 'original', 'unlocked': false,
            'unlock_level': 6},
        ],
      });
      await tester.pumpWidget(_wrap(WardrobeGrid(wardrobe: onlyEveryday)));
      expect(find.text('CANON'), findsNothing);
      expect(find.text('EVERYDAY KIT'), findsOneWidget);
      await tester.tap(find.byKey(const Key('wardrobe-item-range_kit')));
      await tester.tap(find.byKey(const Key('wardrobe-item-his_jacket')));
      expect(tester.takeException(), isNull);
    });
  });

  group('WornLogList', () {
    testWidgets('one row per day, request marked', (tester) async {
      await tester.pumpWidget(
          _wrap(WornLogList(entries: WardrobeLogEntry.listFromJson(logJson()))));
      expect(find.byKey(const Key('worn-log')), findsOneWidget);
      expect(find.text('OCT 03'), findsOneWidget);
      expect(find.text('OCT 02'), findsOneWidget);
      expect(find.textContaining('Off-duty plans.', findRichText: true), findsOneWidget);
      expect(find.textContaining('YOUR REQUEST', findRichText: true), findsOneWidget);
    });

    testWidgets('empty log', (tester) async {
      await tester.pumpWidget(_wrap(const WornLogList(entries: [])));
      expect(find.text('No record yet.'), findsOneWidget);
    });
  });
}
