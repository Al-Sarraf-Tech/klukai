/// What to do when the page comes back from the background.
///
/// iOS freezes a home-screen PWA when it is backgrounded and drops its
/// WebSocket without a close event, so the socket can look open and be dead.
/// On resume we redial if the link is down, or if the page was away for
/// [staleAfter] or longer (a socket frozen that long is not to be trusted).
/// Redialing is cheap and also warms her model (warm-on-connect).
///
/// The duration passed is how long the page was actually hidden — not how
/// long the socket has been silent. A one-second tab switch after a quiet
/// stretch of chat must leave a healthy socket alone: the server cancels
/// whatever is in flight (an image, say) when the old socket closes.
class ResumePolicy {
  const ResumePolicy({this.staleAfter = const Duration(seconds: 25)});

  final Duration staleAfter;

  bool shouldReconnect({
    required bool connected,
    required Duration sinceLastFrame,
  }) => !connected || sinceLastFrame >= staleAfter;
}
