part of 'memory_archive_screen.dart';

/// Archive app bar and desktop category/month navigation.
/// Shares the screen's single State owner; no transport or state is duplicated.
extension _ArchiveSidebar on _MemoryArchiveScreenState {
  PreferredSizeWidget _buildAppBar() {
    return AppBar(
      backgroundColor: GFL2Colors.surface,
      elevation: 0,
      leading: IconButton(
        tooltip: 'Back',
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
      actions: [
        IconButton(
          tooltip: 'The Thread',
          icon: Icon(
            Icons.forum_outlined,
            color: GFL2Colors.affinity.withValues(alpha: 0.8),
            size: 20,
          ),
          onPressed: () => Navigator.of(context).push(
            MaterialPageRoute(
              builder: (_) => ThreadScreen(serverUrl: widget.serverUrl),
            ),
          ),
        ),
      ],
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
                  child: CircularProgressIndicator(
                    strokeWidth: 1.5,
                    color: GFL2Colors.primary,
                  ),
                ),
              )
            else ...[
              ..._categories.map(
                (cat) => _buildSidebarItem(cat.name, cat.count),
              ),
            ],
            // Divider
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
              child: Container(
                height: 1,
                color: GFL2Colors.border.withValues(alpha: 0.4),
              ),
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
              ..._timeline.map(
                (g) => _buildSidebarMonthItem(g.month, g.label, g.count),
              ),
            ],
            const Spacer(),
            // Archive stats
            Padding(
              padding: const EdgeInsets.fromLTRB(14, 8, 14, 16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Container(
                    height: 1,
                    color: GFL2Colors.border.withValues(alpha: 0.4),
                  ),
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
                  color: isSelected
                      ? GFL2Colors.textPrimary
                      : GFL2Colors.textDim,
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
                  color: isSelected
                      ? GFL2Colors.textPrimary
                      : GFL2Colors.textDim.withValues(alpha: 0.7),
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
}
