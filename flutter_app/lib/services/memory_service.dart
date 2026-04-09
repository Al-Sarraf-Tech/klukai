import 'dart:convert';
import 'package:http/http.dart' as http;
import '../models/memory.dart';

class MemoryService {
  final String serverUrl;

  MemoryService({required this.serverUrl});

  Future<List<Memory>> fetchMemories({
    String? category,
    int limit = 20,
    String? before,
  }) async {
    final params = <String, String>{'limit': limit.toString()};
    if (category != null && category != 'All') params['category'] = category;
    if (before != null) params['before'] = before;

    final uri = Uri.parse('$serverUrl/api/memories').replace(queryParameters: params);
    final response = await http.get(uri);
    if (response.statusCode == 200) {
      final data = jsonDecode(response.body) as List;
      return data.map((m) => Memory.fromJson(m)).toList();
    }
    return [];
  }

  Future<List<MemoryCategory>> fetchCategories() async {
    final uri = Uri.parse('$serverUrl/api/memories/categories');
    final response = await http.get(uri);
    if (response.statusCode == 200) {
      final data = jsonDecode(response.body) as List;
      return data.map((c) => MemoryCategory.fromJson(c)).toList();
    }
    return [];
  }

  Future<bool> keepMemory(String id) async {
    final uri = Uri.parse('$serverUrl/api/memories/$id/keep');
    final response = await http.post(uri);
    if (response.statusCode == 200) {
      return jsonDecode(response.body)['ok'] == true;
    }
    return false;
  }

  Future<bool> discardMemory(String id) async {
    final uri = Uri.parse('$serverUrl/api/memories/$id/discard');
    final response = await http.post(uri);
    if (response.statusCode == 200) {
      return jsonDecode(response.body)['ok'] == true;
    }
    return false;
  }

  String imageUrl(String id) => '$serverUrl/api/memories/$id/image';
  String thumbnailUrl(String id) => '$serverUrl/api/memories/$id/thumbnail';
}
