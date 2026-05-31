import 'dart:typed_data';
import 'dart:js_interop';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:web/web.dart' as web;
import '../main.dart';
import '../models/memory.dart';
import '../services/memory_service.dart';
import '../widgets/memory_timeline_entry.dart';

class MemoryArchiveScreen extends StatefulWidget {
  final String serverUrl;
  final int affectionLevel;
  final String affectionLevelName;

  const MemoryArchiveScreen({
    super.key,
    required this.serverUrl,
    this.affectionLevel = 0,
    this.affectionLevelName = 'Cold Assessment',
  });

  @override
  State<MemoryArchiveScreen> createState() => _MemoryArchiveScreenState();
}

class _MemoryArchiveScreenState extends State<MemoryArchiveScreen> {
  late final MemoryService _service;
  String _authToken = '';

  List<MemoryCategory> _categories = [];
  List<MonthGroup> _timeline = [];
  List<Memory> _memories = [];
  String _selectedCategory = 'All';
  String? _selectedMonth;  // null = all months
  bool _loadingCategories = true;
  bool _loadingMemories = true;

  @override
  void initState() {
    super.initState();
    try {
      _authToken = web.window.localStorage.getItem('klukai_token') ?? '';
    } catch (_) {}
    _service = MemoryService(serverUrl: widget.serverUrl);
    _loadCategories();
    _loadTimeline();
    _loadMemories();
  }

  Future<void> _loadCategories() async {
    try {
      final cats = await _service.fetchCategories();
      if (mounted) {
        setState(() {
          _categories = cats;
          _loadingCategories = false;
        });
      }
    } catch (_) {
      if (mounted) setState(() => _loadingCategories = false);
    }
  }

  Future<void> _loadTimeline() async {
    try {
      final tl = await _service.fetchTimeline();
      if (mounted) setState(() => _timeline = tl);
    } catch (_) {}
  }

  Future<void> _loadMemories({String? category, String? month}) async {
    setState(() => _loadingMemories = true);
    try {
      final mems = await _service.fetchMemories(
        category: category == 'All' ? null : category,
        month: month,
        limit: 50,
      );
      if (mounted) {
        setState(() {
          _memories = mems;
          _loadingMemories = false;
        });
      }
    } catch (_) {
      if (mounted) setState(() => _loadingMemories = false);
    }
  }

  void _selectCategory(String cat) {
    if (_selectedCategory == cat) return;
    setState(() => _selectedCategory = cat);
    _loadMemories(category: cat, month: _selectedMonth);
  }

  void _selectMonth(String? month) {
    if (_selectedMonth == month) return;
    setState(() => _selectedMonth = month);
    _loadMemories(category: _selectedCategory, month: month);
  }

  int get _totalKept {
    if (_categories.isEmpty) return _memories.length;
    return _categories.fold(0, (sum, c) => sum + c.count);
  }

  String get _selectedLabel {
    if (_selectedCategory == 'All') return 'ALL MEMORIES';
    return _selectedCategory.toUpperCase();
  }

  int get _selectedCount {
    if (_selectedCategory == 'All') return _totalKept;
    try {
      return _categories.firstWhere((c) => c.name == _selectedCategory).count;
    } catch (_) {
      return _memories.length;
    }
  }

  void _openMemoryDetail(Memory memory) {
    showDialog(
      context: context,
      barrierColor: Colors.black.withValues(alpha: 0.88),
      builder: (_) => _MemoryDetailDialog(
        memory: memory,
        serverUrl: widget.serverUrl,
        authToken: _authToken,
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: GFL2Colors.background,
      appBar: _buildAppBar(),
      body: LayoutBuilder(
        builder: (context, constraints) {
          if (constraints.maxWidth > 600) {
            return _buildDesktopLayout();
          } else {
            return _buildMobileLayout();
          }
        },
      ),
    );
  }

  PreferredSizeWidget _buildAppBar() {
    return AppBar(
      backgroundColor: GFL2Colors.surface,
      elevation: 0,
      leading: IconButton(
        icon: const Icon(Icons.arrow_back, color: GFL2Colors.primary, size: 20),
        onPressed: () => Navigator.of(context).pop(),
      ),
      title: const Text(
        'MEMORY ARCHIVE',
        style: TextStyle(
          color: GFL2Colors.textPrimary,
          fontSize: 13,
          fontWeight: FontWeight.w700,
          letterSpacing: 2.0,
          fontFamily: 'monospace',
        ),
      ),
      bottom: PreferredSize(
        preferredSize: const Size.fromHeight(1),
        child: Container(
          height: 1,
          color: GFL2Colors.border.withValues(alpha: 0.4),
        ),
      ),
    );
  }

  // ── Desktop Layout ──────────────────────────────────────────────────────────

  Widget _buildDesktopLayout() {
    return Row(
      children: [
        _buildDesktopSidebar(),
        Container(width: 1, color: GFL2Colors.border.withValues(alpha: 0.4)),
        Expanded(child: _buildTimeline(isCompact: false)),
      ],
    );
  }

  Widget _buildDesktopSidebar() {
    return SizedBox(
      width: 180,
      child: Container(
        color: GFL2Colors.surface,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            // Section header
            Padding(
              padding: const EdgeInsets.fromLTRB(14, 16, 14, 10),
              child: Text(
                'CATEGORIES',
                style: TextStyle(
                  color: GFL2Colors.textDim.withValues(alpha: 0.6),
                  fontSize: 9,
                  fontFamily: 'monospace',
                  letterSpacing: 1.5,
                  fontWeight: FontWeight.w700,
                ),
              ),
            ),
            // Category list
            if (_loadingCategories)
              const Padding(
                padding: EdgeInsets.all(16),
                child: SizedBox(
                  width: 16,
                  height: 16,
                  child: CircularProgressIndicator(strokeWidth: 1.5, color: GFL2Colors.primary),
                ),
              )
            else ...[
              ..._categories.map((cat) => _buildSidebarItem(cat.name, cat.count)),
            ],
            // Divider
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
              child: Container(height: 1, color: GFL2Colors.border.withValues(alpha: 0.4)),
            ),
            _buildSidebarItem('All', _totalKept),
            // Month/year section
            if (_timeline.isNotEmpty) ...[
              Padding(
                padding: const EdgeInsets.fromLTRB(14, 12, 14, 6),
                child: Text(
                  'TIMELINE',
                  style: TextStyle(
                    color: GFL2Colors.textDim.withValues(alpha: 0.6),
                    fontSize: 9,
                    fontFamily: 'monospace',
                    letterSpacing: 1.5,
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ),
              _buildSidebarMonthItem(null, 'All Months', _totalKept),
              ..._timeline.map((g) => _buildSidebarMonthItem(g.month, g.label, g.count)),
            ],
            const Spacer(),
            // Archive stats
            Padding(
              padding: const EdgeInsets.fromLTRB(14, 8, 14, 16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Container(height: 1, color: GFL2Colors.border.withValues(alpha: 0.4)),
                  const SizedBox(height: 10),
                  Text(
                    'ARCHIVE STATUS',
                    style: TextStyle(
                      color: GFL2Colors.textDim.withValues(alpha: 0.5),
                      fontSize: 9,
                      fontFamily: 'monospace',
                      letterSpacing: 1.5,
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                  const SizedBox(height: 8),
                  Text(
                    '$_totalKept KEPT',
                    style: const TextStyle(
                      color: GFL2Colors.textPrimary,
                      fontSize: 11,
                      fontFamily: 'monospace',
                      letterSpacing: 1.0,
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                  const SizedBox(height: 4),
                  Text(
                    'BONDED // LV.${widget.affectionLevel}',
                    style: TextStyle(
                      color: GFL2Colors.affinity.withValues(alpha: 0.7),
                      fontSize: 10,
                      fontFamily: 'monospace',
                      letterSpacing: 0.8,
                    ),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildSidebarItem(String name, int count) {
    final isSelected = _selectedCategory == name;
    return GestureDetector(
      onTap: () => _selectCategory(name),
      child: Container(
        width: double.infinity,
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 9),
        decoration: BoxDecoration(
          color: isSelected
              ? GFL2Colors.affinity.withValues(alpha: 0.08)
              : Colors.transparent,
          border: Border(
            left: BorderSide(
              color: isSelected ? GFL2Colors.affinity : Colors.transparent,
              width: 2,
            ),
          ),
        ),
        child: Row(
          children: [
            Container(
              width: 5,
              height: 5,
              decoration: BoxDecoration(
                shape: BoxShape.circle,
                color: isSelected
                    ? GFL2Colors.affinity
                    : GFL2Colors.textDim.withValues(alpha: 0.4),
              ),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: Text(
                name,
                style: TextStyle(
                  color: isSelected ? GFL2Colors.textPrimary : GFL2Colors.textDim,
                  fontSize: 11,
                  fontFamily: 'monospace',
                  letterSpacing: 0.6,
                  fontWeight: isSelected ? FontWeight.w700 : FontWeight.w400,
                ),
                overflow: TextOverflow.ellipsis,
              ),
            ),
            Text(
              '$count',
              style: TextStyle(
                color: isSelected
                    ? GFL2Colors.affinity.withValues(alpha: 0.8)
                    : GFL2Colors.textDim.withValues(alpha: 0.4),
                fontSize: 10,
                fontFamily: 'monospace',
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildSidebarMonthItem(String? month, String label, int count) {
    final isSelected = _selectedMonth == month;
    return GestureDetector(
      onTap: () => _selectMonth(month),
      child: Container(
        width: double.infinity,
        padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 7),
        decoration: BoxDecoration(
          color: isSelected
              ? GFL2Colors.primary.withValues(alpha: 0.08)
              : Colors.transparent,
          border: Border(
            left: BorderSide(
              color: isSelected ? GFL2Colors.primary : Colors.transparent,
              width: 2,
            ),
          ),
        ),
        child: Row(
          children: [
            Expanded(
              child: Text(
                label,
                style: TextStyle(
                  color: isSelected ? GFL2Colors.textPrimary : GFL2Colors.textDim.withValues(alpha: 0.7),
                  fontSize: 10,
                  fontFamily: 'monospace',
                  letterSpacing: 0.4,
                  fontWeight: isSelected ? FontWeight.w600 : FontWeight.w400,
                ),
                overflow: TextOverflow.ellipsis,
              ),
            ),
            Text(
              '$count',
              style: TextStyle(
                color: isSelected
                    ? GFL2Colors.primary.withValues(alpha: 0.8)
                    : GFL2Colors.textDim.withValues(alpha: 0.3),
                fontSize: 9,
                fontFamily: 'monospace',
              ),
            ),
          ],
        ),
      ),
    );
  }

  // ── Mobile Layout ───────────────────────────────────────────────────────────

  Widget _buildMobileLayout() {
    return Column(
      children: [
        _buildMobileCategoryTabs(),
        if (_timeline.isNotEmpty) _buildMonthTabs(),
        Container(height: 1, color: GFL2Colors.border.withValues(alpha: 0.4)),
        Expanded(child: _buildTimeline(isCompact: true)),
      ],
    );
  }

  Widget _buildMobileCategoryTabs() {
    final allItems = ['All', ..._categories.map((c) => c.name)];

    return Container(
      color: GFL2Colors.surface,
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      child: _loadingCategories
          ? const SizedBox(
              height: 32,
              child: Center(
                child: SizedBox(
                  width: 16,
                  height: 16,
                  child: CircularProgressIndicator(
                    strokeWidth: 1.5,
                    color: GFL2Colors.primary,
                  ),
                ),
              ),
            )
          : SizedBox(
              height: 32,
              child: ListView.separated(
                scrollDirection: Axis.horizontal,
                physics: const ClampingScrollPhysics(),
                itemCount: allItems.length,
                separatorBuilder: (context2, i2) => const SizedBox(width: 6),
                itemBuilder: (context2, i) => _buildMobileTab(allItems[i]),
              ),
            ),
    );
  }

  Widget _buildMobileTab(String name) {
    final isSelected = _selectedCategory == name;
    return GestureDetector(
      onTap: () => _selectCategory(name),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
        decoration: BoxDecoration(
          color: isSelected
              ? GFL2Colors.affinity.withValues(alpha: 0.15)
              : Colors.transparent,
          borderRadius: BorderRadius.circular(3),
          border: Border.all(
            color: isSelected
                ? GFL2Colors.affinity.withValues(alpha: 0.6)
                : GFL2Colors.border.withValues(alpha: 0.4),
          ),
        ),
        child: Text(
          name.toUpperCase(),
          style: TextStyle(
            color: isSelected ? GFL2Colors.affinity : GFL2Colors.textDim,
            fontSize: 10,
            fontFamily: 'monospace',
            letterSpacing: 0.8,
            fontWeight: isSelected ? FontWeight.w700 : FontWeight.w400,
          ),
        ),
      ),
    );
  }

  Widget _buildMonthTabs() {
    return Container(
      color: GFL2Colors.surface,
      padding: const EdgeInsets.fromLTRB(12, 0, 12, 8),
      child: SizedBox(
        height: 28,
        child: ListView.separated(
          scrollDirection: Axis.horizontal,
          physics: const ClampingScrollPhysics(),
          itemCount: _timeline.length + 1, // +1 for "All" tab
          separatorBuilder: (_, __) => const SizedBox(width: 5),
          itemBuilder: (_, i) {
            if (i == 0) return _buildMonthTab(null, 'ALL');
            final group = _timeline[i - 1];
            return _buildMonthTab(group.month, '${group.label} (${group.count})');
          },
        ),
      ),
    );
  }

  Widget _buildMonthTab(String? month, String label) {
    final isSelected = _selectedMonth == month;
    return GestureDetector(
      onTap: () => _selectMonth(month),
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
        decoration: BoxDecoration(
          color: isSelected
              ? GFL2Colors.primary.withValues(alpha: 0.15)
              : Colors.transparent,
          borderRadius: BorderRadius.circular(3),
          border: Border.all(
            color: isSelected
                ? GFL2Colors.primary.withValues(alpha: 0.6)
                : GFL2Colors.border.withValues(alpha: 0.3),
          ),
        ),
        child: Text(
          label,
          style: TextStyle(
            color: isSelected ? GFL2Colors.primary : GFL2Colors.textDim.withValues(alpha: 0.6),
            fontSize: 9,
            fontFamily: 'monospace',
            letterSpacing: 0.6,
            fontWeight: isSelected ? FontWeight.w700 : FontWeight.w400,
          ),
        ),
      ),
    );
  }

  // ── Shared Timeline ─────────────────────────────────────────────────────────

  Widget _buildTimeline({required bool isCompact}) {
    if (_loadingMemories) {
      return const Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            CircularProgressIndicator(strokeWidth: 1.5, color: GFL2Colors.primary),
            SizedBox(height: 12),
            Text(
              'RETRIEVING MEMORIES...',
              style: TextStyle(
                color: GFL2Colors.textDim,
                fontSize: 10,
                fontFamily: 'monospace',
                letterSpacing: 1.2,
              ),
            ),
          ],
        ),
      );
    }

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        // Timeline header (desktop only — has space)
        if (!isCompact)
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 14, 16, 10),
            child: Row(
              children: [
                Text(
                  _selectedLabel,
                  style: const TextStyle(
                    color: GFL2Colors.textPrimary,
                    fontSize: 12,
                    fontFamily: 'monospace',
                    fontWeight: FontWeight.w700,
                    letterSpacing: 1.5,
                  ),
                ),
                const SizedBox(width: 8),
                Text(
                  '// $_selectedCount ENTRIES',
                  style: TextStyle(
                    color: GFL2Colors.textDim.withValues(alpha: 0.5),
                    fontSize: 11,
                    fontFamily: 'monospace',
                    letterSpacing: 1.0,
                  ),
                ),
              ],
            ),
          ),
        if (!isCompact)
          Container(height: 1, color: GFL2Colors.border.withValues(alpha: 0.3)),
        // Memory list
        Expanded(
          child: _memories.isEmpty
              ? _buildEmptyState()
              : ListView.builder(
                  physics: const ClampingScrollPhysics(
                    parent: AlwaysScrollableScrollPhysics(),
                  ),
                  padding: const EdgeInsets.fromLTRB(16, 16, 16, 24),
                  itemCount: _memories.length,
                  itemBuilder: (_, i) => MemoryTimelineEntry(
                    memory: _memories[i],
                    serverUrl: widget.serverUrl,
                    authToken: _authToken,
                    isCompact: isCompact,
                    onTap: () => _openMemoryDetail(_memories[i]),
                  ),
                ),
        ),
      ],
    );
  }

  Widget _buildEmptyState() {
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(
            Icons.photo_album_outlined,
            color: GFL2Colors.textDim.withValues(alpha: 0.2),
            size: 48,
          ),
          const SizedBox(height: 14),
          Text(
            'NO MEMORIES ARCHIVED YET.',
            style: TextStyle(
              color: GFL2Colors.textDim.withValues(alpha: 0.5),
              fontSize: 11,
              fontFamily: 'monospace',
              letterSpacing: 1.5,
              fontWeight: FontWeight.w600,
            ),
          ),
          const SizedBox(height: 6),
          Text(
            '...I will remember, when the time comes.',
            style: TextStyle(
              color: GFL2Colors.textDim.withValues(alpha: 0.3),
              fontSize: 12,
              fontStyle: FontStyle.italic,
            ),
          ),
        ],
      ),
    );
  }
}

// ── Full-Screen Detail Dialog ────────────────────────────────────────────────

class _MemoryDetailDialog extends StatefulWidget {
  final Memory memory;
  final String serverUrl;
  final String authToken;

  const _MemoryDetailDialog({
    required this.memory,
    required this.serverUrl,
    required this.authToken,
  });

  @override
  State<_MemoryDetailDialog> createState() => _MemoryDetailDialogState();
}

class _MemoryDetailDialogState extends State<_MemoryDetailDialog> {
  bool _downloading = false;
  Uint8List? _imageBytes;
  bool _imageFailed = false;

  String get _imageUrl =>
      '${widget.serverUrl}/api/memories/${widget.memory.id}/image';

  @override
  void initState() {
    super.initState();
    _loadImage();
  }

  Future<void> _loadImage() async {
    try {
      final response = await http.get(
        Uri.parse(_imageUrl),
        headers: {'Authorization': 'Bearer ${widget.authToken}'},
      ).timeout(const Duration(seconds: 20));
      if (response.statusCode == 200 && mounted) {
        setState(() => _imageBytes = response.bodyBytes);
      } else if (mounted) {
        setState(() => _imageFailed = true);
      }
    } catch (_) {
      if (mounted) setState(() => _imageFailed = true);
    }
  }

  void _showSnack(String text) {
    if (!mounted) return;
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(
        content: Text(
          text,
          style: const TextStyle(
            fontFamily: 'monospace',
            letterSpacing: 1.0,
            fontSize: 12,
          ),
        ),
        backgroundColor: GFL2Colors.surface,
        behavior: SnackBarBehavior.floating,
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(4),
          side: BorderSide(color: GFL2Colors.primary.withValues(alpha: 0.4)),
        ),
      ),
    );
  }

  Future<void> _download() async {
    setState(() => _downloading = true);
    try {
      // Reuse the already-loaded full image when present; otherwise fetch it.
      Uint8List? bytes = _imageBytes;
      if (bytes == null) {
        final response = await http.get(
          Uri.parse(_imageUrl),
          headers: {'Authorization': 'Bearer ${widget.authToken}'},
        ).timeout(const Duration(seconds: 30));
        if (response.statusCode == 200) bytes = response.bodyBytes;
      }
      if (bytes == null) {
        _showSnack('DOWNLOAD FAILED');
        return;
      }
      // Real browser download via blob + anchor (same path the chat image
      // bubble uses). The full image is served as WebP; sniff the RIFF magic
      // so the saved file gets the right extension/MIME.
      final isWebp = bytes.length > 12 &&
          bytes[0] == 0x52 && bytes[1] == 0x49 &&
          bytes[2] == 0x46 && bytes[3] == 0x46; // "RIFF"
      final ext = isWebp ? 'webp' : 'png';
      final mime = isWebp ? 'image/webp' : 'image/png';
      final idShort =
          widget.memory.id.substring(0, widget.memory.id.length.clamp(0, 8));
      final blob = web.Blob([bytes.toJS].toJS, web.BlobPropertyBag(type: mime));
      final url = web.URL.createObjectURL(blob);
      final a = web.document.createElement('a') as web.HTMLAnchorElement
        ..href = url
        ..download = 'klukai_memory_$idShort.$ext';
      a.click();
      web.URL.revokeObjectURL(url);
      _showSnack('MEMORY DOWNLOADED');
    } catch (e) {
      debugPrint('Download failed: $e');
      _showSnack('DOWNLOAD FAILED');
    } finally {
      if (mounted) setState(() => _downloading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Dialog(
      backgroundColor: Colors.transparent,
      insetPadding: const EdgeInsets.all(16),
      child: GestureDetector(
        onTap: () => Navigator.of(context).pop(),
        child: Container(
          color: Colors.transparent,
          child: Center(
            child: GestureDetector(
              // Prevent tap-inside from closing
              onTap: () {},
              child: Container(
                constraints: const BoxConstraints(maxWidth: 600),
                decoration: BoxDecoration(
                  color: GFL2Colors.surface,
                  borderRadius: BorderRadius.circular(4),
                  border: Border.all(
                    color: GFL2Colors.border.withValues(alpha: 0.5),
                  ),
                ),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    // Header bar
                    Container(
                      padding: const EdgeInsets.fromLTRB(14, 10, 8, 10),
                      decoration: BoxDecoration(
                        border: Border(
                          bottom: BorderSide(
                            color: GFL2Colors.border.withValues(alpha: 0.4),
                          ),
                        ),
                      ),
                      child: Row(
                        children: [
                          Text(
                            'MEMORY // ${widget.memory.id.substring(0, widget.memory.id.length.clamp(0, 8)).toUpperCase()}',
                            style: TextStyle(
                              color: GFL2Colors.primary.withValues(alpha: 0.7),
                              fontSize: 10,
                              fontFamily: 'monospace',
                              letterSpacing: 1.2,
                              fontWeight: FontWeight.w600,
                            ),
                          ),
                          const Spacer(),
                          IconButton(
                            onPressed: () => Navigator.of(context).pop(),
                            icon: const Icon(Icons.close, color: GFL2Colors.textDim, size: 18),
                            padding: EdgeInsets.zero,
                            constraints: const BoxConstraints(minWidth: 32, minHeight: 32),
                          ),
                        ],
                      ),
                    ),
                    // Full image
                    Flexible(
                      child: ConstrainedBox(
                        constraints: BoxConstraints(
                          maxHeight: MediaQuery.of(context).size.height * 0.6,
                        ),
                        child: _imageBytes != null
                            ? Image.memory(_imageBytes!, fit: BoxFit.contain)
                            : _imageFailed
                                ? Container(
                                    height: 200,
                                    color: GFL2Colors.panel,
                                    child: Center(
                                      child: Icon(
                                        Icons.broken_image_outlined,
                                        color: GFL2Colors.textDim.withValues(alpha: 0.3),
                                        size: 48,
                                      ),
                                    ),
                                  )
                                : Container(
                                    height: 200,
                                    color: GFL2Colors.panel,
                                    child: Center(
                                      child: CircularProgressIndicator(
                                        strokeWidth: 1.5,
                                        color: GFL2Colors.primary.withValues(alpha: 0.5),
                                      ),
                                    ),
                                  ),
                      ),
                    ),
                    // Annotation
                    if (widget.memory.annotation != null &&
                        widget.memory.annotation!.isNotEmpty)
                      Padding(
                        padding: const EdgeInsets.fromLTRB(14, 12, 14, 0),
                        child: Text(
                          '"${widget.memory.annotation}"',
                          style: const TextStyle(
                            color: GFL2Colors.textPrimary,
                            fontSize: 13,
                            fontStyle: FontStyle.italic,
                            height: 1.5,
                          ),
                          textAlign: TextAlign.center,
                        ),
                      ),
                    // Download button
                    Padding(
                      padding: const EdgeInsets.fromLTRB(14, 12, 14, 14),
                      child: SizedBox(
                        width: double.infinity,
                        child: TextButton.icon(
                          onPressed: _downloading ? null : _download,
                          style: TextButton.styleFrom(
                            backgroundColor: GFL2Colors.background,
                            padding: const EdgeInsets.symmetric(vertical: 10),
                            shape: RoundedRectangleBorder(
                              borderRadius: BorderRadius.circular(4),
                              side: BorderSide(
                                color: GFL2Colors.border.withValues(alpha: 0.5),
                              ),
                            ),
                          ),
                          icon: _downloading
                              ? const SizedBox(
                                  width: 14,
                                  height: 14,
                                  child: CircularProgressIndicator(
                                    strokeWidth: 1.5,
                                    color: GFL2Colors.primary,
                                  ),
                                )
                              : const Icon(
                                  Icons.download_outlined,
                                  color: GFL2Colors.primary,
                                  size: 16,
                                ),
                          label: Text(
                            _downloading ? 'DOWNLOADING...' : 'DOWNLOAD',
                            style: const TextStyle(
                              color: GFL2Colors.primary,
                              fontSize: 11,
                              fontFamily: 'monospace',
                              letterSpacing: 1.2,
                              fontWeight: FontWeight.w600,
                            ),
                          ),
                        ),
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
