"""Shared image scaling, region and grid rendering."""
import io
from _screenshots import CaptureError, MAX_PNG_BYTES

MAX_PIXELS = 24_000_000


def transform(image, scale, grid, region):
    from PIL import Image, ImageDraw, ImageFont
    original = image.size
    x, y, w, h = region or [0, 0, *original]
    if x + w > original[0] or y + h > original[1]:
        raise CaptureError('invalid_screenshot_region', 'region extends beyond the screenshot. Use its original_width and original_height.')
    image = image.crop((x, y, x + w, y + h))
    image = image.resize((max(1, w * scale // 100), max(1, h * scale // 100)), Image.Resampling.LANCZOS)
    if grid:
        draw = ImageDraw.Draw(image)
        font_size = max(8, 11 + int(11 * (max(image.size) - 1000) / 2000))
        try:
            font = ImageFont.truetype('arial.ttf', font_size)
        except OSError:
            font = ImageFont.load_default()
        labels = []
        for coord in range((x // 100 + 1) * 100, x + w, 100):
            px = (coord - x) * image.width // w
            draw.line((px, 0, px, image.height), fill='#ff5050')
            labels.append((px + 2, 2, str(coord)))
        for coord in range((y // 100 + 1) * 100, y + h, 100):
            py = (coord - y) * image.height // h
            draw.line((0, py, image.width, py), fill='#ff5050')
            labels.append((2, py + 2, str(coord)))
        # Paint labels last so grid lines cannot cross their opaque backgrounds.
        for left, top, label in labels:
            bounds = draw.textbbox((0, 0), label, font=font)
            label_width, label_height = bounds[2] - bounds[0], bounds[3] - bounds[1]
            left = max(0, min(left, image.width - label_width - 4))
            top = max(0, min(top, image.height - label_height - 4))
            draw.rectangle((left, top, left + label_width + 3, top + label_height + 3), fill='black')
            draw.text((left + 2 - bounds[0], top + 2 - bounds[1]), label, fill='#ffff64', font=font)
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    png = buffer.getvalue()
    if len(png) > MAX_PNG_BYTES:
        raise CaptureError('screenshot_too_large', 'The image is too large. Reduce scale or request a smaller region.')
    return png, dict(width=image.width, height=image.height, original_width=original[0],
                     original_height=original[1], region=[x, y, w, h], scale=scale, grid=grid)

