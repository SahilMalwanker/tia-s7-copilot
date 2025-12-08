# tools/

Scripts that generate the images in [`docs/assets`](../docs/assets). The application does not use them.

| Script | Output | Needs |
|---|---|---|
| `make_readme_art.py` | `hero-light.svg`, `hero-dark.svg`, `cell-cycle.svg`, `hardware.svg` | Python standard library only |
| `make_charts.py` | `results-light.svg`, `results-dark.svg` | `matplotlib` |
| `capture_screenshots.py` | `dashboard-light.png`, `dashboard-dark.png` | `playwright` and a Chromium-based browser |

```bash
python tools/make_readme_art.py
python tools/make_charts.py
python tools/capture_screenshots.py                          # bundled Chromium, headless
python tools/capture_screenshots.py --channel msedge --headed # use the installed Edge instead
```

- **SVG artwork** is written by hand as SVG markup. The hero and wiring diagrams use CSS
  animations, which stop when `prefers-reduced-motion` is set. The cell replay uses SMIL, so
  GitHub can animate it inside an `<img>` tag. It shows the real step names, PLC addresses and
  position triggers from the robot controller.
- **Charts** plot the cycle-time interpolation and the energy model used by the copilot against
  the cycle times measured on the cell (17 cycles at 50, 70 and 90 % speed).
- **Screenshots** serve the real dashboard from `tia_s7_copilot/web` and answer every `/api/*` call
  from a mocked state (80 % speed, arms 2 and 3 assembling), so no PLC, robots or LLM key are needed.
