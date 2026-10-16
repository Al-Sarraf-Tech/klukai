/// Admission and response ownership for one screen's accepted image job.
///
/// This owns no UI state, transport, server work, or retry policy. A generation
/// binds a pending POST to its screen lifetime; an identity-bound claim admits
/// one poll and prevents a late completion/finally block from touching a new job.
class HerPovJobOwner {
  int _generation = 0;
  bool _disposed = false;
  bool _terminal = false;
  String? _jobId;
  HerPovPoll? _activePoll;

  int begin() {
    if (_disposed) throw StateError('Her POV screen is disposed');
    _generation++;
    _jobId = null;
    _activePoll = null;
    _terminal = false;
    return _generation;
  }

  bool isCurrent(int generation) => !_disposed && generation == _generation;

  bool accept(int generation, String jobId) {
    if (!isCurrent(generation) || _jobId != null || jobId.isEmpty) return false;
    _jobId = jobId;
    return true;
  }

  HerPovPoll? claimPoll() {
    final id = _jobId;
    if (_disposed || _terminal || id == null || _activePoll != null) {
      return null;
    }
    return _activePoll = HerPovPoll._(_generation, id);
  }

  bool ownsPoll(HerPovPoll poll) =>
      isCurrent(poll._generation) &&
      !_terminal &&
      poll.jobId == _jobId &&
      identical(_activePoll, poll);

  void release(HerPovPoll poll) {
    if (identical(_activePoll, poll)) _activePoll = null;
  }

  bool complete(HerPovPoll poll) {
    if (!ownsPoll(poll)) return false;
    _terminal = true;
    _activePoll = null;
    return true;
  }

  /// Detach the UI only. Never cancel, resubmit, or replace accepted server work.
  void dispose() {
    _disposed = true;
    _generation++;
    _activePoll = null;
  }
}

/// An immutable capability for one HTTP poll; only its owner can create it.
class HerPovPoll {
  final int _generation;
  final String jobId;

  const HerPovPoll._(this._generation, this.jobId);
}
