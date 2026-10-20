import 'dart:async';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;

import '../models/memory.dart';
import '../services/memory_service.dart';
import '../services/session_auth.dart';
import '../theme/hud.dart';
import '../widgets/hud_parts.dart';
import 'hud_album_links.dart';

/// The album's side gutter.
const _kGutter = EdgeInsets.symmetric(horizontal: 16);

/// The album's filter chips over the archive's real categories.
enum AlbumFilter { all, herPov, withYou, moments }

extension AlbumFilterInfo on AlbumFilter {
  String get label => switch (this) {
    AlbumFilter.all => 'All',
    AlbumFilter.herPov => 'Her POV',
    AlbumFilter.withYou => 'With you',
    AlbumFilter.moments => 'Moments',
  };
}

/// Categories about him: the Commander himself and her Precious Memories.
/// Everything else (Tactical Operations, Mission Records, Squad Moments,
/// Quiet Hours, Dreams) is MOMENTS.
const kWithYouCategories = {'The Commander', 'Precious Memories'};

/// Categories the archive opens at a bond level, mirroring the server's
/// `memory_archive.CATEGORIES_BY_LEVEL`. Only the level label comes from
/// here: a category is shown locked only while /api/memories/categories
/// leaves it out.
const kAlbumCategoryUnlock = {
  'The Commander': 3,
  'Quiet Hours': 3,
  'Precious Memories': 6,
};

/// Her POV portraits are kept with the `her_pov` scene tag.
bool isHerPovMemory(Memory m) => m.sceneTags.contains('her_pov');

/// Dreams are journal text with no picture.
bool isDreamMemory(Memory m) => m.category == 'Dreams';

bool albumFilterMatches(AlbumFilter filter, Memory m) => switch (filter) {
  AlbumFilter.all => true,
  AlbumFilter.herPov => isHerPovMemory(m),
  AlbumFilter.withYou => kWithYouCategories.contains(m.category),
  AlbumFilter.moments => !kWithYouCategories.contains(m.category),
};

/// The card's kind line: HER POV, DREAM, WITH YOU or MOMENTS.
String albumKindLabel(Memory m) {
  if (isHerPovMemory(m)) return 'HER POV';
  if (isDreamMemory(m)) return 'DREAM';
  return kWithYouCategories.contains(m.category) ? 'WITH YOU' : 'MOMENTS';
}

/// Album tab of the Tactical HUD: her memory archive as a featured latest
/// entry over a two-column grid. An embedded tab body (no app bar, no back
/// button); the shell owns the Scaffold and the tab bar.
class HudAlbumScreen extends StatefulWidget {
  const HudAlbumScreen({
    super.key,
    required this.serverUrl,
    this.affectionLevel = 0,
    this.affectionLevelName = 'Cold Assessment',
    this.memoryService,
    this.imageClient,
    this.links,
    this.onAuthExpired,
    this.now,
  });

  final String serverUrl;
  final int affectionLevel;
  final String affectionLevelName;

  /// Archive transport; defaults to a real [MemoryService].
  final MemoryService? memoryService;

  /// Client for the authenticated thumbnails (`http.get` + `Image.memory`:
  /// `Image.network` headers are ignored on the web). Defaults to a real one.
  final http.Client? imageClient;

  /// Detail / Her POV / Thread navigation; defaults to the real screens.
  final AlbumLinks? links;

  /// 401: the token is dead. Defaults to the shared login bounce.
  final VoidCallback? onAuthExpired;

  /// Clock for TODAY / NEW; tests pin it.
  final DateTime Function()? now;

  @override
  State<HudAlbumScreen> createState() => _HudAlbumScreenState();
}

sealed class _GridItem {
  const _GridItem();
}

class _MemoryItem extends _GridItem {
  const _MemoryItem(this.memory);
  final Memory memory;
}

class _HerPovPrompt extends _GridItem {
  const _HerPovPrompt();
}

class _LockedItem extends _GridItem {
  const _LockedItem(this.category, this.level);
  final String category;
  final int level;
}

class _HudAlbumScreenState extends State<HudAlbumScreen> {
  static const int _pageSize = 50;

  late final MemoryService _service =
      widget.memoryService ?? MemoryService(serverUrl: widget.serverUrl);
  late final http.Client _images = widget.imageClient ?? http.Client();
  late final AlbumLinks _links = widget.links ?? defaultAlbumLinks();

  List<MemoryCategory>? _categories;
  List<MonthGroup> _timeline = const [];
  List<Memory> _memories = [];
  AlbumFilter _filter = AlbumFilter.all;
  String? _month;

  bool _loading = true;
  bool _error = false;
  bool _hasMore = false;
  bool _loadingMore = false;
  bool _loadMoreFailed = false;
  int _generation = 0;
  bool _authBounced = false;

  final Map<String, Future<Uint8List?>> _thumbs = {};

  /// Pages on as he nears the end of what is loaded. A sparse filter (few
  /// matches per page) keeps paging until the view is full or the archive
  /// is exhausted.
  late final ScrollController _scroll = ScrollController()
    ..addListener(_maybeLoadMore);

  void _maybeLoadMore() {
    if (!mounted || !_hasMore || _loadMoreFailed || !_scroll.hasClients) {
      return;
    }
    if (_scroll.position.extentAfter < 800) _loadMore();
  }

  @override
  void dispose() {
    _scroll.dispose();
    super.dispose();
  }

  DateTime get _now => (widget.now ?? DateTime.now)();

  @override
  void initState() {
    super.initState();
    _loadCategories();
    _loadTimeline();
    _loadFirstPage();
  }

  void _authExpired() {
    if (_authBounced || !mounted) return;
    _authBounced = true;
    (widget.onAuthExpired ?? handleAuthExpired)();
  }

  Future<void> _loadCategories() async {
    try {
      final cats = await _service.fetchCategories();
      if (mounted) setState(() => _categories = cats);
    } on MemoryServiceException catch (e) {
      if (e.isAuthExpired) _authExpired();
    } catch (_) {}
  }

  Future<void> _loadTimeline() async {
    try {
      final tl = await _service.fetchTimeline();
      if (mounted) setState(() => _timeline = tl);
    } on MemoryServiceException catch (e) {
      if (e.isAuthExpired) _authExpired();
    } catch (_) {}
  }

  /// First page for the current month. Filters are applied on the client,
  /// over every page, so one cursor serves all four chips.
  Future<void> _loadFirstPage() async {
    final generation = ++_generation;
    setState(() {
      _loading = true;
      _error = false;
      _hasMore = false;
      _loadingMore = false;
      _loadMoreFailed = false;
    });
    try {
      final page = await _service.fetchMemories(
        month: _month,
        limit: _pageSize,
      );
      if (!mounted || generation != _generation) return;
      setState(() {
        _memories = [...page];
        _loading = false;
        _hasMore = page.length == _pageSize;
      });
    } on MemoryServiceException catch (e) {
      if (e.isAuthExpired) {
        _authExpired();
        return;
      }
      if (!mounted || generation != _generation) return;
      setState(() {
        _loading = false;
        _error = true;
      });
    } catch (_) {
      if (!mounted || generation != _generation) return;
      setState(() {
        _loading = false;
        _error = true;
      });
    }
  }

  /// The page older than the oldest loaded entry, by its exact server
  /// timestamp and id (rows sharing a timestamp are neither skipped nor
  /// repeated). Idempotent while in flight; a month switch drops a late page.
  Future<void> _loadMore() async {
    if (_loadingMore || !_hasMore || _loading || _memories.isEmpty) return;
    final generation = _generation;
    final oldest = _memories.last;
    setState(() {
      _loadingMore = true;
      _loadMoreFailed = false;
    });
    try {
      final page = await _service.fetchMemories(
        month: _month,
        before:
            oldest.createdAtRaw ?? oldest.createdAt.toUtc().toIso8601String(),
        beforeId: oldest.id.isEmpty ? null : oldest.id,
        limit: _pageSize,
      );
      if (!mounted || generation != _generation) return;
      setState(() {
        final known = _memories.map((m) => m.id).toSet();
        _memories.addAll(page.where((m) => !known.contains(m.id)));
        _hasMore = page.length == _pageSize;
        _loadingMore = false;
      });
    } on MemoryServiceException catch (e) {
      if (e.isAuthExpired) {
        _authExpired();
        return;
      }
      if (!mounted || generation != _generation) return;
      setState(() {
        _loadingMore = false;
        _loadMoreFailed = true;
      });
    } catch (_) {
      if (!mounted || generation != _generation) return;
      setState(() {
        _loadingMore = false;
        _loadMoreFailed = true;
      });
    }
  }

  Future<void> _refresh() async {
    _thumbs.clear();
    await Future.wait([_loadCategories(), _loadTimeline(), _loadFirstPage()]);
  }

  void _selectFilter(AlbumFilter filter) {
    if (filter == _filter) return;
    setState(() => _filter = filter);
  }

  void _selectMonth(String? month) {
    if (month == _month) return;
    setState(() => _month = month);
    _loadFirstPage();
  }

  Future<Uint8List?> _thumb(String id) => _thumbs.putIfAbsent(id, () async {
    try {
      final r = await _images
          .get(
            Uri.parse('${widget.serverUrl}/api/memories/$id/thumbnail'),
            headers: {'Authorization': 'Bearer ${readAuthToken()}'},
          )
          .timeout(const Duration(seconds: 20));
      return r.statusCode == 200 ? r.bodyBytes : null;
    } catch (_) {
      return null;
    }
  });

  void _openDetail(Memory memory) {
    _links.openDetail(
      context,
      memory: memory,
      serverUrl: widget.serverUrl,
      onDiscarded: (id) {
        if (mounted) setState(() => _memories.removeWhere((m) => m.id == id));
      },
    );
  }

  Future<void> _openHerPov() async {
    await _links.openHerPov(
      context,
      serverUrl: widget.serverUrl,
      affectionLevel: widget.affectionLevel,
      affectionLevelName: widget.affectionLevelName,
      onAuthExpired: widget.onAuthExpired,
    );
    // She may have drawn one while he was there.
    if (mounted) _loadFirstPage();
  }

  void _openThread() => _links.openThread(
    context,
    serverUrl: widget.serverUrl,
    onAuthExpired: widget.onAuthExpired,
  );

  Future<void> _pickMonth() async {
    final picked = await showModalBottomSheet<String>(
      context: context,
      backgroundColor: HudColors.ground,
      shape: CutCornerBorder(side: BorderSide(color: HudColors.cyanA(0.4))),
      builder: (ctx) => SafeArea(
        child: ListView(
          shrinkWrap: true,
          padding: const EdgeInsets.symmetric(vertical: 12),
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 4, 16, 8),
              child: HudLabel('Timeline', color: HudColors.cyan),
            ),
            _monthTile(ctx, '', 'All months', null),
            for (final g in _timeline)
              _monthTile(ctx, g.month, g.label, g.count),
          ],
        ),
      ),
    );
    if (picked == null) return;
    _selectMonth(picked.isEmpty ? null : picked);
  }

  Widget _monthTile(BuildContext ctx, String value, String label, int? count) {
    final selected = (_month ?? '') == value;
    return Semantics(
      button: true,
      selected: selected,
      child: InkWell(
        key: ValueKey('album-month-${value.isEmpty ? 'all' : value}'),
        onTap: () => Navigator.of(ctx).pop(value),
        child: Container(
          constraints: const BoxConstraints(minHeight: 48),
          padding: const EdgeInsets.symmetric(horizontal: 16),
          alignment: Alignment.centerLeft,
          child: Row(
            children: [
              Expanded(
                child: Text(
                  label.toUpperCase(),
                  style: HudText.label(
                    size: 12,
                    color: selected ? HudColors.cyan : HudColors.text,
                  ),
                ),
              ),
              if (count != null) Text('$count', style: HudText.label(size: 11)),
            ],
          ),
        ),
      ),
    );
  }

  int get _entryCount {
    final cats = _categories;
    if (cats == null || cats.isEmpty) return _memories.length;
    for (final c in cats) {
      if (c.name == 'All') return c.count;
    }
    return cats.fold(0, (sum, c) => sum + c.count);
  }

  List<_LockedItem> get _locked {
    final cats = _categories;
    if (cats == null) return const [];
    final open = cats.map((c) => c.name).toSet();
    return [
      for (final e in kAlbumCategoryUnlock.entries)
        if (!open.contains(e.key) &&
            (_filter == AlbumFilter.all ||
                (_filter == AlbumFilter.withYou &&
                    kWithYouCategories.contains(e.key)) ||
                (_filter == AlbumFilter.moments &&
                    !kWithYouCategories.contains(e.key))))
          _LockedItem(e.key, e.value),
    ];
  }

  String _dateLine(DateTime d, {bool featured = false}) {
    final now = _now;
    final today = DateTime(now.year, now.month, now.day);
    final day = DateTime(d.year, d.month, d.day);
    final diff = today.difference(day).inDays;
    if (diff == 0) {
      final word = d.hour >= 18 ? 'TONIGHT' : 'TODAY';
      return featured ? '$word · ${hudClock(d)}' : word;
    }
    if (diff == 1) return featured ? 'YESTERDAY · ${hudClock(d)}' : 'YESTERDAY';
    return hudShortDate(d);
  }

  @override
  Widget build(BuildContext context) {
    final visible = _memories
        .where((m) => albumFilterMatches(_filter, m))
        .toList();
    final featured = visible.isEmpty ? null : visible.first;
    final rest = visible.skip(1).map<_GridItem>(_MemoryItem.new).toList();
    final items = <_GridItem>[
      if (_filter == AlbumFilter.herPov) const _HerPovPrompt(),
      ...rest,
      if (!_hasMore && !_loading && !_error) ...[
        if (_filter == AlbumFilter.all) const _HerPovPrompt(),
        ..._locked,
      ],
    ];
    final textScale = MediaQuery.textScalerOf(context).scale(1).clamp(1.0, 1.6);
    if (_hasMore && !_loadingMore && !_loadMoreFailed) {
      WidgetsBinding.instance.addPostFrameCallback((_) => _maybeLoadMore());
    }

    return HudBackground(
      child: RefreshIndicator(
        color: HudColors.cyan,
        backgroundColor: HudColors.ground,
        onRefresh: _refresh,
        child: CustomScrollView(
          key: const Key('album-scroll'),
          controller: _scroll,
          physics: const AlwaysScrollableScrollPhysics(),
          slivers: [
            // No side padding: _header() keeps the gutter itself so the
            // chip row can scroll edge to edge.
            SliverPadding(
              padding: const EdgeInsets.only(top: 16),
              sliver: SliverToBoxAdapter(child: _header()),
            ),
            if (_loading)
              const SliverToBoxAdapter(
                child: HudLoading('Retrieving memories...'),
              )
            else if (_error)
              SliverToBoxAdapter(
                child: HudErrorState(
                  key: const Key('album-error'),
                  title: 'Signal lost // archive unreachable',
                  line: '...the memories are still there. The link is not.',
                  onRetry: _refresh,
                ),
              )
            else ...[
              if (featured != null)
                SliverPadding(
                  padding: const EdgeInsets.fromLTRB(16, 12, 16, 0),
                  sliver: SliverToBoxAdapter(
                    child: _FeaturedCard(
                      key: ValueKey('album-featured-${featured.id}'),
                      memory: featured,
                      dateLine: _dateLine(featured.createdAt, featured: true),
                      isNew: _now.difference(featured.createdAt).inHours < 72,
                      thumb: isDreamMemory(featured)
                          ? null
                          : _thumb(featured.id),
                      onTap: () => _openDetail(featured),
                    ),
                  ),
                )
              else if (!_hasMore)
                SliverToBoxAdapter(child: _emptyState()),
              if (items.isNotEmpty)
                SliverPadding(
                  padding: const EdgeInsets.fromLTRB(16, 10, 16, 0),
                  sliver: SliverGrid(
                    gridDelegate: SliverGridDelegateWithFixedCrossAxisCount(
                      crossAxisCount: 2,
                      mainAxisSpacing: 8,
                      crossAxisSpacing: 8,
                      mainAxisExtent: 118 + 70 * textScale,
                    ),
                    delegate: SliverChildBuilderDelegate(
                      (context, i) => _gridCard(items[i]),
                      childCount: items.length,
                    ),
                  ),
                ),
              if (_hasMore) SliverToBoxAdapter(child: _loadMoreRow()),
            ],
            const SliverToBoxAdapter(child: SizedBox(height: 24)),
          ],
        ),
      ),
    );
  }

  Widget _header() => Column(
    crossAxisAlignment: CrossAxisAlignment.start,
    children: [
      Padding(
        padding: _kGutter,
        child: HudScreenHeader(
          kicker: 'Archive // Memory album',
          trailing: '$_entryCount ${_entryCount == 1 ? 'entry' : 'entries'}',
          trailingKey: const Key('album-count'),
          title: 'Album',
          subtitle: 'Her collection',
          actions: [
            HudIconAction(
              key: const Key('album-her-pov'),
              icon: Icons.brush_outlined,
              label: 'Her POV',
              color: HudColors.violet,
              onPressed: _openHerPov,
            ),
            HudIconAction(
              key: const Key('album-thread'),
              icon: Icons.forum_outlined,
              label: 'The Thread',
              onPressed: _openThread,
            ),
            if (_timeline.isNotEmpty)
              HudIconAction(
                key: const Key('album-months'),
                icon: Icons.calendar_month_outlined,
                label: 'Timeline by month',
                onPressed: _pickMonth,
              ),
          ],
        ),
      ),
      const SizedBox(height: 8),
      HudChipRow<AlbumFilter>(
        keyPrefix: 'album-filter',
        options: {for (final f in AlbumFilter.values) f: f.label},
        value: _filter,
        onChanged: _selectFilter,
        padding: _kGutter,
      ),
      if (_month != null) ...[
        const SizedBox(height: 2),
        Padding(
          padding: _kGutter,
          child: HudTapChip(
            key: const Key('album-month-clear'),
            label: '${_monthLabel(_month!)}  ×',
            selected: true,
            onTap: () => _selectMonth(null),
          ),
        ),
      ],
    ],
  );

  String _monthLabel(String month) {
    for (final g in _timeline) {
      if (g.month == month) return g.label;
    }
    return MonthGroup(month: month, count: 0).label;
  }

  Widget _emptyState() => switch (_filter) {
    AlbumFilter.all => const HudEmptyState(
      key: Key('album-empty'),
      icon: Icons.photo_album_outlined,
      title: 'No memories archived yet.',
      line: '...I will remember, when the time comes.',
    ),
    AlbumFilter.herPov => const HudEmptyState(
      key: Key('album-empty'),
      icon: Icons.brush_outlined,
      title: 'No Her POV yet.',
      line: 'Ask her to draw a moment.',
    ),
    _ => const HudEmptyState(
      key: Key('album-empty'),
      icon: Icons.photo_album_outlined,
      title: 'Nothing kept here yet.',
      line: 'She keeps what matters to her.',
    ),
  };

  Widget _gridCard(_GridItem item) => switch (item) {
    _MemoryItem(:final memory) => _MemoryCard(
      key: ValueKey('album-card-${memory.id}'),
      memory: memory,
      dateLine: '${_dateLine(memory.createdAt)} · ${albumKindLabel(memory)}',
      thumb: isDreamMemory(memory) ? null : _thumb(memory.id),
      onTap: () => _openDetail(memory),
    ),
    _HerPovPrompt() => _PromptCard(
      key: const Key('album-her-pov-card'),
      icon: Icons.brush_outlined,
      banner: "She hasn't shown you",
      kind: 'HER POV',
      caption: 'Ask her to draw a moment.',
      semanticLabel: 'Her POV. Ask her to draw a moment.',
      onTap: _openHerPov,
    ),
    _LockedItem(:final category, :final level) => _PromptCard(
      key: ValueKey('album-locked-$category'),
      icon: Icons.lock_outline,
      banner: "She hasn't shown you",
      kind: 'UNLOCKS AT LV $level',
      caption: category,
      semanticLabel: '$category. Locked until bond level $level.',
    ),
  };

  /// The foot of the grid while older pages remain: a spinner, or after a
  /// failure a tap-to-retry instead of hammering a dead link.
  Widget _loadMoreRow() {
    if (_loadMoreFailed) {
      return Padding(
        padding: const EdgeInsets.fromLTRB(16, 16, 16, 0),
        child: HudActionButton(
          key: const Key('album-load-more'),
          label: 'Signal dropped // tap to retrieve older',
          danger: true,
          onPressed: _loadMore,
        ),
      );
    }
    // No spinner until a page is actually in flight: an idle indicator
    // below the fold would tick (and keep the frame pump busy) for nothing.
    if (!_loadingMore) {
      return const SizedBox(key: Key('album-load-more'), height: 48);
    }
    return const HudLoading(
      'Retrieving older memories...',
      key: Key('album-load-more'),
    );
  }
}

/// The photo area of a card: the thumbnail, a dream's text panel, or a
/// placeholder while it loads / when it failed.
class _Thumb extends StatelessWidget {
  const _Thumb({required this.memory, required this.future});
  final Memory memory;
  final Future<Uint8List?>? future;

  @override
  Widget build(BuildContext context) {
    final f = future;
    if (f == null) {
      return ColoredBox(
        color: HudColors.violetA(0.08),
        child: Center(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const Icon(
                Icons.nights_stay_outlined,
                color: HudColors.violet,
                size: 22,
              ),
              const SizedBox(height: 6),
              HudLabel('Dream', color: HudColors.violet),
            ],
          ),
        ),
      );
    }
    return FutureBuilder<Uint8List?>(
      future: f,
      builder: (context, snap) {
        final bytes = snap.data;
        if (bytes != null) {
          return Image.memory(
            bytes,
            fit: BoxFit.cover,
            gaplessPlayback: true,
            errorBuilder: (_, _, _) =>
                _placeholder(Icons.image_not_supported_outlined),
          );
        }
        if (snap.connectionState == ConnectionState.done) {
          return _placeholder(Icons.image_not_supported_outlined);
        }
        return ColoredBox(
          color: HudColors.cyanA(0.04),
          child: const Center(
            child: SizedBox(
              width: 14,
              height: 14,
              child: CircularProgressIndicator(
                strokeWidth: 1.5,
                color: HudColors.cyan,
              ),
            ),
          ),
        );
      },
    );
  }

  Widget _placeholder(IconData icon) => ColoredBox(
    color: HudColors.cyanA(0.04),
    child: Center(child: Icon(icon, color: HudColors.textMuted, size: 22)),
  );
}

/// A cut-corner, clipped, tappable card surface.
class _CutCard extends StatelessWidget {
  const _CutCard({
    required this.child,
    required this.semanticLabel,
    this.onTap,
    this.edge = 0.3,
  });

  final Widget child;
  final String semanticLabel;
  final VoidCallback? onTap;
  final double edge;

  @override
  Widget build(BuildContext context) => Semantics(
    button: onTap != null,
    label: semanticLabel,
    excludeSemantics: true,
    child: Material(
      color: HudColors.cyanA(0.04),
      clipBehavior: Clip.antiAlias,
      shape: CutCornerBorder(side: BorderSide(color: HudColors.cyanA(edge))),
      child: InkWell(onTap: onTap, child: child),
    ),
  );
}

class _FeaturedCard extends StatelessWidget {
  const _FeaturedCard({
    super.key,
    required this.memory,
    required this.dateLine,
    required this.isNew,
    required this.thumb,
    required this.onTap,
  });

  final Memory memory;
  final String dateLine;
  final bool isNew;
  final Future<Uint8List?>? thumb;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final caption = memory.annotation ?? '';
    final line = '$dateLine · ${albumKindLabel(memory)}';
    return _CutCard(
      edge: 0.45,
      semanticLabel: '${isNew ? 'New. ' : 'Latest. '}$line. $caption',
      onTap: onTap,
      child: SizedBox(
        height: 190,
        child: Stack(
          fit: StackFit.expand,
          children: [
            _Thumb(memory: memory, future: thumb),
            const DecoratedBox(
              decoration: BoxDecoration(
                gradient: LinearGradient(
                  begin: Alignment.bottomCenter,
                  end: Alignment.center,
                  colors: [Color(0xEB06080C), Color(0x0006080C)],
                ),
              ),
            ),
            Positioned(
              top: 10,
              left: 10,
              child: HudBadge(
                isNew ? 'New' : 'Latest',
                key: const Key('album-featured-tag'),
                color: HudColors.violet,
                filled: true,
              ),
            ),
            Positioned(
              left: 12,
              right: 12,
              bottom: 10,
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(line, style: HudText.label(color: HudColors.textDim)),
                  if (caption.isNotEmpty) ...[
                    const SizedBox(height: 2),
                    Text(
                      caption,
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: HudText.body(size: 16, weight: FontWeight.w600),
                    ),
                  ],
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _MemoryCard extends StatelessWidget {
  const _MemoryCard({
    super.key,
    required this.memory,
    required this.dateLine,
    required this.thumb,
    required this.onTap,
  });

  final Memory memory;
  final String dateLine;
  final Future<Uint8List?>? thumb;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final caption = memory.annotation ?? '';
    return _CutCard(
      semanticLabel: '$dateLine. $caption',
      onTap: onTap,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          SizedBox(
            height: 118,
            child: _Thumb(memory: memory, future: thumb),
          ),
          Expanded(
            child: _CardText(line: dateLine, caption: caption),
          ),
        ],
      ),
    );
  }
}

class _CardText extends StatelessWidget {
  const _CardText({required this.line, required this.caption});
  final String line;
  final String caption;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.fromLTRB(9, 7, 9, 7),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          line,
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          style: HudText.label(size: 9.5, color: HudColors.textDim),
        ),
        const SizedBox(height: 2),
        Flexible(
          child: Text(
            caption,
            maxLines: 2,
            overflow: TextOverflow.ellipsis,
            style: HudText.body(
              size: 13.5,
              weight: FontWeight.w600,
              color: HudColors.text,
            ).copyWith(height: 1.2),
          ),
        ),
      ],
    ),
  );
}

/// The striped, violet "she hasn't shown you" card: a locked category, or
/// the way into Her POV.
class _PromptCard extends StatelessWidget {
  const _PromptCard({
    super.key,
    required this.icon,
    required this.banner,
    required this.kind,
    required this.caption,
    required this.semanticLabel,
    this.onTap,
  });

  final IconData icon;
  final String banner;
  final String kind;
  final String caption;
  final String semanticLabel;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) => _CutCard(
    semanticLabel: semanticLabel,
    onTap: onTap,
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        SizedBox(
          height: 118,
          child: HudStripes(
            child: Center(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Icon(icon, color: HudColors.violet, size: 22),
                  const SizedBox(height: 6),
                  Padding(
                    padding: const EdgeInsets.symmetric(horizontal: 6),
                    child: Text(
                      banner.toUpperCase(),
                      textAlign: TextAlign.center,
                      style: HudText.label(size: 9.5, color: HudColors.violet),
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
        Expanded(
          child: _CardText(line: kind, caption: caption),
        ),
      ],
    ),
  );
}
