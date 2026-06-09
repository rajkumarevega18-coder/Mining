# Cloudflare captcha template images (optional)

When a Cloudflare challenge appears, the code:

1. **Takes a full-screen screenshot** (same as screen sharing) with pyautogui – not the browser viewport, so coordinates match the real screen and avoid iframe/div offset issues.
2. **Finds the captcha image** in that screenshot (template match with images in this folder). The match position in the image is the **exact screen coordinate**.
3. **Clicks using the OS** (pyautogui) at that screen position so the click is visible and accurate.
4. **Deletes the screenshot** file after clicking.

**Use reference images so the script finds the captcha in the screenshot:**

1. **This folder:** Save crops as **cloudflare_checkbox.png** or **cloudflare_verifying.png** (checkbox or “Verifying…” widget only).
2. **Or use the `reference/` subfolder:** Put **any** of your reference screenshots there (e.g. “Verifying…” spinner + Cloudflare logo, or “Verify you are human” checkbox). Any `.png` in `reference/` is used: we take a full-screen screenshot, search for each reference image in it, and click the center of the first match.
3. Crop **only the widget** (checkbox or Verifying box), not the whole page, so the match is accurate on every system.

**Click is in the wrong place (e.g. 984,564 instead of ~420,420):**  
If no template matches, we use window center. To use a fixed viewport position instead (e.g. widget at 420,420):

```cmd
set CAPTCHA_VIEWPORT_X=420
set CAPTCHA_VIEWPORT_Y=420
```

If the click is still shifted vertically, set `CAPTCHA_CHROME_OFFSET_Y=80` (or 100, 110 – depends on your browser chrome height).

If no template matches and you don't set viewport, the code falls back to the center of the browser window.

**Requirements:**

- **OS click (visible):** `pip install pyautogui pygetwindow`
- **Template matching:** `pip install opencv-python-headless` (or `opencv-python`). If OpenCV is missing, the code uses window center only.
