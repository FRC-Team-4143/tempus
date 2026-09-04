# static/wallet/

Artwork for the Apple Wallet / Google Wallet badge passes (see `app/services/wallet.py`).

| file | purpose | source |
|------|---------|--------|
| `icon.png`, `icon@2x.png`, `icon@3x.png` | Apple `.pkpass` icon (required) — 29 / 58 / 87 px | generated placeholder (kiosk red tile + clock) — replace with real team art at the same sizes |
| `logo.png`, `logo@2x.png` | Apple pass logo (top-left) **and** Google pass `logo.sourceUri` — 160×50 / 320×100 | generated placeholder ("TEMPUS" wordmark) — replace with real team art |
| `add-to-apple-wallet.svg` | "Add to Apple Wallet" button on `/badge/<id>` | Apple official badge, en-US/UK (Add to Apple Wallet Guidelines). Use per Apple's guidelines. |
| `add-to-google-wallet.svg` | "Add to Google Wallet" button on `/badge/<id>` | Google official badge, en-US (Google Wallet button guidelines). Use per Google's guidelines. |

The two `add-to-*.svg` files are the vendors' official, self-contained artwork — don't
recolor or distort them. The `icon*`/`logo*` PNGs are placeholders; swapping in real
team art needs no code change as long as the filenames and pixel sizes stay the same.
