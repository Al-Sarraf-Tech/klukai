part of 'her_pov_screen.dart';

/// Responsive screen layout and read-only kept-art gallery presentation.
/// Shares the screen's single State owner; no transport or state is duplicated.
extension _HerPovLayout on _HerPovScreenState {
  Widget _buildDesktop(EdgeInsets pad) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(24, 12, 24, 20),
      child: Column(
        children: [
          _topBar(showTitle: true),
          const SizedBox(height: 16),
          Expanded(
            child: Row(
              children: [
                Expanded(flex: 5, child: _stage(showCta: true)),
                const SizedBox(width: 20),
                Expanded(flex: 4, child: _buildGalleryPanel()),
              ],
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildMobile(EdgeInsets pad) {
    return Column(
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(4, 0, 12, 0),
          child: _topBar(showTitle: true),
        ),
        Expanded(
          child: ListView(
            padding: const EdgeInsets.fromLTRB(16, 8, 16, 16),
            children: [
              SizedBox(height: 460, child: _stage(showCta: false)),
              const SizedBox(height: 16),
              SizedBox(height: 300, child: _buildGalleryPanel()),
              const SizedBox(height: 100),
            ],
          ),
        ),
        ClipRect(
          child: BackdropFilter(
            filter: ImageFilter.blur(sigmaX: 20, sigmaY: 20),
            child: Container(
              width: double.infinity,
              padding: EdgeInsets.fromLTRB(16, 12, 16, 10 + pad.bottom * 0.25),
              decoration: BoxDecoration(
                color: GFL2Colors.surface.withValues(alpha: 0.88),
                border: Border(
                  top: BorderSide(
                    color: GFL2Colors.border.withValues(alpha: 0.55),
                  ),
                ),
              ),
              child: _cta(fullWidth: true),
            ),
          ),
        ),
      ],
    );
  }

  Widget _topBar({required bool showTitle}) {
    return Row(
      children: [
        IconButton(
          tooltip: 'Back',
          onPressed: () => Navigator.pop(context),
          icon: const Icon(Icons.arrow_back_ios_new_rounded, size: 18),
          color: GFL2Colors.textPrimary,
        ),
        if (showTitle)
          Text(
            'HER POV',
            style: TextStyle(
              color: GFL2Colors.primary.withValues(alpha: 0.95),
              letterSpacing: 3.5,
              fontSize: 12,
              fontWeight: FontWeight.w700,
            ),
          ),
      ],
    );
  }

  Widget _buildGalleryPanel() {
    return ClipRRect(
      borderRadius: BorderRadius.circular(22),
      child: Container(
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(22),
          border: Border.all(color: GFL2Colors.border.withValues(alpha: 0.7)),
          color: GFL2Colors.surface.withValues(alpha: 0.55),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 14, 8, 8),
              child: Row(
                children: [
                  const Text(
                    'KEPT FROM HER SIDE',
                    style: TextStyle(
                      color: GFL2Colors.textDim,
                      fontSize: 11,
                      letterSpacing: 1.5,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  const Spacer(),
                  IconButton(
                    onPressed: _loadGallery,
                    icon: const Icon(Icons.refresh_rounded, size: 18),
                    color: GFL2Colors.textDim,
                    tooltip: 'Refresh',
                  ),
                ],
              ),
            ),
            Expanded(child: _galleryBody()),
          ],
        ),
      ),
    );
  }

  Widget _galleryBody() {
    if (_board.phase == GalleryPhase.loading && _board.items.isEmpty) {
      return const Center(
        child: CircularProgressIndicator(
          key: Key('gallery-loading'),
          strokeWidth: 2,
          color: GFL2Colors.primary,
        ),
      );
    }
    if (_board.phase == GalleryPhase.error) {
      return Column(
        key: const Key('gallery-error'),
        children: [
          if (_board.items.isNotEmpty)
            Expanded(child: _galleryGrid(_board.items)),
          const SizedBox(height: 8),
          const Text(
            'Portraits unavailable',
            textAlign: TextAlign.center,
            style: TextStyle(color: GFL2Colors.textPrimary, fontSize: 13),
          ),
          TextButton(
            key: const Key('gallery-retry'),
            onPressed: _loadGallery,
            child: const Text('Retry'),
          ),
        ],
      );
    }
    if (_board.items.isEmpty) {
      return const Center(
        child: Text(
          'No portraits yet.\nAsk her to find a moment.',
          key: Key('gallery-empty'),
          textAlign: TextAlign.center,
          style: TextStyle(color: GFL2Colors.textDim, height: 1.5),
        ),
      );
    }
    return _galleryGrid(_board.items);
  }

  Widget _galleryGrid(List<Memory> items) {
    return GridView.builder(
      padding: const EdgeInsets.fromLTRB(12, 0, 12, 12),
      gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
        crossAxisCount: 2,
        mainAxisSpacing: 10,
        crossAxisSpacing: 10,
        childAspectRatio: 0.78,
      ),
      itemCount: items.length,
      itemBuilder: (_, i) {
        final m = items[i];
        return ClipRRect(
          borderRadius: BorderRadius.circular(12),
          child: Stack(
            fit: StackFit.expand,
            children: [
              Image.network(
                _memories.thumbnailUrl(m.id),
                fit: BoxFit.cover,
                headers: _headers,
                errorBuilder: (context, error, stack) => Container(
                  color: GFL2Colors.panel,
                  child: const Icon(
                    Icons.image_not_supported_outlined,
                    color: GFL2Colors.textDim,
                  ),
                ),
              ),
              Positioned(
                left: 0,
                right: 0,
                bottom: 0,
                child: Container(
                  padding: const EdgeInsets.fromLTRB(8, 18, 8, 8),
                  decoration: BoxDecoration(
                    gradient: LinearGradient(
                      begin: Alignment.topCenter,
                      end: Alignment.bottomCenter,
                      colors: [
                        Colors.transparent,
                        Colors.black.withValues(alpha: 0.78),
                      ],
                    ),
                  ),
                  child: Text(
                    m.annotation ?? m.category,
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(
                      color: Colors.white,
                      fontSize: 11,
                      height: 1.25,
                    ),
                  ),
                ),
              ),
            ],
          ),
        );
      },
    );
  }
}
