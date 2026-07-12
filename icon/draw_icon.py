"""WindowCheck app mark — the original: cream rounded tile, ochre window-square
with one open (top-right) corner and a small swung-open casement flap."""
import sys
from AppKit import (NSBitmapImageRep, NSGraphicsContext, NSColor, NSBezierPath,
                    NSDeviceRGBColorSpace, NSMakeRect, NSMakePoint)

W = 1024
OUT = sys.argv[1]

CREAM = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.957, 0.925, 0.847, 1.0)
OCHRE = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.804, 0.522, 0.157, 1.0)

rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
    None, W, W, 8, 4, True, False, NSDeviceRGBColorSpace, 0, 0)
ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.setCurrentContext_(ctx)

NSColor.clearColor().set()
NSBezierPath.fillRect_(NSMakeRect(0, 0, W, W))

# cream rounded tile
inset, radius = 80, 205
tile = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
    NSMakeRect(inset, inset, W - 2 * inset, W - 2 * inset), radius, radius)
CREAM.set()
tile.fill()

# window square with one open (top-right) corner
lo, hi = 322, 702          # square 322..702, side 380, center 512
gap = 92                   # how far the top & right edges stop short of TR
sw = 58
OCHRE.set()
frame = NSBezierPath.alloc().init()
frame.setLineWidth_(sw)
frame.setLineCapStyle_(1)   # round
frame.setLineJoinStyle_(1)  # round
frame.moveToPoint_(NSMakePoint(hi, hi - gap))     # right edge, below the gap
frame.lineToPoint_(NSMakePoint(hi, lo))           # down to BR
frame.lineToPoint_(NSMakePoint(lo, lo))           # across to BL
frame.lineToPoint_(NSMakePoint(lo, hi))           # up to TL
frame.lineToPoint_(NSMakePoint(hi - gap, hi))     # across top, stop before TR
frame.stroke()

# the swung-open casement: a small square hinged at the open corner
cas = NSBezierPath.alloc().init()
cas.setLineWidth_(sw)
cas.setLineCapStyle_(1)
cas.setLineJoinStyle_(1)
cx, cy = hi + 78, hi + 78     # outside the gap, up-right
cas.moveToPoint_(NSMakePoint(hi - gap, hi))       # hinge on the top edge
cas.lineToPoint_(NSMakePoint(cx, cy))             # up-right to open corner
cas.lineToPoint_(NSMakePoint(hi, hi - gap))       # back down to right edge
cas.stroke()

ctx.flushGraphics()
NSGraphicsContext.restoreGraphicsState()
rep.representationUsingType_properties_(4, {}).writeToFile_atomically_(OUT, True)
print("wrote", OUT)
