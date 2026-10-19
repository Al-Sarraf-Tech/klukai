// Where the HUD album hands off: a memory's full detail, Her POV and the
// Thread. Those screens talk to the browser (package:web), so the album
// reaches them through this seam and stays testable on the VM.
//
// Conditional export, as in platform/browser.dart: the browser build gets the
// real screens; the VM gets inert links. Tests inject their own [AlbumLinks].
export 'hud_album_links_api.dart';
export 'hud_album_links_stub.dart'
    if (dart.library.js_interop) 'hud_album_links_web.dart';
