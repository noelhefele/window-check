"""WindowCheck mark, 'air' variant: window-square with one open corner, plus
soft breeze currents flowing out of that corner and fading (air dissipating)."""
import sys, math
from AppKit import (NSBitmapImageRep, NSGraphicsContext, NSColor, NSBezierPath,
                    NSDeviceRGBColorSpace, NSMakeRect, NSMakePoint, NSGradient)

W = 1024
OUT = sys.argv[1]

CREAM = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.957, 0.925, 0.847, 1.0)
def ochre(a=1.0):
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(0.804, 0.522, 0.157, a)

rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
    None, W, W, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0)
ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.setCurrentContext_(ctx)

NSColor.clearColor().set()
NSBezierPath.fillRect_(NSMakeRect(0, 0, W, W))

# cream rounded tile
tile = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
    NSMakeRect(80, 80, W - 160, W - 160), 205, 205)
CREAM.set(); tile.fill()

# window square, open top-right corner (shifted slightly left/down to give air room)
lo, hi, gap, sw = 300, 648, 92, 58
ochre(1.0).set()
frame = NSBezierPath.alloc().init()
frame.setLineWidth_(sw); frame.setLineCapStyle_(1); frame.setLineJoinStyle_(1)
frame.moveToPoint_(NSMakePoint(hi, hi - gap))
frame.lineToPoint_(NSMakePoint(hi, lo))
frame.lineToPoint_(NSMakePoint(lo, lo))
frame.lineToPoint_(NSMakePoint(lo, hi))
frame.lineToPoint_(NSMakePoint(hi - gap, hi))
frame.stroke()

def wave(x0, x1, y, amp, cycles, n=48):
    return [(x0 + (x1 - x0) * (i / n),
             y + amp * math.sin(cycles * math.pi * (i / n)))
            for i in range(n + 1)]

def ribbon(pts, width, taper=0.6):
    """Build a closed tapered ribbon (thick->thin) following a centerline."""
    n = len(pts)
    top, bot = [], []
    for i in range(n):
        if i == 0:
            dx, dy = pts[1][0] - pts[0][0], pts[1][1] - pts[0][1]
        elif i == n - 1:
            dx, dy = pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]
        else:
            dx, dy = pts[i + 1][0] - pts[i - 1][0], pts[i + 1][1] - pts[i - 1][1]
        L = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / L, dx / L
        w = (width / 2) * (1 - taper * (i / (n - 1)))
        top.append((pts[i][0] + nx * w, pts[i][1] + ny * w))
        bot.append((pts[i][0] - nx * w, pts[i][1] - ny * w))
    path = NSBezierPath.alloc().init()
    path.moveToPoint_(NSMakePoint(*top[0]))
    for p in top[1:]:
        path.lineToPoint_(NSMakePoint(*p))
    for p in reversed(bot):
        path.lineToPoint_(NSMakePoint(*p))
    path.closePath()
    return path

# two smooth currents streaming out of the open corner, fading left->right
grad = NSGradient.alloc().initWithStartingColor_endingColor_(ochre(0.9), ochre(0.0))
grad.drawInBezierPath_angle_(ribbon(wave(hi - gap + 30, 905, hi + 4, 22, 2.1), 34), 0.0)
grad.drawInBezierPath_angle_(ribbon(wave(hi + 4, 905, hi - gap - 26, 18, 2.1), 28), 0.0)

ctx.flushGraphics()
NSGraphicsContext.restoreGraphicsState()
rep.representationUsingType_properties_(4, {}).writeToFile_atomically_(OUT, True)
print("wrote", OUT)
