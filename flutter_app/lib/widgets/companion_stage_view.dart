/// Her 3D stage inside the Chat tab: the companion page (three.js avatar) in
/// a same-origin iframe, `companion.html?embed=1&mode=3d`, which hides its
/// own chrome and keeps its own socket so the avatar reacts to the chat.
/// The Chat tab keeps it loaded across its CHAT ⇄ 3D flip: under the CHAT
/// face it is parked (not drawn, no taps, its render loop and sound stopped).
/// In 3D nothing Flutter draws covers it: the stage draws its own HUD (her
/// status, her line, `▶ VOICE`) from the `klukai:hud` snapshots it is sent.
///
/// Browser-only (package:web); the chat screen is the only importer.
library;

import 'dart:async';
import 'dart:js_interop';

import 'package:flutter/material.dart';
import 'package:web/web.dart' as web;

/// Set once he has opened her 3D view on this device; from then on the Chat
/// tab loads the stage in the background so his first flip finds her there.
const String kStageUsedKey = 'klukai_3d_used';

/// The embedded stage's URL, relative to `<base href>` (/app/). Nothing
/// covers the iframe, so it frames her with no insets. [hidden]: it is
/// loaded behind the CHAT face and starts parked.
String companionStageSrc({bool hidden = false}) =>
    'companion.html?embed=1&mode=3d&inset_top=0&inset_bottom=0'
    '${hidden ? '&hidden=1' : ''}';

/// The PWA's handle on the embedded stage.
abstract class CompanionStageLink {
  /// Ask her avatar to say [text] once, lip-synced. [id] identifies this tap
  /// (the stage drops a repeated id). False when there is no stage to ask.
  bool speak(String text, {required int id});

  /// Send the stage its HUD: a full [snapshot] of what it shows (see
  /// [stageHudMessage]). False when there is no stage to tell.
  bool sendHud(Map<String, Object?> snapshot);
}

/// A `klukai:*` message from the stage (`klukai:stage-ready`,
/// `klukai:stage-notice`, `klukai:speaking`, `klukai:voice-error`,
/// `klukai:speak-status`, `klukai:herday`, `klukai:auth-expired`).
typedef StageEventHandler =
    void Function(String type, Map<String, Object?> data);

/// Builds the stage. Production uses [CompanionStageView]; tests inject a
/// stand-in so no page loads. [visible] is false while the CHAT face covers
/// it: the same stage must stay built (rebuilding it reloads the page).
/// [interactive] is false while something of the PWA's (a menu, a sheet)
/// is open over it, so that gets his taps instead.
typedef CompanionStageBuilder =
    Widget Function(
      BuildContext context, {
      required String src,
      required bool visible,
      required bool interactive,
      required ValueChanged<CompanionStageLink?> onLink,
      required StageEventHandler onEvent,
    });

Widget defaultCompanionStage(
  BuildContext context, {
  required String src,
  required bool visible,
  required bool interactive,
  required ValueChanged<CompanionStageLink?> onLink,
  required StageEventHandler onEvent,
}) => CompanionStageView(
  src: src,
  visible: visible,
  interactive: interactive,
  onLink: onLink,
  onEvent: onEvent,
);

/// The `klukai:visibility` message: the stage parks (no render loop, no
/// sound, her voice stopped) or carries on, silently.
const String kStageVisibilityMessage = 'klukai:visibility';

/// The `klukai:hud` message: what the stage's own HUD shows.
const String kStageHudMessage = 'klukai:hud';

/// The `klukai:hud` message for [snapshot]: `level`, `mood`, `bpm`,
/// `spiking`, `wearing`, `line`, `lineId`, `streaming` and `status`.
Map<String, Object?> stageHudMessage(Map<String, Object?> snapshot) => {
  ...snapshot,
  'type': kStageHudMessage,
};

/// Posts [message] to the stage [frame], on this page's origin only. False
/// when the frame has no window yet.
bool postToStage(web.HTMLIFrameElement frame, Map<String, Object?> message) {
  final win = frame.contentWindow;
  if (win == null) return false;
  win.postMessage(message.jsify(), web.window.location.origin.toJS);
  return true;
}

/// Lets the stage [frame] take pointer events, or not: only while it is
/// shown and nothing of the PWA's is open over it.
void setStageInteractive(
  web.HTMLIFrameElement frame, {
  required bool visible,
  bool interactive = true,
}) {
  frame.style.pointerEvents = visible && interactive ? 'auto' : 'none';
}

/// Shows or parks the stage [frame]. Parked, it stays loaded but is not drawn
/// and takes no pointer events (the Flutter content over it does), and the
/// page is told so; shown, it is told to carry on. Same origin only.
void setStageVisible(
  web.HTMLIFrameElement frame, {
  required bool visible,
  bool interactive = true,
}) {
  frame.style.visibility = visible ? 'visible' : 'hidden';
  setStageInteractive(frame, visible: visible, interactive: interactive);
  postToStage(frame, {'type': kStageVisibilityMessage, 'visible': visible});
}

/// Whether the stage iframe is painted. A shown stage always is. A parked one
/// is painted only until its page has loaded: the engine puts a platform
/// view's element into the page when it is first painted, and afterwards
/// leaves it where it is while unpainted (only its slot goes), so the page
/// keeps running, unrendered, and the CHAT face over it renders as if it
/// were not there.
bool paintStage({required bool visible, required bool loaded}) =>
    visible || !loaded;

/// The iframe. Messages are exchanged with it only on this page's origin, and
/// only messages whose source is this iframe's window are accepted.
class CompanionStageView extends StatefulWidget {
  const CompanionStageView({
    super.key,
    required this.src,
    this.visible = true,
    this.interactive = true,
    required this.onLink,
    required this.onEvent,
  });

  /// Read once, when the iframe is created; a later change does not navigate.
  final String src;

  /// False while the CHAT face covers it (see [setStageVisible]).
  final bool visible;

  /// False while a menu or sheet of the PWA's is open over it.
  final bool interactive;

  /// Called with the link once the iframe exists, and with null when it is
  /// gone. Must not call setState (it runs during dispose).
  final ValueChanged<CompanionStageLink?> onLink;
  final StageEventHandler onEvent;

  @override
  State<CompanionStageView> createState() => _CompanionStageViewState();
}

class _CompanionStageViewState extends State<CompanionStageView>
    implements CompanionStageLink {
  web.HTMLIFrameElement? _frame;
  StreamSubscription<web.MessageEvent>? _messages;
  StreamSubscription<web.Event>? _loads;

  /// The page has loaded (see [paintStage]).
  bool _loaded = false;

  @override
  void initState() {
    super.initState();
    _messages = web.EventStreamProviders.messageEvent
        .forTarget(web.window)
        .listen(_onMessage);
  }

  void _onMessage(web.MessageEvent event) {
    final win = _frame?.contentWindow;
    if (win == null || event.origin != web.window.location.origin) return;
    final source = event.source;
    if (source == null || !source.strictEquals(win).toDart) return;
    final data = event.data.dartify();
    if (data is! Map) return;
    final type = data['type'];
    if (type is! String || !type.startsWith('klukai:')) return;
    widget.onEvent(type, {for (final e in data.entries) '${e.key}': e.value});
  }

  @override
  bool speak(String text, {required int id}) {
    final frame = _frame;
    if (frame == null) return false;
    return postToStage(frame, {'type': 'klukai:speak', 'text': text, 'id': id});
  }

  @override
  bool sendHud(Map<String, Object?> snapshot) {
    final frame = _frame;
    if (frame == null) return false;
    return postToStage(frame, stageHudMessage(snapshot));
  }

  void _created(Object element) {
    final frame = element as web.HTMLIFrameElement;
    frame
      ..src = widget.src
      ..title = 'Klukai 3D'
      ..allow = 'autoplay';
    frame.style
      ..border = '0'
      ..width = '100%'
      ..height = '100%'
      ..display = 'block'
      ..backgroundColor = '#06080c';
    _frame = frame;
    _show(frame);
    // Said before the page loads, it reaches the blank document and is lost:
    // say it again once the stage is listening.
    _loads = web.EventStreamProviders.loadEvent.forElement(frame).listen((_) {
      _show(frame);
      if (mounted && !_loaded) setState(() => _loaded = true);
    });
    widget.onLink(this);
  }

  void _show(web.HTMLIFrameElement frame) => setStageVisible(
    frame,
    visible: widget.visible,
    interactive: widget.interactive,
  );

  @override
  void didUpdateWidget(CompanionStageView oldWidget) {
    super.didUpdateWidget(oldWidget);
    final frame = _frame;
    if (frame == null) return;
    if (oldWidget.visible != widget.visible) {
      _show(frame);
    } else if (oldWidget.interactive != widget.interactive) {
      setStageInteractive(
        frame,
        visible: widget.visible,
        interactive: widget.interactive,
      );
    }
  }

  @override
  void dispose() {
    _messages?.cancel();
    _loads?.cancel();
    _frame = null;
    widget.onLink(null);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => Offstage(
    offstage: !paintStage(visible: widget.visible, loaded: _loaded),
    child: HtmlElementView.fromTagName(
      key: const Key('companion-stage-iframe'),
      tagName: 'iframe',
      onElementCreated: _created,
    ),
  );
}
