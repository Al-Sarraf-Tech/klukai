import 'local_prefs_api.dart';

final LocalPrefs _vmPrefs = MemoryLocalPrefs();

/// VM fallback: there is no localStorage outside the browser.
LocalPrefs defaultLocalPrefs() => _vmPrefs;
