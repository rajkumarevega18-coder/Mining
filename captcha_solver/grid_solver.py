# captcha_solver/grid_solver.py
"""
Grid CAPTCHA solver: read the instruction text (e.g. "Choose all the hats"),
score each tile image against that prompt with a vision model (CLIP),
and return window-relative (x, y) coordinates to click for matching tiles.
"""
import re
import time
from typing import List, Optional, Tuple

# Optional OCR for prompt extraction (fallback if prompt not in job)
try:
    import pytesseract
    HAS_OCR = True
except ImportError:
    HAS_OCR = False

try:
    from PIL import Image
    import torch
    from transformers import CLIPProcessor, CLIPModel
    HAS_CLIP = True
except ImportError:
    HAS_CLIP = False

# Default grid layout for IOPScience-style CAPTCHA (tune to your window size)
DEFAULT_GRID = {
    "rows": 3,
    "cols": 3,
    "grid_top": 280,      # pixels from top of window content
    "grid_left": 200,     # pixels from left
    "tile_size": 120,
    "gap": 12,
    "content_top_offset": 80,  # skip browser chrome (title bar, etc.)
}


def _extract_keyword_from_prompt(prompt: str) -> str:
    """
    Get the object to select from text like "Choose all the hats" or "Select all animals".
    Returns a short phrase for CLIP (e.g. "hat", "animal").
    """
    if not prompt or not prompt.strip():
        return "object"
    text = prompt.strip().lower()
    # "Choose all the hats" -> "hats", "Select all images with animals" -> "animals"
    for pattern in [
        r"all\s+(?:the\s+)?(\w+)",
        r"select\s+all\s+(?:images?\s+with\s+)?(\w+)",
        r"choose\s+all\s+(?:the\s+)?(\w+)",
        r"(\w+)\s*$",
    ]:
        m = re.search(pattern, text)
        if m:
            word = m.group(1)
            # simple singularize for common cases
            if word.endswith("s") and len(word) > 1:
                word = word[:-1]
            return word
    return text.split()[-1] if text.split() else "object"


def _load_clip():
    """Lazy-load CLIP model and processor (heavy)."""
    if not HAS_CLIP:
        raise RuntimeError("Install: pip install torch transformers pillow")
    model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32")
    processor = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")
    return model, processor


# Module-level cache so we load once per process
_clip_model = None
_clip_processor = None


def _get_clip():
    global _clip_model, _clip_processor
    if _clip_model is None:
        _clip_model, _clip_processor = _load_clip()
    return _clip_model, _clip_processor


def extract_prompt_from_image(img: Image.Image, top_height: int = 120) -> str:
    """
    OCR the top part of the CAPTCHA screenshot to get the instruction text.
    Fallback if the miner does not send the prompt in the job.
    """
    if not HAS_OCR:
        return ""
    try:
        crop = img.crop((0, 0, img.width, min(top_height, img.height)))
        text = pytesseract.image_to_string(crop).strip()
        return text
    except Exception:
        return ""


def crop_tiles(
    img: Image.Image,
    rows: int = 3,
    cols: int = 3,
    grid_top: int = 0,
    grid_left: int = 0,
    tile_size: int = 120,
    gap: int = 12,
) -> List[Image.Image]:
    """Crop a grid of tiles from the full screenshot. Returns list row-by-row (0..rows*cols-1)."""
    tiles = []
    for r in range(rows):
        for c in range(cols):
            x = grid_left + c * (tile_size + gap)
            y = grid_top + r * (tile_size + gap)
            box = (x, y, x + tile_size, y + tile_size)
            if x + tile_size <= img.width and y + tile_size <= img.height:
                tiles.append(img.crop(box).copy())
            else:
                # pad or use black tile so we still have 9
                tiles.append(Image.new("RGB", (tile_size, tile_size), (128, 128, 128)))
    return tiles


def score_tiles_with_clip(
    tile_images: List[Image.Image],
    prompt: str,
    device: Optional[str] = None,
    threshold: float = 0.22,
) -> List[int]:
    """
    Score each tile against the prompt using CLIP. Return indices of tiles
    that match (score >= threshold). prompt should be the object, e.g. "hat".
    """
    if not HAS_CLIP or not tile_images:
        return []
    keyword = _extract_keyword_from_prompt(prompt)
    text_prompt = f"a photo of a {keyword}"
    model, processor = _get_clip()
    if device is None:
        device = "cuda" if torch.cuda.is_available() else "cpu"
    model = model.to(device)

    # Process each tile: get image embedding and compare to text
    inputs = processor(text=[text_prompt], images=tile_images, return_tensors="pt", padding=True)
    inputs = {k: v.to(device) for k, v in inputs.items()}

    with torch.no_grad():
        outputs = model(**inputs)
        # logits_per_image: [num_images, 1]; we want similarity per image
        logits = outputs.logits_per_image.squeeze(-1)
        if logits.dim() == 0:
            logits = logits.unsqueeze(0)
        scores = logits.cpu().float().numpy()

    # Normalize to 0..1 for thresholding (CLIP logits can be negative)
    import numpy as np
    scores = np.asarray(scores)
    smax, smin = float(scores.max()), float(scores.min())
    if smax > smin:
        scores = (scores - smin) / (smax - smin)
    else:
        scores = np.ones_like(scores) * 0.5

    matching = [i for i, s in enumerate(scores) if s >= threshold]
    if not matching and len(scores) >= 2:
        top_k = min(3, len(scores))
        matching = np.argsort(scores)[-top_k:].tolist()
    return matching


def tile_indices_to_coords(
    indices: List[int],
    rows: int,
    cols: int,
    grid_top: int,
    grid_left: int,
    tile_size: int,
    gap: int,
) -> List[Tuple[int, int]]:
    """
    Convert tile indices (0..8 for 3x3) to (x, y) center coordinates
    relative to the window (same coordinate system as the screenshot).
    """
    coords = []
    for idx in indices:
        r, c = idx // cols, idx % cols
        cx = grid_left + c * (tile_size + gap) + tile_size // 2
        cy = grid_top + r * (tile_size + gap) + tile_size // 2
        coords.append((cx, cy))
    return coords


def solve_grid_captcha(
    screenshot: Image.Image,
    prompt: str,
    content_top_offset: int = DEFAULT_GRID["content_top_offset"],
    rows: int = DEFAULT_GRID["rows"],
    cols: int = DEFAULT_GRID["cols"],
    grid_top: int = DEFAULT_GRID["grid_top"],
    grid_left: int = DEFAULT_GRID["grid_left"],
    tile_size: int = DEFAULT_GRID["tile_size"],
    gap: int = DEFAULT_GRID["gap"],
    clip_threshold: float = 0.22,
) -> List[Tuple[int, int]]:
    """
    Full pipeline: crop tiles from screenshot, score with CLIP against prompt,
    return list of (rel_x, rel_y) to click. Coordinates are relative to the
    *window* (including content_top_offset so they match win.left + rel_x).
    """
    # Grid position in the screenshot (screenshot is already of content area if you pass it so)
    # If screenshot is full window, grid_top/grid_left are from top-left of window;
    # we'll return coords relative to window, so add content_top_offset only if
    # screenshot was taken with that offset already applied. Here we assume
    # screenshot is full window → so grid_top already includes any offset we want
    # in "window" coords. So: rel_x = grid_left + ..., rel_y = grid_top + ...
    tiles = crop_tiles(
        screenshot, rows=rows, cols=cols,
        grid_top=grid_top, grid_left=grid_left,
        tile_size=tile_size, gap=gap,
    )
    if not prompt or not prompt.strip():
        prompt = extract_prompt_from_image(screenshot)
    if not prompt or not prompt.strip():
        return []

    matching = score_tiles_with_clip(tiles, prompt, threshold=clip_threshold)
    coords = tile_indices_to_coords(
        matching, rows, cols, grid_top, grid_left, tile_size, gap
    )
    # Coordinates are relative to the image. If the image is the window content
    # starting at (0, 0) of content, then we need to add content_top_offset to y
    # when the screenshot was full window. Actually in our design we take screenshot
    # as full window (win.left, win.top, win.width, win.height). So the image
    # has browser chrome at top. So grid_top in the image is from top of window.
    # So (grid_left + cx, grid_top + cy) are already window-relative. Our tile
    # centers are in "image" coords: (grid_left + c*(tile_size+gap) + tile_size//2, ...).
    # So we're good: rel_x, rel_y are relative to the screenshot which is the window.
    return coords


def get_default_grid_config() -> dict:
    """Return default grid layout for IOPScience-style CAPTCHA."""
    return dict(DEFAULT_GRID)
