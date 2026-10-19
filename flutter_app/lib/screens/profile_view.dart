part of 'profile_screen.dart';

/// Dossier presentation and wardrobe actions, using the screen's single state.
extension _ProfileView on _ProfileScreenState {
  Widget _buildHeader() {
    return Center(
      child: Column(
        children: [
          Container(
            width: 120,
            height: 120,
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(8),
              border: Border.all(
                color: GFL2Colors.primary.withValues(alpha: 0.5),
                width: 2,
              ),
              boxShadow: [
                BoxShadow(
                  color: GFL2Colors.primary.withValues(alpha: 0.15),
                  blurRadius: 16,
                ),
              ],
            ),
            child: ClipRRect(
              borderRadius: BorderRadius.circular(6),
              child: Image.asset(
                'assets/klukai_portrait.png',
                fit: BoxFit.cover,
                errorBuilder: (_, e, s) => Container(
                  color: GFL2Colors.panel,
                  child: const Center(
                    child: Text(
                      'K',
                      style: TextStyle(
                        color: GFL2Colors.primary,
                        fontSize: 40,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                  ),
                ),
              ),
            ),
          ),
          const SizedBox(height: 12),
          const Text(
            'KLUKAI',
            style: TextStyle(
              color: GFL2Colors.textPrimary,
              fontSize: 20,
              fontWeight: FontWeight.w800,
              letterSpacing: 3,
            ),
          ),
          Container(
            width: 50,
            height: 2,
            margin: const EdgeInsets.only(top: 4),
            color: GFL2Colors.accent,
          ),
          const SizedBox(height: 6),
          Text(
            kKlukaiDesignation,
            style: TextStyle(
              color: GFL2Colors.textMuted,
              fontSize: 11,
              fontFamily: 'monospace',
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildStats() {
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: GFL2Colors.surface,
        borderRadius: BorderRadius.circular(4),
        border: Border.all(color: GFL2Colors.border.withValues(alpha: 0.3)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _sectionTitle('OPERATIONAL STATUS'),
          const SizedBox(height: 8),
          if (_records == _RecordPhase.loading && !_recordsGood)
            const Padding(
              padding: EdgeInsets.only(bottom: 8),
              child: Text(
                'Loading records',
                key: Key('profile-records-loading'),
                style: TextStyle(color: GFL2Colors.textMuted, fontSize: 12),
              ),
            ),
          if (_records == _RecordPhase.empty)
            const Padding(
              padding: EdgeInsets.only(bottom: 8),
              child: Text(
                'No milestones yet',
                key: Key('profile-records-empty'),
                style: TextStyle(color: GFL2Colors.textMuted, fontSize: 12),
              ),
            ),
          if (_records == _RecordPhase.error)
            const Padding(
              padding: EdgeInsets.only(bottom: 4),
              child: Text(
                'Records unavailable',
                key: Key('profile-records-error'),
                style: TextStyle(color: GFL2Colors.danger, fontSize: 12),
              ),
            ),
          Align(
            alignment: Alignment.centerLeft,
            child: TextButton(
              key: const Key('profile-records-retry'),
              onPressed: _loadData,
              child: const Text('Retry'),
            ),
          ),
          _statRow('TRUST LEVEL', widget.affectionLevelName.toUpperCase()),
          // Affection is on a 0–1000 scale (the gauge fills score/1000).
          _statRow('AFFECTION', '${widget.affectionScore}/1000'),
          _statRow('INTERACTIONS', '$_interactions'),
          _statRow('MILESTONES', '${_milestones.length}'),
          _statRow('CURRENT OUTFIT', _todayName),
        ],
      ),
    );
  }

  Widget _buildBackstory() {
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: GFL2Colors.surface,
        borderRadius: BorderRadius.circular(4),
        border: Border(
          left: BorderSide(
            color: GFL2Colors.primary.withValues(alpha: 0.4),
            width: 2,
          ),
        ),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _sectionTitle('CLASSIFIED PROFILE'),
          const SizedBox(height: 8),
          Text(
            kKlukaiBackstory,
            style: TextStyle(
              color: GFL2Colors.textPrimary.withValues(alpha: 0.8),
              fontSize: 13,
              height: 1.6,
            ),
          ),
        ],
      ),
    );
  }

  Widget _panel({
    required Key key,
    required String title,
    required List<Widget> children,
  }) {
    return Container(
      key: key,
      width: double.infinity,
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: GFL2Colors.surface,
        borderRadius: BorderRadius.circular(4),
        border: Border.all(color: GFL2Colors.border.withValues(alpha: 0.3)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _sectionTitle(title),
          const SizedBox(height: 8),
          ...children,
        ],
      ),
    );
  }

  Widget _buildTodaySchedule() {
    final day = _herDay!;
    return _panel(
      key: const Key('profile-today'),
      title: 'TODAY',
      children: [
        if ((day.status?.label ?? '').isNotEmpty)
          Padding(
            padding: const EdgeInsets.only(bottom: 8),
            child: Text(
              day.status!.label,
              style: const TextStyle(
                color: GFL2Colors.textPrimary,
                fontSize: 12,
                fontWeight: FontWeight.w600,
              ),
            ),
          ),
        ScheduleStrip(blocks: day.schedule),
      ],
    );
  }

  Widget _buildCostumeSelector() {
    final wardrobe = _wardrobe!;
    final today = wardrobe.today ?? _herDay?.outfit;
    return _panel(
      key: const Key('profile-wardrobe'),
      title: 'WARDROBE',
      children: [
        if (today != null) TodayOutfitCard(outfit: today),
        WardrobeGrid(
          wardrobe: wardrobe,
          onSelect: _setCostume,
          onLockedTap: (_) => _say('Not yet.'),
        ),
      ],
    );
  }

  Widget _buildWornLog() {
    return _panel(
      key: const Key('profile-worn'),
      title: 'WORN THIS MONTH',
      children: [WornLogList(entries: _log)],
    );
  }

  Widget _buildSquadRoster() {
    const squad = kSquadRoster;

    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: GFL2Colors.surface,
        borderRadius: BorderRadius.circular(4),
        border: Border.all(color: GFL2Colors.border.withValues(alpha: 0.3)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          _sectionTitle('SQUAD ROSTER'),
          const SizedBox(height: 8),
          ...squad.map(
            (s) => Padding(
              padding: const EdgeInsets.only(bottom: 6),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Container(
                    width: 4,
                    height: 4,
                    margin: const EdgeInsets.only(top: 6, right: 8),
                    decoration: const BoxDecoration(
                      shape: BoxShape.circle,
                      color: GFL2Colors.primary,
                    ),
                  ),
                  Expanded(
                    child: RichText(
                      text: TextSpan(
                        children: [
                          TextSpan(
                            text: s.$1,
                            style: const TextStyle(
                              color: GFL2Colors.textPrimary,
                              fontSize: 11,
                              fontWeight: FontWeight.w700,
                              fontFamily: 'monospace',
                            ),
                          ),
                          TextSpan(
                            text: '  ${s.$2}',
                            style: TextStyle(
                              color: GFL2Colors.textMuted,
                              fontSize: 11,
                            ),
                          ),
                        ],
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildTimelineButton() {
    return GestureDetector(
      onTap: () => Navigator.push(
        context,
        MaterialPageRoute(
          builder: (_) => TimelineScreen(milestones: _milestones),
        ),
      ),
      child: Container(
        width: double.infinity,
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(
          color: GFL2Colors.surface,
          borderRadius: BorderRadius.circular(4),
          border: Border.all(color: GFL2Colors.border.withValues(alpha: 0.3)),
        ),
        child: Row(
          children: [
            Icon(
              Icons.timeline,
              color: GFL2Colors.primary.withValues(alpha: 0.6),
              size: 18,
            ),
            const SizedBox(width: 8),
            const Text(
              'RELATIONSHIP TIMELINE',
              style: TextStyle(
                color: GFL2Colors.textPrimary,
                fontSize: 12,
                fontWeight: FontWeight.w700,
                letterSpacing: 1,
                fontFamily: 'monospace',
              ),
            ),
            const Spacer(),
            Icon(
              Icons.chevron_right,
              color: GFL2Colors.textDim.withValues(alpha: 0.4),
              size: 18,
            ),
          ],
        ),
      ),
    );
  }

  Widget _sectionTitle(String title) {
    return Text(
      title,
      style: TextStyle(
        color: GFL2Colors.primary,
        fontSize: 10,
        fontWeight: FontWeight.w700,
        letterSpacing: 1.5,
        fontFamily: 'monospace',
      ),
    );
  }

  Widget _statRow(String label, String value) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 4),
      child: Row(
        children: [
          Text(
            label,
            style: TextStyle(
              color: GFL2Colors.textMuted,
              fontSize: 10,
              fontFamily: 'monospace',
              letterSpacing: 0.5,
            ),
          ),
          const Spacer(),
          Text(
            value,
            style: const TextStyle(
              color: GFL2Colors.textPrimary,
              fontSize: 11,
              fontWeight: FontWeight.w700,
              fontFamily: 'monospace',
            ),
          ),
        ],
      ),
    );
  }
}
