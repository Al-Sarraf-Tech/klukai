import 'package:flutter/material.dart';

import '../models/her_day.dart';
import '../services/wardrobe_service.dart';
import '../theme/hud.dart';
import '../widgets/hud_parts.dart';

/// The Kit tab's category chips.
enum KitFilter { all, duty, offDuty, formal, seasonal }

extension KitFilterInfo on KitFilter {
  String get label => switch (this) {
    KitFilter.all => 'All',
    KitFilter.duty => 'Duty',
    KitFilter.offDuty => 'Off-duty',
    KitFilter.formal => 'Formal',
    KitFilter.seasonal => 'Seasonal',
  };
}

/// The catalog's categories (config/personality.yaml `costumes:`) under each
/// chip. A category not listed here still shows under ALL.
const kKitCategoryGroups = <KitFilter, Set<String>>{
  KitFilter.duty: {'duty', 'work', 'training', 'weather'},
  KitFilter.offDuty: {'off_duty', 'rider', 'sleep', 'swim'},
  KitFilter.formal: {'formal', 'oath'},
  KitFilter.seasonal: {'seasonal'},
};

bool kitFilterMatches(KitFilter filter, WardrobeItem item) =>
    filter == KitFilter.all ||
    (kKitCategoryGroups[filter]?.contains(item.category) ?? false);

/// `OFF-DUTY · ORIGINAL`.
String kitKindLine(WardrobeItem item) {
  final category = item.category.isEmpty
      ? 'KIT'
      : item.category.replaceAll('_', '-').toUpperCase();
  return '$category · ${item.isCanon ? 'CANON' : 'ORIGINAL'}';
}

/// What a locked card says instead of the blurb.
const kKitLockedLine = "She hasn't let you see it yet.";

enum _Phase { loading, ready, error }

/// Kit tab of the Tactical HUD: her wardrobe from the server catalog, with
/// the request flow (POST /api/costume) and the worn log. An embedded tab
/// body; the shell owns the Scaffold and the tab bar.
class HudKitScreen extends StatefulWidget {
  const HudKitScreen({
    super.key,
    required this.serverUrl,
    this.wardrobeService,
    this.onAuthExpired,
    this.onOutfitChanged,
  });

  final String serverUrl;

  /// /api/outfits, /api/costume, /api/wardrobe/log; defaults to a real one.
  final WardrobeService? wardrobeService;

  /// 401: the token is dead; the shell's logout/reauth path.
  final VoidCallback? onAuthExpired;

  /// She agreed to a request: today's outfit changed (refresh Day / Chat).
  final VoidCallback? onOutfitChanged;

  @override
  State<HudKitScreen> createState() => _HudKitScreenState();
}

class _HudKitScreenState extends State<HudKitScreen> {
  late final WardrobeService _service =
      widget.wardrobeService ?? WardrobeService(serverUrl: widget.serverUrl);

  _Phase _phase = _Phase.loading;
  Wardrobe? _wardrobe;
  List<WardrobeLogEntry> _log = const [];
  KitFilter _filter = KitFilter.all;
  bool _requesting = false;
  bool _authBounced = false;

  /// Her answer to the last request: (line, granted).
  (String, bool)? _reply;

  @override
  void initState() {
    super.initState();
    _load();
  }

  void _authExpired() {
    if (_authBounced || !mounted) return;
    _authBounced = true;
    widget.onAuthExpired?.call();
  }

  Future<void> _load() async {
    if (_wardrobe == null && _phase != _Phase.loading) {
      setState(() => _phase = _Phase.loading);
    }
    await Future.wait([_loadOutfits(), _loadLog()]);
  }

  Future<void> _loadOutfits() async {
    try {
      final w = await _service.fetchOutfits();
      if (!mounted) return;
      setState(() {
        _wardrobe = w;
        _phase = _Phase.ready;
      });
    } on WardrobeServiceException catch (e) {
      if (e.isAuthExpired) {
        _authExpired();
        return;
      }
      if (mounted && _wardrobe == null) setState(() => _phase = _Phase.error);
    } catch (_) {
      if (mounted && _wardrobe == null) setState(() => _phase = _Phase.error);
    }
  }

  /// The worn log fails soft: no section rather than an error.
  Future<void> _loadLog() async {
    try {
      final log = await _service.fetchLog(days: 30);
      if (mounted) setState(() => _log = log);
    } on WardrobeServiceException catch (e) {
      if (e.isAuthExpired) _authExpired();
    } catch (_) {}
  }

  /// His request, decided by the server (the same table as a chat request:
  /// the unlock level, the oath's days, the seasons, the hour).
  Future<void> _request(WardrobeItem item) async {
    if (_requesting) return;
    setState(() => _requesting = true);
    try {
      await _service.setCostume(item.id);
      if (!mounted) return;
      setState(() => _reply = ('Request granted · ${item.name}', true));
      await _loadOutfits();
      widget.onOutfitChanged?.call();
    } on WardrobeServiceException catch (e) {
      if (e.isAuthExpired) {
        _authExpired();
        return;
      }
      final line = e.isLocked
          ? e.refusalLine
          : e.isUnknown
          ? "That isn't in my wardrobe, Commander."
          : 'Comms disrupted. Try again.';
      if (mounted) setState(() => _reply = (line, false));
    } catch (_) {
      if (mounted) {
        setState(() => _reply = ('Comms disrupted. Try again.', false));
      }
    } finally {
      if (mounted) setState(() => _requesting = false);
    }
  }

  void _tapCard(WardrobeItem item) {
    if (!item.unlocked) {
      setState(() => _reply = ('Not yet.', false));
      return;
    }
    if (item.current) {
      showHudSnack(context, 'She already has it on.');
      return;
    }
    _confirm(item);
  }

  Future<void> _confirm(WardrobeItem item) async {
    final ok = await showModalBottomSheet<bool>(
      context: context,
      backgroundColor: HudColors.ground,
      shape: CutCornerBorder(side: BorderSide(color: HudColors.cyanA(0.4))),
      builder: (ctx) => SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(16, 16, 16, 12),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              HudLabel(kitKindLine(item)),
              const SizedBox(height: 4),
              Text(
                item.name,
                style: HudText.body(size: 20, weight: FontWeight.w700),
              ),
              if (item.blurb.isNotEmpty) ...[
                const SizedBox(height: 4),
                Text(
                  item.blurb,
                  style: HudText.body(size: 14, color: HudColors.textDim),
                ),
              ],
              const SizedBox(height: 16),
              HudActionButton(
                key: const Key('kit-confirm'),
                label: 'Ask her to wear it',
                onPressed: () => Navigator.of(ctx).pop(true),
              ),
              const SizedBox(height: 8),
              HudActionButton(
                label: 'Cancel',
                danger: true,
                onPressed: () => Navigator.of(ctx).pop(false),
              ),
            ],
          ),
        ),
      ),
    );
    if (ok == true) await _request(item);
  }

  /// REQUEST AN OUTFIT: every unlocked outfit she isn't already wearing.
  Future<void> _pick() async {
    final options = (_wardrobe?.outfits ?? const <WardrobeItem>[])
        .where((o) => o.unlocked && !o.current)
        .toList();
    if (options.isEmpty) {
      showHudSnack(context, 'Nothing else she will show you yet.');
      return;
    }
    final picked = await showModalBottomSheet<WardrobeItem>(
      context: context,
      isScrollControlled: true,
      backgroundColor: HudColors.ground,
      shape: CutCornerBorder(side: BorderSide(color: HudColors.cyanA(0.4))),
      builder: (ctx) => SafeArea(
        child: ConstrainedBox(
          constraints: BoxConstraints(
            maxHeight: MediaQuery.sizeOf(ctx).height * 0.7,
          ),
          child: ListView(
            shrinkWrap: true,
            padding: const EdgeInsets.symmetric(vertical: 12),
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 4, 16, 8),
                child: HudLabel('Request an outfit', color: HudColors.cyan),
              ),
              for (final o in options)
                Semantics(
                  button: true,
                  label: '${o.name}, ${kitKindLine(o)}',
                  excludeSemantics: true,
                  child: InkWell(
                    key: ValueKey('kit-pick-${o.id}'),
                    onTap: () => Navigator.of(ctx).pop(o),
                    child: Container(
                      constraints: const BoxConstraints(minHeight: 52),
                      padding: const EdgeInsets.symmetric(
                        horizontal: 16,
                        vertical: 6,
                      ),
                      alignment: Alignment.centerLeft,
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          Text(
                            o.name,
                            style: HudText.body(
                              size: 16,
                              weight: FontWeight.w600,
                            ),
                          ),
                          HudLabel(kitKindLine(o), size: 9.5),
                        ],
                      ),
                    ),
                  ),
                ),
            ],
          ),
        ),
      ),
    );
    if (picked != null) await _request(picked);
  }

  @override
  Widget build(BuildContext context) {
    final w = _wardrobe;
    final total = w?.outfits.length ?? 0;
    final unlocked = w?.outfits.where((o) => o.unlocked).length ?? 0;
    return HudBackground(
      child: RefreshIndicator(
        color: HudColors.cyan,
        backgroundColor: HudColors.ground,
        onRefresh: _load,
        child: ListView(
          key: const Key('kit-scroll'),
          physics: const AlwaysScrollableScrollPhysics(),
          padding: const EdgeInsets.fromLTRB(16, 16, 16, 24),
          children: [
            HudScreenHeader(
              kicker: 'Kit // Wardrobe',
              trailing: w == null ? null : '$unlocked / $total unlocked',
              trailingKey: const Key('kit-count'),
              title: 'Kit',
              subtitle: 'What she wears',
            ),
            const SizedBox(height: 8),
            HudChipRow<KitFilter>(
              keyPrefix: 'kit-filter',
              options: {for (final f in KitFilter.values) f: f.label},
              value: _filter,
              onChanged: (f) => setState(() => _filter = f),
            ),
            const SizedBox(height: 10),
            ...switch (_phase) {
              _Phase.loading => [const HudLoading('Opening her wardrobe...')],
              _Phase.error => [
                HudErrorState(
                  key: const Key('kit-error'),
                  title: 'Signal lost // wardrobe unreachable',
                  line: 'Her kit is still on the rack. The link is not.',
                  onRetry: _load,
                ),
              ],
              _Phase.ready => _body(w!),
            },
          ],
        ),
      ),
    );
  }

  List<Widget> _body(Wardrobe w) {
    final shown = w.outfits.where((o) => kitFilterMatches(_filter, o)).toList();
    return [
      if (shown.isEmpty)
        HudEmptyState(
          key: const Key('kit-empty'),
          icon: Icons.checkroom_outlined,
          title: 'Nothing here yet.',
          line: _filter == KitFilter.all
              ? 'Her wardrobe is empty.'
              : 'Nothing in ${_filter.label.toLowerCase()} she will show you.',
        )
      else
        for (var i = 0; i < shown.length; i += 2) ...[
          IntrinsicHeight(
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Expanded(
                  child: _OutfitCard(item: shown[i], onTap: _tapCard),
                ),
                const SizedBox(width: 8),
                Expanded(
                  child: i + 1 < shown.length
                      ? _OutfitCard(item: shown[i + 1], onTap: _tapCard)
                      : const SizedBox.shrink(),
                ),
              ],
            ),
          ),
          const SizedBox(height: 8),
        ],
      const SizedBox(height: 2),
      HudActionButton(
        key: const Key('kit-request'),
        label: 'Request an outfit',
        busy: _requesting,
        onPressed: _pick,
      ),
      if (_reply != null) ...[
        const SizedBox(height: 8),
        HudPanel(
          key: const Key('kit-reply'),
          accent: _reply!.$2 ? HudColors.cyan : HudColors.violet,
          child: Semantics(
            liveRegion: true,
            child: Text(
              _reply!.$2 ? _reply!.$1.toUpperCase() : '"${_reply!.$1}"',
              style: _reply!.$2
                  ? HudText.label(size: 11, color: HudColors.cyan)
                  : HudText.body(size: 15, color: HudColors.text),
            ),
          ),
        ),
      ],
      if (_log.isNotEmpty) ...[
        const SizedBox(height: 14),
        HudCollapsible(
          key: const Key('kit-worn'),
          title: 'Worn this month',
          child: Column(children: [for (final e in _log) _WornRow(entry: e)]),
        ),
      ],
    ];
  }
}

class _OutfitCard extends StatelessWidget {
  const _OutfitCard({required this.item, required this.onTap});
  final WardrobeItem item;
  final ValueChanged<WardrobeItem> onTap;

  @override
  Widget build(BuildContext context) {
    final locked = !item.unlocked;
    final equipped = item.current;
    final accent = equipped ? HudColors.violet : HudColors.cyan;
    final blurb = locked ? kKitLockedLine : item.blurb;
    final badge = equipped
        ? const HudBadge('Equipped', color: HudColors.violet, filled: true)
        : HudBadge(
            'LV ${item.unlockLevel}',
            color: locked ? HudColors.violet : HudColors.cyan,
          );
    final state = equipped
        ? 'Equipped'
        : locked
        ? 'Locked until level ${item.unlockLevel}'
        : 'Level ${item.unlockLevel}';

    final body = Padding(
      padding: const EdgeInsets.all(10),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            kitKindLine(item),
            style: HudText.label(
              size: 9.5,
              color: locked ? kHudDimText : HudColors.textDim,
            ),
          ),
          const SizedBox(height: 3),
          Text(
            item.name,
            maxLines: 2,
            overflow: TextOverflow.ellipsis,
            style: HudText.body(
              size: 17,
              weight: FontWeight.w700,
              color: locked ? kHudDimText : HudColors.text,
            ).copyWith(height: 1.1),
          ),
          if (blurb.isNotEmpty) ...[
            const SizedBox(height: 4),
            Text(
              blurb,
              key: locked ? ValueKey('kit-locked-${item.id}') : null,
              maxLines: 3,
              overflow: TextOverflow.ellipsis,
              style: HudText.body(
                size: 12.5,
                color: locked ? kHudDimText : HudColors.textDim,
              ).copyWith(height: 1.25),
            ),
          ],
          const Spacer(),
          const SizedBox(height: 8),
          badge,
        ],
      ),
    );

    return Semantics(
      button: true,
      label: '${item.name}, ${kitKindLine(item)}. $state. $blurb',
      excludeSemantics: true,
      child: ConstrainedBox(
        constraints: const BoxConstraints(minHeight: 118),
        child: Material(
          key: ValueKey('kit-card-${item.id}'),
          color: equipped
              ? HudColors.violetA(0.1)
              : HudColors.cyanA(locked ? 0.0 : 0.04),
          clipBehavior: Clip.antiAlias,
          shape: CutCornerBorder(
            side: BorderSide(
              color: equipped
                  ? HudColors.violet
                  : accent.withValues(alpha: 0.3),
            ),
          ),
          child: InkWell(
            onTap: () => onTap(item),
            child: locked
                ? HudStripes(color: HudColors.cyan, child: body)
                : body,
          ),
        ),
      ),
    );
  }
}

class _WornRow extends StatelessWidget {
  const _WornRow({required this.entry});
  final WardrobeLogEntry entry;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 5),
    child: Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        SizedBox(
          width: 58,
          child: Text(entry.shortDay, style: HudText.label(size: 10.5)),
        ),
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                entry.name,
                style: HudText.body(size: 14.5, weight: FontWeight.w600),
              ),
              if (entry.requested)
                HudLabel('Your request', color: HudColors.violet, size: 9.5)
              else if (entry.reason.isNotEmpty)
                Text(
                  entry.reason,
                  style: HudText.body(size: 12.5, color: HudColors.textDim),
                ),
            ],
          ),
        ),
      ],
    ),
  );
}
