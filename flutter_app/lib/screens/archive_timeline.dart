part of 'memory_archive_screen.dart';

/// Mobile filters and shared timeline loading, error, and empty presentation.
/// Shares the screen's single State owner; no transport or state is duplicated.
extension _ArchiveTimeline on _MemoryArchiveScreenState {
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
          separatorBuilder: (_, _) => const SizedBox(width: 5),
          itemBuilder: (_, i) {
            if (i == 0) return _buildMonthTab(null, 'ALL');
            final group = _timeline[i - 1];
            return _buildMonthTab(
              group.month,
              '${group.label} (${group.count})',
            );
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
            color: isSelected
                ? GFL2Colors.primary
                : GFL2Colors.textDim.withValues(alpha: 0.6),
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
            CircularProgressIndicator(
              strokeWidth: 1.5,
              color: GFL2Colors.primary,
            ),
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
          child: _memoriesError
              ? _buildErrorState()
              : _memories.isEmpty
              ? _buildEmptyState()
              : ListView.builder(
                  physics: const ClampingScrollPhysics(
                    parent: AlwaysScrollableScrollPhysics(),
                  ),
                  padding: const EdgeInsets.fromLTRB(16, 16, 16, 24),
                  itemCount: _memories.length,
                  itemBuilder: (_, i) => MemoryTimelineEntry(
                    // Keyed by memory id so a filter switch never reuses
                    // a recycled State (and its stale thumbnail) for a
                    // different memory.
                    key: ValueKey(_memories[i].id),
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

  Widget _buildErrorState() {
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(
            Icons.sensors_off,
            color: GFL2Colors.danger.withValues(alpha: 0.35),
            size: 48,
          ),
          const SizedBox(height: 14),
          Text(
            'SIGNAL LOST // ARCHIVE UNREACHABLE',
            style: TextStyle(
              color: GFL2Colors.danger.withValues(alpha: 0.7),
              fontSize: 11,
              fontFamily: 'monospace',
              letterSpacing: 1.5,
              fontWeight: FontWeight.w600,
            ),
          ),
          const SizedBox(height: 6),
          Text(
            '...the memories are still there. The link is not.',
            style: TextStyle(
              color: GFL2Colors.textDim.withValues(alpha: 0.3),
              fontSize: 12,
              fontStyle: FontStyle.italic,
            ),
          ),
          const SizedBox(height: 16),
          TextButton(
            onPressed: _retryLoad,
            style: TextButton.styleFrom(
              backgroundColor: GFL2Colors.background,
              padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 10),
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(4),
                side: BorderSide(
                  color: GFL2Colors.primary.withValues(alpha: 0.4),
                ),
              ),
            ),
            child: const Text(
              'RETRY',
              style: TextStyle(
                color: GFL2Colors.primary,
                fontSize: 11,
                fontFamily: 'monospace',
                letterSpacing: 1.5,
                fontWeight: FontWeight.w700,
              ),
            ),
          ),
        ],
      ),
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
