#!/usr/bin/env python3
"""Regenerates electron/assets/Mira.icns from the same logo geometry used
inline in workspace.html and the menu bar icon. Run this if the logo design
changes; scripts/build_app.js just consumes the resulting .icns unchanged.

Requires PyObjC (ships with macOS's system Python), and the `iconutil`
command-line tool (also built into macOS).
"""
import os
import shutil
import subprocess
import Quartz
import CoreFoundation

ASSETS_DIR = os.path.join(os.path.dirname(__file__), '..', 'assets')
ICONSET_DIR = os.path.join(ASSETS_DIR, 'Mira.iconset')
ICNS_PATH = os.path.join(ASSETS_DIR, 'Mira.icns')


def draw(size, path_out):
    s = float(size)
    cs = Quartz.CGColorSpaceCreateDeviceRGB()
    ctx = Quartz.CGBitmapContextCreate(None, size, size, 8, size * 4, cs,
                                        Quartz.kCGImageAlphaPremultipliedLast)

    def P(x, y):
        return (x / 128.0 * s, (128.0 - y) / 128.0 * s)

    rect = Quartz.CGRectMake(0, 0, s, s)
    radius = s * 30.0 / 128.0
    path = Quartz.CGPathCreateWithRoundedRect(rect, radius, radius, None)
    Quartz.CGContextAddPath(ctx, path)
    Quartz.CGContextClip(ctx)

    grad_cs = Quartz.CGColorSpaceCreateDeviceRGB()
    colors = [
        (0.545, 0.361, 0.965, 1.0),  # #8b5cf6
        (0.388, 0.400, 0.945, 1.0),  # #6366f1
        (0.024, 0.714, 0.831, 1.0),  # #06b6d4
    ]
    flat = [c for color in colors for c in color]
    gradient = Quartz.CGGradientCreateWithColorComponents(grad_cs, flat, [0.0, 0.45, 1.0], 3)
    Quartz.CGContextDrawLinearGradient(ctx, gradient, (0, s), (s, 0), 0)
    Quartz.CGContextResetClip(ctx)

    Quartz.CGContextSetRGBFillColor(ctx, 1, 1, 1, 1)
    r = s * 6.5 / 128.0
    cx, cy = P(64, 30)
    Quartz.CGContextFillEllipseInRect(ctx, Quartz.CGRectMake(cx - r, cy - r, r * 2, r * 2))

    Quartz.CGContextSetRGBStrokeColor(ctx, 1, 1, 1, 1)
    Quartz.CGContextSetLineWidth(ctx, s * 11.0 / 128.0)
    Quartz.CGContextSetLineCap(ctx, Quartz.kCGLineCapRound)
    Quartz.CGContextSetLineJoin(ctx, Quartz.kCGLineJoinRound)
    Quartz.CGContextBeginPath(ctx)
    Quartz.CGContextMoveToPoint(ctx, *P(36, 90))
    Quartz.CGContextAddLineToPoint(ctx, *P(36, 46))
    Quartz.CGContextAddLineToPoint(ctx, *P(64, 76))
    Quartz.CGContextAddLineToPoint(ctx, *P(92, 46))
    Quartz.CGContextAddLineToPoint(ctx, *P(92, 90))
    Quartz.CGContextStrokePath(ctx)

    img = Quartz.CGBitmapContextCreateImage(ctx)
    url = CoreFoundation.CFURLCreateWithFileSystemPath(None, path_out, 0, False)
    dest = Quartz.CGImageDestinationCreateWithURL(url, "public.png", 1, None)
    Quartz.CGImageDestinationAddImage(dest, img, None)
    Quartz.CGImageDestinationFinalize(dest)


def main():
    if os.path.exists(ICONSET_DIR):
        shutil.rmtree(ICONSET_DIR)
    os.makedirs(ICONSET_DIR)

    # (nominal size, filename) pairs an .iconset directory must contain
    for px, name in [
        (16, 'icon_16x16.png'), (32, 'icon_16x16@2x.png'),
        (32, 'icon_32x32.png'), (64, 'icon_32x32@2x.png'),
        (128, 'icon_128x128.png'), (256, 'icon_128x128@2x.png'),
        (256, 'icon_256x256.png'), (512, 'icon_256x256@2x.png'),
        (512, 'icon_512x512.png'), (1024, 'icon_512x512@2x.png'),
    ]:
        draw(px, os.path.join(ICONSET_DIR, name))

    subprocess.run(['iconutil', '-c', 'icns', ICONSET_DIR, '-o', ICNS_PATH], check=True)
    shutil.rmtree(ICONSET_DIR)
    print(f"wrote {ICNS_PATH}")


if __name__ == '__main__':
    main()
