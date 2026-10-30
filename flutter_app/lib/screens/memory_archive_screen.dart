import 'dart:typed_data';
import 'dart:js_interop';

import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:web/web.dart' as web;

import '../main.dart';
import '../models/memory.dart';
import '../services/memory_service.dart';
import '../services/session_auth.dart';
import '../widgets/memory_timeline_entry.dart';
import 'thread_screen.dart';

part 'archive_sidebar.dart';

part 'archive_timeline.dart';

part 'archive_detail.dart';

class MemoryArchiveScreen extends StatefulWidget {
  final String serverUrl;
  final int affectionLevel;
  final String affectionLevelName;

  /// Archive transport. Defaults to a real [MemoryService] in production;
  /// tests may inject a fake so the screen can be pumped without a backend.
  final MemoryService? memoryService;

  /// What to do on a 401. Defaults to the shared login bounce; tests inject
  /// a recorder so the browser is never actually navigated.
  final VoidCallback? onAuthExpired;

  const MemoryArchiveScreen({
    super.key,
    required this.serverUrl,
    this.affectionLevel = 0,
    this.affectionLevelName = 'Cold Assessment',
    this.memoryService,
    this.onAuthExpired,
  });

  @override
  State<MemoryArchiveScreen> createState() => _MemoryArchiveScreenState();
}

class _MemoryArchiveScreenState extends State<MemoryArchiveScreen> {
  late final MemoryService _service;
  String _authToken = '';

  List<MemoryCategory> _categories = [];
  bool _categoriesFailed = false;
  List<MonthGroup> _timeline = [];
  bool _timelineFailed = false;
  List<Memory> _memories = [];
  String _selectedCategory = 'All';
  String? _selectedMonth; // null = all months
  bool _loadingCategories = true;
  bool _loadingMemories = true;
  bool _memoriesError = false;

  /// Paging. The header count comes from /api/memories/categories, so the
  /// list must be able to reach every one of those rows, not stop at the
  /// first page while the sidebar says "120 KEPT". A full page arms a
  /// sentinel row at the end of the list that fetches the next page
  /// (`before` = oldest loaded createdAt) when it scrolls into view.
  static const int _pageSize = 50;
  bool _hasMore = false;
  bool _loadingMore = false;
  bool _loadMoreFailed = false;

  /// Request-generation counter: each _loadMemories call bumps it, and
  /// responses for any generation other than the latest are discarded — so a
  /// slow response for an old filter can never clobber a quick filter switch.
  int _loadGeneration = 0;

  @override
  void initState() {
    super.initState();
    try {
      _authToken = web.window.localStorage.getItem('klukai_token') ?? '';
    } catch (_) {}
    _service =
        widget.memoryService ?? MemoryService(serverUrl: widget.serverUrl);
    _loadCategories();
    _loadTimeline();
    _loadMemories();
  }

  /// The token is dead: clear it and return to login (the shared bounce in
  /// session_auth) instead of rendering a fake-empty archive.
  void _handleAuthExpired() => (widget.onAuthExpired ?? handleAuthExpired)();

  Future<void> _loadCategories() async {
    try {
      final cats = await _service.fetchCategories();
      if (mounted) {
        setState(() {
          // The server ends the list with its own {"name": "All"} total. The
          // screen draws its own All and sums the real categories: kept in,
          // that row showed twice and doubled every total.
          _categories = cats.where((c) => c.name != 'All').toList();
          _loadingCategories = false;
          _categoriesFailed = false;
        });
      }
    } on MemoryServiceException catch (e) {
      if (e.isAuthExpired) {
        _handleAuthExpired();
        return;
      }
      if (mounted) {
        setState(() {
          _loadingCategories = false;
          _categoriesFailed = true;
        });
      }
    } catch (_) {
      if (mounted) {
        setState(() {
          _loadingCategories = false;
          _categoriesFailed = true;
        });
      }
    }
  }

  Future<void> _loadTimeline() async {
    try {
      final tl = await _service.fetchTimeline();
      if (mounted) {
        setState(() {
          _timeline = tl;
          _timelineFailed = false;
        });
      }
    } on MemoryServiceException catch (e) {
      if (e.isAuthExpired) {
        _handleAuthExpired();
        return;
      }
      if (mounted) setState(() => _timelineFailed = true);
    } catch (_) {
      if (mounted) setState(() => _timelineFailed = true);
    }
  }

  Future<void> _loadMemories({String? category, String? month}) async {
    final generation = ++_loadGeneration;
    setState(() {
      _loadingMemories = true;
      _memoriesError = false;
      // A new first page disarms any paging state from the previous filter;
      // an in-flight load-more for it is dropped by the generation check.
      _hasMore = false;
      _loadingMore = false;
      _loadMoreFailed = false;
    });
    try {
      final mems = await _service.fetchMemories(
        category: category == 'All' ? null : category,
        month: month,
        limit: _pageSize,
      );
      // Stale response (the filter changed since this request left) — drop it.
      if (!mounted || generation != _loadGeneration) return;
      setState(() {
        _memories = mems;
        _loadingMemories = false;
        _hasMore = mems.length == _pageSize;
      });
    } on MemoryServiceException catch (e) {
      if (e.isAuthExpired) {
        _handleAuthExpired();
        return;
      }
      if (!mounted || generation != _loadGeneration) return;
      setState(() {
        _loadingMemories = false;
        _memoriesError = true;
      });
    } catch (_) {
      // Network failure etc. — surface a retry, never a fake-empty archive.
      if (!mounted || generation != _loadGeneration) return;
      setState(() {
        _loadingMemories = false;
        _memoriesError = true;
      });
    }
  }

  /// Fetch the page older than the oldest loaded entry. Idempotent while a
  /// page is in flight; a filter switch mid-flight bumps the generation and
  /// the late page is dropped.
  Future<void> _loadMore() async {
    if (_loadingMore || !_hasMore || _loadingMemories || _memories.isEmpty) {
      return;
    }
    final generation = _loadGeneration;
    // The list is newest-first, so the last row is the oldest one we hold.
    // The cursor is its exact server timestamp plus its id: rows sharing the
    // timestamp are neither skipped nor repeated.
    final oldest = _memories.last;
    final before =
        oldest.createdAtRaw ?? oldest.createdAt.toUtc().toIso8601String();
    setState(() {
      _loadingMore = true;
      _loadMoreFailed = false;
    });
    try {
      final page = await _service.fetchMemories(
        category: _selectedCategory == 'All' ? null : _selectedCategory,
        month: _selectedMonth,
        before: before,
        beforeId: oldest.id.isEmpty ? null : oldest.id,
        limit: _pageSize,
      );
      if (!mounted || generation != _loadGeneration) return;
      setState(() {
        final known = _memories.map((m) => m.id).toSet();
        _memories.addAll(page.where((m) => !known.contains(m.id)));
        _hasMore = page.length == _pageSize;
        _loadingMore = false;
      });
    } on MemoryServiceException catch (e) {
      if (e.isAuthExpired) {
        _handleAuthExpired();
        return;
      }
      if (!mounted || generation != _loadGeneration) return;
      setState(() {
        _loadingMore = false;
        _loadMoreFailed = true;
      });
    } catch (_) {
      if (!mounted || generation != _loadGeneration) return;
      setState(() {
        _loadingMore = false;
        _loadMoreFailed = true;
      });
    }
  }

  void _retryLoad() {
    _loadCategories();
    _loadTimeline();
    _loadMemories(category: _selectedCategory, month: _selectedMonth);
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
    showMemoryDetail(
      context,
      memory: memory,
      serverUrl: widget.serverUrl,
      authToken: _authToken,
      onDiscarded: (id) {
        if (mounted) setState(() => _memories.removeWhere((m) => m.id == id));
      },
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
}
