import 'dart:convert';
import 'dart:io';
import 'dart:async';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import '../constants.dart';
import '../models/car.dart';
import '../models/diagnostic.dart';

// ─────────────────────────────────────────────────────────────────────────────
// ── استثناء API ──
// ─────────────────────────────────────────────────────────────────────────────
class ApiException implements Exception {
  final int statusCode;
  final String message;
  final Map<String, dynamic>? details;

  const ApiException(this.statusCode, this.message, {this.details});

  bool get isUnauthorized => statusCode == 401;
  bool get isPaymentRequired => statusCode == 402;
  bool get isForbidden => statusCode == 403;
  bool get isNotFound => statusCode == 404;
  bool get isServerError => statusCode >= 500;
  bool get isNetworkError => statusCode == 0;
  bool get isTimeout => statusCode == 408;

  @override
  String toString() => 'ApiException($statusCode): $message';
}

// ─────────────────────────────────────────────────────────────────────────────
// ── نتیجه عملیات API ──
// ─────────────────────────────────────────────────────────────────────────────
class ApiResponse<T> {
  final T data;
  final int statusCode;
  final Map<String, dynamic>? meta;

  const ApiResponse({required this.data, required this.statusCode, this.meta});
}

// ─────────────────────────────────────────────────────────────────────────────
// ── پارامترهای pagination ──
// ─────────────────────────────────────────────────────────────────────────────
class PaginationParams {
  final int page;
  final int limit;

  const PaginationParams({this.page = 1, this.limit = 20});

  Map<String, String> toQueryParams() {
    final safePage = page < 1 ? 1 : page;
    return {
      'page': safePage.toString(),
      'limit': limit.toString(),
      'offset': ((safePage - 1) * limit).toString(),
    };
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// ── سرویس اصلی ──
// ─────────────────────────────────────────────────────────────────────────────
class ApiService {
  final http.Client _httpClient;

  final Duration _defaultTimeout;
  final Duration _diagnoseTimeout;
  final Duration _uploadTimeout;

  List<Car>? _carsCache;
  DateTime? _carsCacheTime;
  static const _carsCacheDuration = Duration(hours: 6);

  final Map<String, DateTime> _lastRequestTime = {};
  static const _minRequestInterval = Duration(milliseconds: 500);

  ApiService({
    http.Client? httpClient,
    Duration defaultTimeout = const Duration(seconds: 20),
    Duration diagnoseTimeout = const Duration(seconds: 60),
    Duration uploadTimeout = const Duration(seconds: 90),
  })  : _httpClient = httpClient ?? http.Client(),
        _defaultTimeout = defaultTimeout,
        _diagnoseTimeout = diagnoseTimeout,
        _uploadTimeout = uploadTimeout;

  // ─────────────────────────────────────────
  // ── هدرها ──
  // ─────────────────────────────────────────
  Map<String, String> _getHeaders([String? token]) {
    return {
      'Content-Type': 'application/json',
      'Accept': 'application/json',
      if (token != null) 'Authorization': 'Bearer $token',
    };
  }

  Map<String, String> _getMultipartHeaders(String token) {
    return {
      'Accept': 'application/json',
      'Authorization': 'Bearer $token',
    };
  }

  // ─────────────────────────────────────────
  // ── parse پاسخ ──
  // ─────────────────────────────────────────
  dynamic _parseBody(http.Response response) {
    if (response.body.isEmpty) return <String, dynamic>{};
    try {
      return jsonDecode(utf8.decode(response.bodyBytes));
    } catch (_) {
      throw ApiException(response.statusCode, 'خطا در پردازش پاسخ سرور.');
    }
  }

  Map<String, dynamic> _parseAndEnsure(
    http.Response response, {
    String? defaultError,
  }) {
    final data = _parseBody(response);

    if (response.statusCode >= 400 || (data is Map && data['success'] == false)) {
      final msg = data is Map
          ? (data['error'] ?? data['message'] ?? defaultError ?? 'خطای سرور')
          : (defaultError ?? 'خطای ناشناخته');

      final details = data is Map ? Map<String, dynamic>.from(data) : null;

      throw ApiException(response.statusCode, msg.toString(), details: details);
    }

    // ✅ اگر سرور لیست برگرداند، آن را در یک مپ می‌پیچیم
    if (data is List) return {'data': data};
    return data is Map<String, dynamic> ? data : <String, dynamic>{'data': data};
  }

  // ─────────────────────────────────────────
  // ── rate limiting ──
  // ─────────────────────────────────────────
  Future<void> _checkRateLimit(String key) async {
    final last = _lastRequestTime[key];
    if (last != null) {
      final elapsed = DateTime.now().difference(last);
      if (elapsed < _minRequestInterval) {
        await Future.delayed(_minRequestInterval - elapsed);
      }
    }
    // ✅ ثبت زمان دقیقاً قبل از ارسال درخواست
    _lastRequestTime[key] = DateTime.now();
  }

  // ─────────────────────────────────────────
  // ── ارسال امن با مدیریت خطا ──
  // ─────────────────────────────────────────
  Future<http.Response> _safeCall(
    Future<http.Response> Function() call, {
    Duration? timeout,
    String? rateLimitKey,
  }) async {
    if (rateLimitKey != null) {
      await _checkRateLimit(rateLimitKey);
    }

    try {
      final response = await call().timeout(timeout ?? _defaultTimeout);
      _log('${response.statusCode}', response.request?.url.toString() ?? '');
      return response;
    } on SocketException catch (e) {
      _logError('SocketException', e.message);
      throw const ApiException(0, 'عدم اتصال به اینترنت. لطفاً شبکه خود را بررسی کنید.');
    } on TimeoutException {
      _logError('Timeout', 'درخواست بیش از حد طول کشید.');
      throw const ApiException(408, 'زمان درخواست پایان یافت. لطفاً دوباره تلاش کنید.');
    } on HandshakeException catch (e) {
      _logError('TLS Error', e.message);
      throw const ApiException(0, 'خطا در اتصال امن. لطفاً اینترنت خود را بررسی کنید.');
    } catch (e) {
      if (e is ApiException) rethrow;
      _logError('Unknown', e.toString());
      throw const ApiException(500, 'خطای نامشخص در برقراری ارتباط با سرور.');
    }
  }

  void _log(String status, String url) => debugPrint('[API] $status → ${_truncate(url)}');
  void _logError(String type, String msg) => debugPrint('[API Error] $type: ${_truncate(msg)}');
  String _truncate(String s, {int max = 100}) => s.length > max ? '${s.substring(0, max)}...' : s;

  // ─────────────────────────────────────────
  // ── لیست خودروها (با cache) ──
  // ─────────────────────────────────────────
  Future<List<Car>> getCars({bool forceRefresh = false}) async {
    if (!forceRefresh &&
        _carsCache != null &&
        _carsCacheTime != null &&
        DateTime.now().difference(_carsCacheTime!) < _carsCacheDuration) {
      debugPrint('[API] لیست خودروها از cache برگشت داده شد.');
      return _carsCache!;
    }

    // ✅ ساخت ایمن URL با Uri — اول /cars.json سپس /api/v1/cars
    Future<http.Response> fetchCars(Uri uri) => _safeCall(
          () => _httpClient.get(uri, headers: {'Accept': 'application/json'}),
          rateLimitKey: 'getCars',
        );

    http.Response response;
    try {
      final baseUri = Uri.parse(Constants.baseUrl);
      final carsUri = baseUri.replace(path: '/cars.json', queryParameters: null);
      response = await fetchCars(carsUri);
      if (response.statusCode >= 400) {
        response = await fetchCars(Uri.parse(Constants.carsApi));
      }
    } on ApiException {
      response = await fetchCars(Uri.parse(Constants.carsApi));
    }

    final body = _parseBody(response);
    final List<dynamic> rawList = body is Map && body.containsKey('cars')
        ? body['cars'] as List<dynamic>
        : (body is List ? body : []);

    final cars = rawList.map((j) => Car.fromJson(j as Map<String, dynamic>)).toList();

    _carsCache = cars;
    _carsCacheTime = DateTime.now();

    return cars;
  }

  void clearCarsCache() {
    _carsCache = null;
    _carsCacheTime = null;
  }

  // ─────────────────────────────────────────
  // ─ـ احراز هویت و پروفایل ──
  // ─────────────────────────────────────────
  Future<void> sendOtp(String phone) async {
    final response = await _safeCall(
      () => _httpClient.post(
        Uri.parse(Constants.account),
        headers: _getHeaders(),
        body: jsonEncode({'action': 'send', 'phone': phone}),
      ),
      rateLimitKey: 'sendOtp_$phone',
    );
    _parseAndEnsure(response, defaultError: 'خطا در ارسال کد تأیید');
  }

  Future<Map<String, dynamic>> verifyOtp(String phone, String code, {String? referralCode}) async {
    final body = <String, dynamic>{
      'action': 'verify',
      'phone': phone,
      'code': code,
      if (referralCode != null && referralCode.trim().isNotEmpty) 'referralCode': referralCode.trim(),
    };

    final response = await _safeCall(
      () => _httpClient.post(
        Uri.parse(Constants.account),
        headers: _getHeaders(),
        body: jsonEncode(body),
      ),
      rateLimitKey: 'verifyOtp_$phone',
    );

    return _parseAndEnsure(response, defaultError: 'خطا در ورود');
  }

  Future<Map<String, dynamic>> getProfile(String token) async {
    final response = await _safeCall(
      () => _httpClient.get(Uri.parse(Constants.credits), headers: _getHeaders(token)),
      rateLimitKey: 'getProfile',
    );
    return _parseAndEnsure(response, defaultError: 'خطا در دریافت پروفایل');
  }

  Future<void> requestWithdraw(String token, {required int amount, required String cardNumber, required String fullName}) async {
    final response = await _safeCall(
      () => _httpClient.post(
        Uri.parse(Constants.withdraw),
        headers: _getHeaders(token),
        body: jsonEncode({'amount': amount, 'cardNumber': cardNumber, 'fullName': fullName}),
      ),
      rateLimitKey: 'withdraw',
    );
    _parseAndEnsure(response, defaultError: 'خطا در ثبت درخواست برداشت');
  }

  // ─────────────────────────────────────────
  // ─ـ عیب‌یابی ──
  // ─────────────────────────────────────────
  Future<String> diagnose(String token, String carId, String description, {required String year, String? carName}) async {
    final body = <String, dynamic>{
      'carId': carId,
      'year': year,
      'description': description,
      if (carName != null && carName.trim().isNotEmpty) 'carName': carName.trim(),
    };

    final response = await _safeCall(
      () => _httpClient.post(
        Uri.parse(Constants.diagnose),
        headers: _getHeaders(token),
        body: jsonEncode(body),
      ),
      timeout: _diagnoseTimeout,
      rateLimitKey: 'diagnose',
    );

    final data = _parseAndEnsure(response, defaultError: 'خطا در عیب‌یابی');
    final result = _extractResult(data);
    if (result == null || result.isEmpty) throw const ApiException(500, 'سرور نتیجه‌ای برنگرداند.');
    return result;
  }

  String? _extractResult(Map<String, dynamic> data) {
    if (data['data'] is Map) {
      final d = data['data'] as Map;
      return d['result']?.toString() ?? d['answer']?.toString() ?? d['text']?.toString();
    }
    return data['result']?.toString() ?? data['answer']?.toString() ?? data['text']?.toString();
  }

  // ✅ پارامتر onProgress حذف شد چون در http پیش‌فرض پشتیبانی نمی‌شود
  Future<String> uploadAudioAndDiagnose(
    String token, {
    required String filePath,
    required String carId,
    required String year,
    String? carName,
  }) async {
    final file = File(filePath);
    if (!await file.exists()) throw const ApiException(0, 'فایل صوتی پیدا نشد.');

    final fileSize = await file.length();
    debugPrint('[API] آپلود فایل صوتی: ${fileSize ~/ 1024} KB');

    final uri = Uri.parse(Constants.diagnoseAudio);
    final request = http.MultipartRequest('POST', uri)
      ..headers.addAll(_getMultipartHeaders(token))
      ..fields['carId'] = carId
      ..fields['year'] = year
      ..files.add(await http.MultipartFile.fromPath('audio', filePath, filename: 'engine_sound.m4a'));

    if (carName != null && carName.trim().isNotEmpty) request.fields['carName'] = carName.trim();

    try {
      final streamedResponse = await request.send().timeout(_uploadTimeout);
      final response = await http.Response.fromStream(streamedResponse);

      final data = _parseAndEnsure(response, defaultError: 'خطا در آپلود و تحلیل صدا');
      final result = _extractResult(data);
      if (result == null || result.isEmpty) throw const ApiException(500, 'سرور نتیجه تحلیل صدا را برنگرداند.');
      return result;
    } on ApiException {
      rethrow;
    } on TimeoutException {
      throw const ApiException(408, 'آپلود فایل بیش از حد طول کشید. لطفاً دوباره تلاش کنید.');
    } catch (e) {
      throw ApiException(500, 'خطا در آپلود فایل: $e');
    }
  }

  // ─────────────────────────────────────────
  // ─ـ تاریخچه ──
  // ─────────────────────────────────────────
  Future<List<Diagnostic>> getHistory(String token, {PaginationParams? pagination}) async {
    final params = {'history': 'true', ...?(pagination?.toQueryParams())};
    final uri = Uri.parse(Constants.diagnose).replace(queryParameters: params);

    final response = await _safeCall(
      () => _httpClient.get(uri, headers: _getHeaders(token)),
      rateLimitKey: 'getHistory',
    );

    final data = _parseAndEnsure(response, defaultError: 'خطا در دریافت تاریخچه');

    // ✅ پارس تمیزتر و بدون تداخل
    final List<dynamic> rawList;
    if (data['data'] is List) {
      rawList = data['data'] as List;
    } else if (data['history'] is List) {
      rawList = data['history'] as List;
    } else if (data['items'] is List) {
      rawList = data['items'] as List;
    } else {
      rawList = [];
    }

    return rawList.whereType<Map<String, dynamic>>().map(Diagnostic.fromJson).toList();
  }

  Future<void> deleteHistory(String token, String diagnosticId) async {
    if (diagnosticId.isEmpty) throw const ApiException(400, 'شناسه تاریخچه نامعتبر است.');

    final uri = Uri.parse('${Constants.diagnose}/$diagnosticId');
    final response = await _safeCall(
      () => _httpClient.delete(uri, headers: _getHeaders(token)),
      rateLimitKey: 'deleteHistory',
    );
    _parseAndEnsure(response, defaultError: 'خطا در حذف تاریخچه');
  }

  // ─────────────────────────────────────────
  // ─ـ پرداخت ──
  // ─────────────────────────────────────────
  Future<String> getPaymentUrl(String token, String productId) async {
    if (productId.isEmpty) throw const ApiException(400, 'شناسه محصول نامعتبر است.');

    final response = await _safeCall(
      () => _httpClient.post(
        Uri.parse(Constants.purchase),
        headers: _getHeaders(token),
        body: jsonEncode({'productId': productId}),
      ),
      rateLimitKey: 'getPaymentUrl',
    );

    final data = _parseAndEnsure(response, defaultError: 'خطا در ایجاد درگاه پرداخت');
    final url = data['paymentUrl']?.toString() ?? data['payment_url']?.toString() ?? data['url']?.toString();

    if (url == null || url.isEmpty) throw const ApiException(500, 'لینک پرداخت از سرور دریافت نشد.');
    return url;
  }

  Future<List<Map<String, dynamic>>> getProducts() async {
    try {
      final response = await _safeCall(
        () => _httpClient.get(
          Uri.parse(Constants.products),
          headers: {'Accept': 'application/json'},
        ),
        rateLimitKey: 'getProducts',
      );
      final data = _parseAndEnsure(response, defaultError: 'خطا در دریافت بسته‌ها');
      final raw = data['data'] is List ? data['data'] as List : const [];
      return raw.whereType<Map>().map((e) => Map<String, dynamic>.from(e)).toList();
    } catch (_) {
      return const [];
    }
  }

  void dispose() {
    _httpClient.close();
    _lastRequestTime.clear();
    debugPrint('[API] ApiService بسته شد.');
  }
}
