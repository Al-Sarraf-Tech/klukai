import 'dart:js_interop';
import 'dart:typed_data';
import 'package:web/web.dart' as web;

void downloadArtwork(Uint8List bytes, String mime, String extension) {
  final blob = web.Blob([bytes.toJS].toJS, web.BlobPropertyBag(type: mime));
  final url = web.URL.createObjectURL(blob);
  final anchor = web.document.createElement('a') as web.HTMLAnchorElement
    ..href = url
    ..download =
        'klukai_artwork_${DateTime.now().millisecondsSinceEpoch}.$extension';
  anchor.click();
  // Give the browser time to consume the download before releasing the URL.
  Future<void>.delayed(
    const Duration(seconds: 1),
    () => web.URL.revokeObjectURL(url),
  );
}
