/// What to do when the page comes back from the background.
///
/// iOS freezes a home-screen PWA when it is backgrounded and drops its
/// WebSocket without a close event, so the socket can look open and be dead.
/// On resume we redial if the link is down, or if nothing has arrived for
/// [staleAfter] (a frozen socket delivers nothing). Redialing is cheap and
/// also warms her model (warm-on-connect).
class ResumePolicy {
  const ResumePolicy({this.staleAfter = const Duration(seconds: 25)});

  final Duration staleAfter;

  bool shouldReconnect({required bool connected, required Duration sinceLastFrame}) =>
      !connected || sinceLastFrame >= staleAfter;
}
