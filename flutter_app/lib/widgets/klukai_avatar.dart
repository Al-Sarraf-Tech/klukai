import 'dart:js_interop';
import 'dart:ui_web' as ui_web;
import 'package:flutter/material.dart';
import 'package:web/web.dart' as web;

@JS('klukaiBridge.init')
external JSPromise<JSBoolean> _jsInit(JSString canvasId, JSString modelUrl);

@JS('klukaiBridge.setMood')
external void _jsSetMood(JSString mood);

@JS('klukaiBridge.playReaction')
external void _jsPlayReaction(JSString reaction);

@JS('klukaiBridge.setTalking')
external void _jsSetTalking(JSBoolean enabled);

@JS('klukaiBridge.setBlush')
external void _jsSetBlush(JSNumber intensity);

@JS('klukaiBridge.lookAt')
external void _jsLookAt(JSNumber normX, JSNumber normY);

@JS('klukaiBridge.setDormMode')
external void _jsSetDormMode(JSBoolean enabled);

@JS('klukaiBridge.dispose')
external void _jsDispose();

@JS('window.__threeReady')
external JSBoolean? get _jsThreeReady;

class KlukaiAvatarController {
  bool _initialized = false;

  bool get isInitialized => _initialized;

  Future<bool> init(String canvasId, String modelUrl) async {
    // Wait for Three.js to load (module scripts are async)
    for (int i = 0; i < 50; i++) {
      if (_jsThreeReady?.toDart == true) break;
      await Future.delayed(const Duration(milliseconds: 100));
    }
    if (_jsThreeReady?.toDart != true) {
      debugPrint('[KlukaiAvatar] Three.js not ready after 5s');
      return false;
    }
    try {
      final result = await _jsInit(canvasId.toJS, modelUrl.toJS).toDart;
      _initialized = result.toDart;
      return _initialized;
    } catch (e) {
      debugPrint('[KlukaiAvatar] Init failed: $e');
      return false;
    }
  }

  void setMood(String mood) {
    if (!_initialized) return;
    _jsSetMood(mood.toJS);
  }

  void playReaction(String reaction) {
    if (!_initialized) return;
    _jsPlayReaction(reaction.toJS);
  }

  void setTalking(bool enabled) {
    if (!_initialized) return;
    _jsSetTalking(enabled.toJS);
  }

  void setBlush(double intensity) {
    if (!_initialized) return;
    _jsSetBlush(intensity.toJS);
  }

  void lookAt(double normX, double normY) {
    if (!_initialized) return;
    _jsLookAt(normX.toJS, normY.toJS);
  }

  void setDormMode(bool enabled) {
    if (!_initialized) return;
    _jsSetDormMode(enabled.toJS);
  }

  void dispose() {
    if (!_initialized) return;
    _jsDispose();
    _initialized = false;
  }
}

// Register platform view factories for CanvasKit renderer
bool _viewFactoriesRegistered = false;

void _ensureViewFactories() {
  if (_viewFactoriesRegistered) return;
  _viewFactoriesRegistered = true;
  // We register a factory per canvas ID dynamically in the widget
}

class KlukaiAvatar extends StatefulWidget {
  final String modelUrl;
  final KlukaiAvatarController controller;
  final VoidCallback? onTap;

  const KlukaiAvatar({
    super.key,
    required this.modelUrl,
    required this.controller,
    this.onTap,
  });

  @override
  State<KlukaiAvatar> createState() => _KlukaiAvatarState();
}

class _KlukaiAvatarState extends State<KlukaiAvatar> {
  static int _nextId = 0;
  late final String _viewType;
  late final String _canvasId;
  bool _ready = false;

  @override
  void initState() {
    super.initState();
    final id = _nextId++;
    _viewType = 'klukai-avatar-$id';
    _canvasId = 'klukai-canvas-$id';

    // Register a platform view factory for this instance
    ui_web.platformViewRegistry.registerViewFactory(
      _viewType,
      (int viewId) {
        // Wrap canvas in a div so Flutter's platform view sizing works
        final container = web.document.createElement('div') as web.HTMLDivElement;
        container.style.width = '100%';
        container.style.height = '100%';
        container.style.position = 'relative';
        container.style.overflow = 'hidden';

        final canvas = web.document.createElement('canvas') as web.HTMLCanvasElement;
        canvas.id = _canvasId;
        canvas.style.width = '100%';
        canvas.style.height = '100%';
        canvas.style.display = 'block';
        canvas.style.position = 'absolute';
        canvas.style.top = '0';
        canvas.style.left = '0';
        container.appendChild(canvas);
        return container;
      },
    );

    WidgetsBinding.instance.addPostFrameCallback((_) => _initCanvas());
  }

  Future<void> _initCanvas() async {
    // Skip if controller is already initialized (widget rebuilt but same controller)
    if (widget.controller.isInitialized) {
      if (mounted) setState(() => _ready = true);
      return;
    }
    // Give the platform view time to mount in the DOM
    await Future.delayed(const Duration(milliseconds: 500));
    final success = await widget.controller.init(_canvasId, widget.modelUrl);
    if (mounted) {
      setState(() => _ready = success);
    }
  }

  @override
  void dispose() {
    // Don't dispose the controller here — it's owned by ChatScreen
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: widget.onTap,
      onPanUpdate: (details) {
        final box = context.findRenderObject() as RenderBox?;
        if (box == null) return;
        final size = box.size;
        final normX = (details.localPosition.dx / size.width) * 2 - 1;
        final normY = -((details.localPosition.dy / size.height) * 2 - 1);
        widget.controller.lookAt(normX, normY);
      },
      child: HtmlElementView(viewType: _viewType),
    );
  }
}
