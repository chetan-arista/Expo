# axe-core (vendored, unmodified)

- Package: axe-core 4.14.0 by Deque Systems, Inc. (https://github.com/dequelabs/axe-core)
- Licence: Mozilla Public License 2.0 (MPL-2.0), full text in `LICENSE`. Code bundled inside
  `axe.min.js` keeps its own licences, listed in `LICENSE-3RD-PARTY.txt` (MIT and others).
- Source: https://registry.npmjs.org/axe-core/-/axe-core-4.14.0.tgz
  (npm integrity `sha512-9WTZxEjsZ7b13TH8JPmbV2z8CHbl80/2hm3XPEG4JgNdQLK81IBRXmSxHfMAOkSqQeRxT/0dwNDz2GOm3zzpcQ==`); `axe.min.js` is the file as shipped (`sha256-IMCf4Veoo0ow4kGqofza3mV3NPCKs3ns++t9RcxG6Hg=`).
- Source code form (MPL-2.0 section 3.2): https://github.com/dequelabs/axe-core/tree/v4.14.0 and `axe.js` in the same npm package.
- `rules.json`: data read from `axe.min.js` with `axe.getRules()` (105 rules: ids, tags, impact,
  help text and help link), covered by the same licence. `level` and `run` are added by ARACHNIX:
  ARACHNIX runs 93 of them (deprecated and experimental rules and `frame-tested` are left out).
- Used by: `crawler/render_capture.py` injects `axe.min.js` into pages rendered in Chrome; the
  Accessibility tab (`crawler/seo/tabs/accessibility.py`) names its rule filters from `rules.json`.
- Update: change `AXE_VERSION`, `AXE_INTEGRITY` and `AXE_MIN_SHA256` in `crawler/vendor/axe_core.py`,
  then run `python -m crawler.vendor.fetch_axe --force`.
- Fetched: 2026-10-28 by `crawler/vendor/fetch_axe.py`.
