import 'package:flutter/material.dart';
import 'package:geolocator/geolocator.dart';
import 'package:google_maps_flutter/google_maps_flutter.dart';
import 'package:url_launcher/url_launcher.dart';

import '../services/map_service.dart';
import '../theme/brand.dart';
import '../widgets/brand_logo.dart';

/// تعمیرگاه‌های نزدیک — لیست از API بک‌اند (`/api/v1/garages/nearby`).
class GaragesScreen extends StatefulWidget {
  const GaragesScreen({super.key});

  @override
  State<GaragesScreen> createState() => _GaragesScreenState();
}

class _GaragesScreenState extends State<GaragesScreen> {
  final _garageService = GarageService();
  final _searchCtrl = TextEditingController();

  bool _loading = true;
  String? _error;
  LatLng? _userLocation;
  List<Garage> _garages = [];

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    _searchCtrl.dispose();
    _garageService.dispose();
    super.dispose();
  }

  Future<void> _load({String? query}) async {
    setState(() {
      _loading = true;
      _error = null;
    });

    try {
      final loc = await _resolveLocation();
      final garages = await _garageService.findNearbyGarages(
        loc,
        config: GarageSearchConfig(
          radiusMeters: 8000,
          maxResults: 40,
          keyword: query,
        ),
      );
      if (!mounted) return;
      setState(() {
        _userLocation = loc;
        _garages = garages;
        _loading = false;
      });
    } on GarageException catch (e) {
      if (!mounted) return;
      setState(() {
        _error = e.message;
        _loading = false;
      });
    } catch (_) {
      if (!mounted) return;
      setState(() {
        _error = 'موقعیت یا لیست تعمیرگاه‌ها در دسترس نیست.';
        _loading = false;
      });
    }
  }

  Future<LatLng> _resolveLocation() async {
    var permission = await Geolocator.checkPermission();
    if (permission == LocationPermission.denied) {
      permission = await Geolocator.requestPermission();
    }
    if (permission == LocationPermission.denied ||
        permission == LocationPermission.deniedForever) {
      throw const GarageException(
        'برای نمایش تعمیرگاه نزدیک، دسترسی موقعیت را فعال کنید.',
        type: GarageErrorType.invalidRequest,
      );
    }

    final enabled = await Geolocator.isLocationServiceEnabled();
    if (!enabled) {
      throw const GarageException(
        'سرویس موقعیت دستگاه خاموش است.',
        type: GarageErrorType.invalidRequest,
      );
    }

    final pos = await Geolocator.getCurrentPosition(
      locationSettings: const LocationSettings(
        accuracy: LocationAccuracy.medium,
        timeLimit: Duration(seconds: 12),
      ),
    );
    return LatLng(pos.latitude, pos.longitude);
  }

  Future<void> _openMaps(Garage g) async {
    final uri = Uri.parse(
      'https://www.google.com/maps/search/?api=1&query=${g.location.latitude},${g.location.longitude}',
    );
    if (await canLaunchUrl(uri)) {
      await launchUrl(uri, mode: LaunchMode.externalApplication);
    }
  }

  Future<void> _call(Garage g) async {
    final phone = g.phoneNumber?.replaceAll(' ', '') ?? '';
    if (phone.isEmpty) return;
    final uri = Uri.parse('tel:$phone');
    if (await canLaunchUrl(uri)) {
      await launchUrl(uri);
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);

    return Scaffold(
      appBar: AppBar(
        title: BrandWordmark(compact: true, markSize: 28),
      ),
      body: Column(
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 8, 16, 4),
            child: TextField(
              controller: _searchCtrl,
              textInputAction: TextInputAction.search,
              onSubmitted: (v) => _load(query: v.trim().isEmpty ? null : v.trim()),
              decoration: InputDecoration(
                hintText: 'جستجوی نام یا تخصص (مثلاً گیربکس)',
                prefixIcon: const Icon(Icons.search_rounded),
                suffixIcon: IconButton(
                  tooltip: 'جستجو',
                  onPressed: () => _load(
                    query: _searchCtrl.text.trim().isEmpty
                        ? null
                        : _searchCtrl.text.trim(),
                  ),
                  icon: const Icon(Icons.search_rounded),
                ),
              ),
            ),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 0, 16, 8),
            child: Align(
              alignment: Alignment.centerRight,
              child: Text(
                _userLocation == null
                    ? 'تعمیرگاه‌های نزدیک از دیتابیس مکانیک هوشمند'
                    : 'بر اساس موقعیت فعلی شما (${_userLocation!.latitude.toStringAsFixed(3)}, ${_userLocation!.longitude.toStringAsFixed(3)})',
                style: TextStyle(fontSize: 12, color: theme.hintColor),
              ),
            ),
          ),
          Expanded(child: _buildBody(theme)),
        ],
      ),
    );
  }

  Widget _buildBody(ThemeData theme) {
    if (_loading) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_error != null) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(Icons.location_off_rounded, size: 48, color: theme.hintColor),
              const SizedBox(height: 12),
              Text(_error!, textAlign: TextAlign.center),
              const SizedBox(height: 16),
              ElevatedButton(
                onPressed: () => _load(),
                child: const Text('تلاش دوباره'),
              ),
            ],
          ),
        ),
      );
    }
    if (_garages.isEmpty) {
      return Center(
        child: Text(
          'تعمیرگاهی در این محدوده ثبت نشده است.',
          style: TextStyle(color: theme.hintColor),
        ),
      );
    }

    return RefreshIndicator(
      color: Brand.amber,
      onRefresh: () => _load(
        query: _searchCtrl.text.trim().isEmpty ? null : _searchCtrl.text.trim(),
      ),
      child: ListView.separated(
        padding: const EdgeInsets.fromLTRB(16, 4, 16, 24),
        itemCount: _garages.length,
        separatorBuilder: (_, __) => const SizedBox(height: 10),
        itemBuilder: (context, i) => _GarageTile(
          garage: _garages[i],
          onCall: () => _call(_garages[i]),
          onMap: () => _openMaps(_garages[i]),
        ),
      ),
    );
  }
}

class _GarageTile extends StatelessWidget {
  final Garage garage;
  final VoidCallback onCall;
  final VoidCallback onMap;

  const _GarageTile({
    required this.garage,
    required this.onCall,
    required this.onMap,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: theme.cardColor,
        borderRadius: BorderRadius.circular(16),
        border: Border.all(
          color: garage.isPremium
              ? Brand.amber.withOpacity(0.45)
              : theme.dividerColor,
        ),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  garage.name,
                  style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 15),
                ),
              ),
              if (garage.badges.isNotEmpty)
                Wrap(
                  spacing: 4,
                  children: garage.badges
                      .map(
                        (b) => Chip(
                          visualDensity: VisualDensity.compact,
                          label: Text(b, style: const TextStyle(fontSize: 11)),
                          backgroundColor: Brand.amber.withOpacity(0.18),
                          side: BorderSide.none,
                        ),
                      )
                      .toList(),
                ),
            ],
          ),
          if (garage.address != null && garage.address!.isNotEmpty) ...[
            const SizedBox(height: 4),
            Text(garage.address!, style: TextStyle(color: theme.hintColor, fontSize: 12)),
          ],
          const SizedBox(height: 8),
          Wrap(
            spacing: 10,
            runSpacing: 4,
            children: [
              if (garage.distanceLabel.isNotEmpty)
                Text(garage.distanceLabel, style: const TextStyle(fontSize: 12)),
              Text(garage.ratingLabel, style: const TextStyle(fontSize: 12)),
              if (garage.openStatusLabel.isNotEmpty)
                Text(garage.openStatusLabel, style: const TextStyle(fontSize: 12)),
            ],
          ),
          if (garage.specialties.isNotEmpty) ...[
            const SizedBox(height: 6),
            Text(
              garage.specialties.join(' · '),
              style: TextStyle(fontSize: 12, color: theme.colorScheme.secondary),
            ),
          ],
          const SizedBox(height: 10),
          Row(
            children: [
              if (garage.hasPhone)
                TextButton.icon(
                  onPressed: onCall,
                  icon: const Icon(Icons.call_rounded, size: 18),
                  label: const Text('تماس'),
                ),
              TextButton.icon(
                onPressed: onMap,
                icon: const Icon(Icons.map_rounded, size: 18),
                label: const Text('مسیریابی'),
              ),
            ],
          ),
        ],
      ),
    );
  }
}
