/// Her 3D stage inside the Chat tab: the companion page (three.js avatar) in
/// a same-origin iframe, `companion.html?embed=1&mode=3d`, which hides its
/// own chrome and keeps its own socket so the avatar reacts to the chat.
///
/// Browser-only (package:web); the chat screen is the only importer.
library;

import 'dart:async';
import 'dart:js_interop';

import 'package:flutter/material.dart';
import 'package:web/web.dart' as web;

/// Where the embedded stage frames her: the HUD covers this much of the top
/// and bottom of the iframe. Passed in the URL; the stage clamps them.
const int kStageInsetTop = 120;
const int kStageInsetBottom = 210;

/// The embedded stage's URL, relative to `<base href>` (/app/).
String companionStageSrc({
  int insetTop = kStageInsetTop,
  int insetBottom = kStageInsetBottom,
}) =>
    'companion.html?embed=1&mode=3d&inset_top=$insetTop&inset_bottom=$insetBottom';

/// The PWA's handle on the embedded stage.
abstract class CompanionStageLink {
  /// Ask her avatar to say [text] once, lip-synced. [id] identifies this tap
  /// (the stage drops a repeated id). False when there is no stage to ask.
  bool speak(String text, {required int id});
}

/// A `klukai:*` message from the stage (`klukai:speak-status`,
/// `klukai:stage-notice`, `klukai:stage-ready`, `klukai:speaking`).
typedef StageEventHandler =
    void Function(String type, Map<String, Object?> data);

/// Builds the stage. Production uses [CompanionStageView]; tests inject a
/// stand-in so no page loads.
typedef CompanionStageBuilder =
    Widget Function(
      BuildContext context, {
      required String src,
      required ValueChanged<CompanionStageLink?> onLink,
      required StageEventHandler onEvent,
    });

Widget defaultCompanionStage(
  BuildContext context, {
  required String src,
  required ValueChanged<CompanionStageLink?> onLink,
  required StageEventHandler onEvent,
}) => CompanionStageView(src: src, onLink: onLink, onEvent: onEvent);

/// The iframe. Messages are exchanged with it only on this page's origin, and
/// only messages whose source is this iframe's window are accepted.
class CompanionStageView extends StatefulWidget {
  const CompanionStageView({
    super.key,
    required this.src,
    required this.onLink,
    required this.onEvent,
  });

  final String src;

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
    final win = _frame?.contentWindow;
    if (win == null) return false;
    win.postMessage(
      {'type': 'klukai:speak', 'text': text, 'id': id}.jsify(),
      web.window.location.origin.toJS,
    );
    return true;
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
    widget.onLink(this);
  }

  @override
  void dispose() {
    _messages?.cancel();
    _frame = null;
    widget.onLink(null);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => HtmlElementView.fromTagName(
    key: const Key('companion-stage-iframe'),
    tagName: 'iframe',
    onElementCreated: _created,
  );
}

/// Lets Flutter controls drawn over the iframe receive their taps: an
/// invisible DOM element under [child] catches pointer events that would
/// otherwise land in the iframe (the pattern of package:pointer_interceptor).
class HudPointerShield extends StatelessWidget {
  const HudPointerShield({super.key, required this.child});
  final Widget child;

  @override
  Widget build(BuildContext context) => Stack(
    children: [
      Positioned.fill(
        child: HtmlElementView.fromTagName(tagName: 'div', isVisible: false),
      ),
      child,
    ],
  );
}
