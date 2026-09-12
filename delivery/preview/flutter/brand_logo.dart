import 'package:flutter/material.dart';

import '../theme/brand.dart';

/// لوگوی دایره‌ای برند (فایل ریشه `logo.png` → `assets/branding/mark.png`).
class BrandMark extends StatelessWidget {
  final double size;
  final bool glow;

  const BrandMark({super.key, this.size = 72, this.glow = false});

  @override
  Widget build(BuildContext context) {
    final image = Image.asset(
      Brand.markAsset,
      width: size,
      height: size,
      filterQuality: FilterQuality.high,
      errorBuilder: (_, __, ___) => Icon(
        Icons.handyman_rounded,
        size: size * 0.72,
        color: Brand.amber,
      ),
    );

    if (!glow) return image;

    return Container(
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        boxShadow: [
          BoxShadow(
            color: Brand.amber.withOpacity(0.28),
            blurRadius: size * 0.35,
            spreadRadius: 1,
          ),
        ],
      ),
      child: image,
    );
  }
}

/// علامت + نام فارسی (+ شعار اختیاری) برای اسپلش، ورود و AppBar.
class BrandWordmark extends StatelessWidget {
  final double markSize;
  final bool showSlogan;
  final bool compact;
  final Color? titleColor;

  const BrandWordmark({
    super.key,
    this.markSize = 56,
    this.showSlogan = false,
    this.compact = false,
    this.titleColor,
  });

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final color = titleColor ?? theme.colorScheme.secondary;

    if (compact) {
      return Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          BrandMark(size: markSize),
          const SizedBox(width: 8),
          Flexible(
            child: Text(
              Brand.nameFa,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(
                fontWeight: FontWeight.bold,
                fontSize: 17,
                color: color,
              ),
            ),
          ),
        ],
      );
    }

    return Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        BrandMark(size: markSize, glow: true),
        const SizedBox(height: 14),
        Text(
          Brand.nameFa,
          textAlign: TextAlign.center,
          style: TextStyle(
            fontSize: markSize > 64 ? 26 : 22,
            fontWeight: FontWeight.bold,
            color: color,
          ),
        ),
        if (showSlogan) ...[
          const SizedBox(height: 6),
          Text(
            Brand.slogan,
            textAlign: TextAlign.center,
            style: TextStyle(
              fontSize: 13,
              color: theme.hintColor,
            ),
          ),
        ],
      ],
    );
  }
}
