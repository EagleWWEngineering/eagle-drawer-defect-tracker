/* label-scan.js - PROJECT_SPEC_PHASE9.md Part 3: "Scan label" on the New Defect
 * form. Two independent reads of the same physical label:
 *
 *   1. QR code -> the six-digit order number. Free, offline, exact. Decoded via
 *      the native BarcodeDetector where available, vendored jsQR otherwise (iOS
 *      Safari has no native BarcodeDetector). Works whether or not OCR is
 *      configured at all - see GET /api/v1/scan/config.
 *   2. Printed text -> the work order line + dimensions, via OCR. Default engine
 *      is Tesseract.js, vendored and running entirely in this browser tab -
 *      app/static/js/vendor/{tesseract.min.js,tesseract-worker.min.js,
 *      tesseract-core-lstm.wasm.js,eng.traineddata.gz}. Never a CDN, never a
 *      server round trip for recognition itself. The line label is read from
 *      a single, generous whole-label recognition pass (word-level output,
 *      no character whitelist - see HOTFIX 3 below); dimensions still come
 *      from their own small, targeted crop. Only the already-recognised TEXT
 *      is posted to POST /api/v1/scan/parse-label for parsing/validation
 *      (app/services/ocr_service.py - one implementation, tested in Python,
 *      never duplicated here). If OCR_PROVIDER is a cloud option instead
 *      (azure/google/anthropic), the captured photo is posted to
 *      POST /api/v1/scan/diagnose instead of running Tesseract locally.
 *
 * Manual entry always works, at every step - this module only ever fills form
 * fields via the callbacks the caller supplies; it never submits anything, and
 * every field it touches stays a normal, editable input afterward.
 *
 * HOTFIX 1 (2026-09-01) - "scan modal will not close, label is never read":
 * startScan() used to be a single `async function` returning its {stop}
 * controller only as a promise's resolved value, awaited nowhere by the
 * caller - fixed by returning a fully-working controller SYNCHRONOUSLY before
 * any fallible setup runs. See git history for the full writeup.
 *
 * HOTFIX 2 (2026-09-01) - "line label never fills, everything else reads fine":
 * on a real phone against a real label, the QR and dimension crop worked but
 * the line-label crop consistently came back empty. Root cause: the line-label
 * crop was recognised with `tessedit_pageseg_mode` SINGLE_LINE (7), a mode
 * that assumes a normal multi-word text line - Tesseract's segmentation step
 * frequently refuses to treat a single isolated 1-2 character glyph as a
 * recognisable "line" under that mode and returns empty rather than a wrong
 * guess. Also replaced blind QR-corner extrapolation for the crop geometry
 * with label-boundary detection seeded from the QR - extrapolating a crop
 * position across the whole label from the QR's corners alone magnifies any
 * small error in the QR's detected rotation/scale, worst exactly at the far
 * corner where the line label sits. See detectLabelBoundary/
 * buildNormalizedLabelCanvas below.
 *
 * HOTFIX 3 (2026-09-01) - "read the whole label, rank by position, drop the
 * corner crop": tested against real printed labels, HOTFIX 2's corner crop
 * for the line label STILL failed - confirmed across real labels that the
 * label's own thickness/corner-block text ("5/8 | 6") sits immediately beside
 * the line-label letter, on the same line. A crop off by even a few percent
 * captures that neighbouring digit/punctuation instead of the letter, and
 * under an A-Z whitelist a digit either gets forced into a wrong letter or
 * (per HOTFIX 2's PSM fix) correctly returns nothing. No crop of that corner
 * is narrow enough to exclude the neighbour and wide enough to reliably
 * contain the letter despite normal geometry error - so this drops corner
 * cropping for the line label ENTIRELY. The line label is now read from a
 * single, generous whole-label recognition pass (PSM_BLOCK_OF_TEXT, no
 * character whitelist - the label has letters, digits, fractions and
 * punctuation) producing WORD-level output, converted to the same
 * {text, x, y} shape the cloud providers already send and run through
 * app/services/ocr_service.py's EXISTING candidate/blocklist/distance-ranking
 * logic unchanged - ranking by relative distance from the QR survives a
 * sloppy bounding box (this generous region does not need to be precise),
 * whereas a tight crop does not; neighbouring text becomes just another
 * rejected candidate instead of a contaminant. The dimension crop is
 * unchanged (it was already reported working) - see
 * deriveDimensionRegionFromNormalizedLabel/deriveDimensionRegionFromQrExtrapolation.
 *
 * HOTFIX 4 (2026-09-01) - "select by corner proximity, then fall back to a
 * picker": across four real-label attempts, the true letter was NEVER among
 * the candidates HOTFIX 3 produced - the winners were real characters found
 * elsewhere on the label (fragments of other words), because "furthest from
 * the QR" is satisfied by anything at the far end of the label, not
 * specifically the corner. Three changes, all in
 * app/services/ocr_service.py (never here - see parse_line_label): (1)
 * candidates are now ranked by distance TO the expected corner (computed
 * below and sent as corner_x/corner_y/qr_size), nearest wins, not distance
 * FROM the QR; (2) a token OCR merged with neighbouring punctuation/digits
 * (e.g. "3.5B") now yields a candidate letter extracted from its own
 * leading/trailing edge, but ONLY when already near the expected corner;
 * (3) a distance cap and a confidence floor are both enforced - a candidate
 * beyond either is not a candidate, and if nothing qualifies the result is
 * an honest "couldn't read it" (line_label: null), never a distant guess.
 * This file's own job for this fix is narrow: compute the expected corner
 * (expectedCornerInNormalizedSpace/expectedCornerFromQrExtrapolation) and
 * run one MORE recognition pass - PSM_SPARSE_TEXT over a generously padded
 * region around that corner specifically (scattered-character segmentation
 * suits an isolated letter better than the whole-label block pass) - whose
 * words feed into the exact same `lines` list posted to the server, right
 * alongside the whole-label pass's words. When OCR still can't produce a
 * confident result, the New Defect form shows a manual letter-picker instead
 * of leaving the operator with an empty field and no path forward - see
 * app/templates/defect_entry.html.
 *
 * HOTFIX 5 (2026-09-01) - "the boundary walk stops short of the real edge on
 * a real label": given an actual photo of a real work-order label (not a
 * synthetic harness - the only way HOTFIX 4's premise was ever visible),
 * HOTFIX 2's boundary walk was measured stopping as little as ~1 qrSize out
 * from the QR on one side, versus the label's real edge over 4 qrSizes out -
 * a norm nowhere close to a printed label. Root cause: comparing a sample's
 * Euclidean distance from a sampled "label white" reference cannot
 * distinguish "crossed onto the wood background" from "the ray crossed a
 * black text line, a red QC stamp dot, or a yellow highlighter band" - every
 * one of those deviates from white just as much as wood does, and on a
 * label with print running most of the way to its own edge (confirmed on
 * the real label used to diagnose this), no length of "sustained" run fixes
 * that - extending the run only ever finds MORE printed content, never
 * distinguishes it from background. Fix: replaced the white-distance check
 * with isWoodBackgroundColor - wood has a warm color cast (red channel
 * clearly above blue) that stays close between red and green; none of black
 * ink (no warm cast), a red stamp (red enormously above green, not just
 * blue), or yellow highlighter (red BELOW green) can satisfy it. Verified by
 * porting this exact walk to a disposable Python script run directly against
 * the real photo's pixels (not a synthetic harness) with the QR's actual
 * detected corners: the previous check placed the expected corner over 1000px
 * from the label's true far corner (the line label sits there) - beyond
 * ocr_service.py's own distance cap, and short enough that the normalised
 * label canvas didn't contain that corner's pixels AT ALL, so no amount of
 * OCR tuning downstream could have recovered it. This fix placed it about
 * 110px away - comfortably inside both the sparse-corner crop and the
 * existing distance cap, with no change needed to ocr_service.py itself.
 * Still not verified against a live camera capture end-to-end, or against
 * any label but this one real photo - see the final report for this hotfix.
 */

(function () {
  "use strict";

  const VENDOR_BASE = "/static/js/vendor/";

  // Tesseract PSM (page segmentation mode) values used explicitly below - see
  // https://github.com/tesseract-ocr/tesseract/blob/main/include/tesseract/publictypes.h
  const PSM_SINGLE_LINE = "7"; // a line of text with normal word spacing - the dimension crop
  const PSM_BLOCK_OF_TEXT = "6"; // a single uniform block of text - the whole-label pass (HOTFIX 3)
  const PSM_SPARSE_TEXT = "11"; // scattered characters, no block layout assumed - the corner pass (HOTFIX 4)

  const OCR_UPSCALE = 3; // Tesseract reads small, tightly-cropped text far better upscaled - the dimension crop
  // The whole-label region is already a few hundred pixels across at typical
  // phone resolution - upscaling it the same 3x as the tiny dimension crop
  // would multiply a full recognition pass's runtime for no benefit
  // (PROJECT_SPEC_PHASE9.md hotfix 3, "Performance": downscale/adjust here
  // first if a real device turns out to need it, never reintroduce a corner
  // crop to claw back speed).
  const FULL_LABEL_UPSCALE = 1;
  const CROP_PADDING_FACTOR = 1.35; // "pad generously" - better an oversized crop with a constrained whitelist than a tight one that clips something

  // --- Label-boundary detection (see HOTFIX 2 above) ----------------------
  const NORMALIZED_LABEL_WIDTH = 900;
  const NORMALIZED_LABEL_HEIGHT = 560;
  // The dimension crop, as a fraction of the normalised label - anchored near
  // the QR's own corner (same general area as the order/qty/ship text) rather
  // than a fixed pixel offset, so distance/angle/rotation stop mattering once
  // the label is normalised. Unchanged by hotfix 3 - this crop already works
  // in the field; only the line label's OWN crop was removed (see HOTFIX 3).
  const DIMENSION_NORM_WIDTH_FRACTION = 0.62;
  const DIMENSION_NORM_HEIGHT_FRACTION = 0.28;
  const DIMENSION_NORM_INSET_FRACTION = 0.08; // skip past the QR's own footprint
  // The whole-label region used for the line-label pass, when label-boundary
  // detection fails and the QR-extrapolated fallback is used instead - a
  // generous margin (in multiples of qrSize) added around the span from the
  // QR's own position out to its mirrored (estimated far-corner) point. Does
  // not need to be precise - the position-ranking parser in ocr_service.py is
  // what makes a sloppy region safe, not a tight boundary.
  const FULL_LABEL_FALLBACK_MARGIN_QR_MULTIPLE = 1.5;

  // HOTFIX 4 - the sparse-text pass over the EXPECTED CORNER (where the line
  // label is expected diagonally opposite the QR): scattered-character
  // segmentation suits an isolated letter far better than the whole-label
  // block pass, and this crop is small/targeted enough to be worth
  // upscaling (see OCR_UPSCALE) the way the dimension crop already is. Sized
  // generously ("does not need to be precise") - ocr_service.py's distance
  // cap from the expected corner is what actually decides which recognised
  // token counts as a candidate, not how tightly this region is drawn.
  const SPARSE_CORNER_NORM_FRACTION = 0.48; // normalised-label-space case
  const SPARSE_CORNER_FALLBACK_QR_MULTIPLE = 3.0; // QR-extrapolated fallback case

  const BOUNDARY_WALK_STEP_PX = 4;
  const BOUNDARY_CONSECUTIVE_DEVIATIONS_TO_CONFIRM_EDGE = 5; // a run this long is "left the label", not a brushed-past line of text
  const BOUNDARY_MAX_WALK_QR_MULTIPLE = 10; // give up looking this many qrSizes out
  const BOUNDARY_MIN_PLAUSIBLE_QR_MULTIPLE = 0.4; // an edge closer than this is implausible - not a real boundary

  // HOTFIX 5 (2026-09-01) - "the boundary walk stops at a printed feature,
  // not the physical edge": the previous check (a sample's Euclidean RGB
  // distance from a sampled reference white exceeding a flat threshold) could
  // not tell "left the label onto the wood" apart from "the ray crossed a
  // black text line / red QC stamp dot / yellow highlighter band" - all of
  // those deviate from white too, and on a real photographed label (see the
  // module docstring) that made the walk stop as little as ~1 qrSize out,
  // sometimes over a thousand pixels short of that label's real edge, on a
  // long label whose printed content runs close to it. Every drawer part
  // this app scans a label on is wood (see CLAUDE.md) - wood has a color
  // signature under normal lighting that none of those three printed
  // elements share: a clearly WARM cast (red channel well above blue) where
  // red and green stay close together. Plain black ink is desaturated (all
  // three channels roughly equal - no warm cast at all). A red QC stamp's red
  // channel is enormously above its green channel, not just its blue. Yellow
  // highlighter's red channel sits BELOW its green channel - not warm by this
  // measure at all. None of the three can satisfy all of the checks below,
  // calibrated against real photos of Eagle's own maple/plywood stock - only
  // genuine wood background can. If a label ever sits on a non-wood surface
  // this fails to match, the walk simply doesn't find a confirmed edge in
  // that direction and returns null like it always could - callers already
  // fall back to the QR-extrapolated crops in that case (see
  // deriveDimensionRegionFromQrExtrapolation and friends below), so an
  // unfamiliar background degrades gracefully, it doesn't produce a
  // confidently wrong corner.
  const BOUNDARY_WOOD_RED_MINUS_BLUE_MIN = 12;
  const BOUNDARY_WOOD_RED_MINUS_GREEN_MIN = 2;
  const BOUNDARY_WOOD_RED_MINUS_GREEN_MAX = 35;

  function log(...args) {
    if (window.LABEL_SCAN_DEBUG) console.log("[label-scan]", ...args);
  }

  /** Every failure in this module goes through here at least once - console
   * output is UNCONDITIONAL (never gated behind a debug flag), so a real
   * problem is always diagnosable from DevTools even when nobody is watching
   * for it (fail-loud, matching the rest of this app). */
  function logError(context, err) {
    console.error("[label-scan]", context, err);
  }

  /** Calls a caller-supplied callback defensively - a callback that itself
   * throws (e.g. a missing DOM element on the caller's side) must never take
   * down this module's own control flow (teardown in particular - see stop()
   * below). */
  function safeCall(fn, ...args) {
    if (!fn) return;
    try {
      fn(...args);
    } catch (err) {
      logError("a scan callback threw", err);
    }
  }

  function isDebugEnabled() {
    try {
      return new URLSearchParams(window.location.search).get("scandebug") === "1";
    } catch (err) {
      return false;
    }
  }

  // -------------------------------------------------------------------------
  // Tesseract.js worker
  // -------------------------------------------------------------------------
  //
  // Deliberately NOT a page-level singleton (it used to be, to amortise the
  // ~6MB first-load cost across repeated scans) - every scan session's worker
  // is fully terminated on close, with no shared state that could outlive one
  // "Scan label" attempt. The underlying vendored files are still served from
  // this app's own static handler and cached by the browser's normal HTTP
  // cache after the first load, so a fresh worker on the next scan is still
  // fast in practice.

  async function createTesseractWorker(onPreparing) {
    safeCall(onPreparing);
    return window.Tesseract.createWorker("eng", 1, {
      workerPath: VENDOR_BASE + "tesseract-worker.min.js",
      corePath: VENDOR_BASE + "tesseract-core-lstm.wasm.js",
      langPath: VENDOR_BASE,
      logger: (m) => log("tesseract", m.status, m.progress),
    });
  }

  /** Sequential, not Promise.all'd: tessedit_char_whitelist AND
   * tessedit_pageseg_mode are WORKER-wide parameters (setParameters), not
   * per-call options, so recognising the two crops "concurrently" on one
   * worker would race two different settings against each other and could
   * silently corrupt either result - the exact same class of bug for PSM as
   * the whitelist race already fixed in hotfix 1. `pageSegMode` is REQUIRED
   * (no default) specifically so a caller can never forget to set it per-crop
   * and accidentally inherit whatever the previous crop used. */
  async function recognizeCrop(worker, canvas, whitelist, pageSegMode) {
    await worker.setParameters({
      tessedit_char_whitelist: whitelist,
      tessedit_pageseg_mode: pageSegMode,
    });
    const { data } = await worker.recognize(canvas);
    return data;
  }

  // -------------------------------------------------------------------------
  // QR decoding
  // -------------------------------------------------------------------------

  //: The literal double backslash in `.../WorkOrderPDFs/\\178414.pdf` is
  //: malformed at source (PROJECT_SPEC_PHASE9.md) - tolerate one or more
  //: slashes/backslashes before the six-digit order number.
  const QR_ORDER_NUMBER_RE = /[\\/]+(\d{6})\.pdf/i;

  function extractOrderNumberFromQrText(text) {
    const match = QR_ORDER_NUMBER_RE.exec(text || "");
    return match ? match[1] : null;
  }

  /** Normalises both native BarcodeDetector and jsQR results to
   * { text, corners: [{x,y} x4] } (corners in video-pixel coordinates,
   * order: topLeft, topRight, bottomRight, bottomLeft), or null. */
  async function detectQrOnce(video, canvas, ctx, state) {
    if (window.BarcodeDetector && !state.barcodeDetectorUnsupported) {
      try {
        if (!state.barcodeDetector) {
          state.barcodeDetector = new window.BarcodeDetector({ formats: ["qr_code"] });
        }
        const results = await state.barcodeDetector.detect(video);
        if (results && results.length) {
          const r = results[0];
          return { text: r.rawValue, corners: r.cornerPoints };
        }
        return null;
      } catch (err) {
        // A genuine "not supported on this platform" failure (e.g. some
        // desktop Chrome builds advertise BarcodeDetector without a working
        // backend) would otherwise retry - and fail - on every single frame.
        // Fall back to jsQR for the rest of this session instead.
        state.barcodeDetectorUnsupported = true;
        log("BarcodeDetector unavailable, falling back to jsQR for this session", err);
      }
    }
    if (window.jsQR) {
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      const imageData = ctx.getImageData(0, 0, canvas.width, canvas.height);
      const result = window.jsQR(imageData.data, imageData.width, imageData.height);
      if (result) {
        const loc = result.location;
        return {
          text: result.data,
          corners: [
            loc.topLeftCorner,
            loc.topRightCorner,
            loc.bottomRightCorner,
            loc.bottomLeftCorner,
          ],
        };
      }
    }
    return null;
  }

  function qrBoundingBox(corners) {
    const xs = corners.map((c) => c.x);
    const ys = corners.map((c) => c.y);
    const minX = Math.min(...xs);
    const maxX = Math.max(...xs);
    const minY = Math.min(...ys);
    const maxY = Math.max(...ys);
    return {
      x: minX,
      y: minY,
      size: Math.max(maxX - minX, maxY - minY),
      centerX: (minX + maxX) / 2,
      centerY: (minY + maxY) / 2,
    };
  }

  // -------------------------------------------------------------------------
  // Pure vector/affine math (no DOM/canvas dependency - exported on
  // window.LabelScan._internal for the disposable Node repro harnesses used
  // to validate this during development; not part of the public API)
  // -------------------------------------------------------------------------

  function vecSub(a, b) {
    return { x: a.x - b.x, y: a.y - b.y };
  }
  function vecAdd(a, b) {
    return { x: a.x + b.x, y: a.y + b.y };
  }
  function vecScale(a, s) {
    return { x: a.x * s, y: a.y * s };
  }
  function vecLen(a) {
    return Math.sqrt(a.x * a.x + a.y * a.y);
  }
  function vecNormalize(a) {
    const len = vecLen(a) || 1;
    return { x: a.x / len, y: a.y / len };
  }

  /** The QR's own local coordinate frame: origin (center), axisU (its
   * "rightward" direction), axisV (its "downward" direction), and qrSize
   * (average edge length) - derived from the four corner points
   * (topLeft, topRight, bottomRight, bottomLeft) rather than an axis-aligned
   * bounding box, so it's correct even when the QR is rotated in frame. */
  function qrAxesFromCorners(corners) {
    const [topLeft, topRight, bottomRight, bottomLeft] = corners;
    const origin = {
      x: (topLeft.x + topRight.x + bottomRight.x + bottomLeft.x) / 4,
      y: (topLeft.y + topRight.y + bottomRight.y + bottomLeft.y) / 4,
    };
    const rightTop = vecSub(topRight, topLeft);
    const rightBottom = vecSub(bottomRight, bottomLeft);
    const axisU = vecNormalize(vecAdd(rightTop, rightBottom));
    const downLeft = vecSub(bottomLeft, topLeft);
    const downRight = vecSub(bottomRight, topRight);
    const axisV = vecNormalize(vecAdd(downLeft, downRight));
    const qrSize = (vecLen(rightTop) + vecLen(rightBottom) + vecLen(downLeft) + vecLen(downRight)) / 4;
    return { origin, axisU, axisV, qrSize };
  }

  /** The unique affine transform mapping src0/src1/src2 to dst0/dst1/dst2
   * respectively (3 point correspondences fully determine a 2D affine map).
   * Returns {a,b,c,d,e,f} in the canvas setTransform convention:
   * dst.x = a*src.x + c*src.y + e ; dst.y = b*src.x + d*src.y + f. */
  function solveAffine(src0, src1, src2, dst0, dst1, dst2) {
    const dx1 = src1.x - src0.x;
    const dy1 = src1.y - src0.y;
    const dx2 = src2.x - src0.x;
    const dy2 = src2.y - src0.y;
    const det = dx1 * dy2 - dx2 * dy1;
    if (Math.abs(det) < 1e-9) return null; // degenerate (collinear) correspondences

    const dstx1 = dst1.x - dst0.x;
    const dstx2 = dst2.x - dst0.x;
    const dsty1 = dst1.y - dst0.y;
    const dsty2 = dst2.y - dst0.y;

    const a = (dstx1 * dy2 - dstx2 * dy1) / det;
    const c = (dstx2 * dx1 - dstx1 * dx2) / det;
    const e = dst0.x - a * src0.x - c * src0.y;

    const b = (dsty1 * dy2 - dsty2 * dy1) / det;
    const d = (dsty2 * dx1 - dsty1 * dx2) / det;
    const f = dst0.y - b * src0.x - d * src0.y;

    return { a, b, c, d, e, f };
  }

  function invertAffine(m) {
    const det = m.a * m.d - m.c * m.b;
    if (Math.abs(det) < 1e-9) return null;
    const a = m.d / det;
    const b = -m.b / det;
    const c = -m.c / det;
    const d = m.a / det;
    const e = -(a * m.e + c * m.f);
    const f = -(b * m.e + d * m.f);
    return { a, b, c, d, e, f };
  }

  function applyAffine(m, p) {
    return { x: m.a * p.x + m.c * p.y + m.e, y: m.b * p.x + m.d * p.y + m.f };
  }

  // -------------------------------------------------------------------------
  // Label-boundary detection (see HOTFIX 2 at the top of this file)
  // -------------------------------------------------------------------------

  function getPixel(imageData, x, y) {
    const xi = Math.max(0, Math.min(imageData.width - 1, Math.round(x)));
    const yi = Math.max(0, Math.min(imageData.height - 1, Math.round(y)));
    const i = (yi * imageData.width + xi) * 4;
    const d = imageData.data;
    return { r: d[i], g: d[i + 1], b: d[i + 2] };
  }

  /** HOTFIX 5 - see the constants above: true only for a color with wood's
   * warm-but-not-saturated signature, which black ink, a red QC stamp, and
   * yellow highlighter all fail. */
  function isWoodBackgroundColor(color) {
    const redMinusBlue = color.r - color.b;
    const redMinusGreen = color.r - color.g;
    return (
      redMinusBlue > BOUNDARY_WOOD_RED_MINUS_BLUE_MIN &&
      redMinusGreen > BOUNDARY_WOOD_RED_MINUS_GREEN_MIN &&
      redMinusGreen < BOUNDARY_WOOD_RED_MINUS_GREEN_MAX
    );
  }

  /** Walks outward from `start` along `direction` (a unit vector) in
   * BOUNDARY_WALK_STEP_PX increments, looking for a SUSTAINED run of
   * BOUNDARY_CONSECUTIVE_DEVIATIONS_TO_CONFIRM_EDGE samples that all match
   * isWoodBackgroundColor. Requiring a sustained run (not just one matching
   * sample) is defense in depth against a single mis-colored pixel (JPEG
   * noise, antialiasing at a stroke edge); HOTFIX 5 is what stops printed
   * label content from matching in the first place, so this run no longer
   * needs to be long enough to out-wait a whole printed word the way the
   * previous white-distance version did. Returns the distance (from `start`)
   * where the run began, or null if no such run was found within
   * `maxDistance`. */
  function walkToEdge(imageData, start, direction, maxDistance) {
    let consecutiveDeviations = 0;
    for (let dist = 0; dist <= maxDistance; dist += BOUNDARY_WALK_STEP_PX) {
      const p = vecAdd(start, vecScale(direction, dist));
      const color = getPixel(imageData, p.x, p.y);
      if (isWoodBackgroundColor(color)) {
        consecutiveDeviations++;
        if (consecutiveDeviations >= BOUNDARY_CONSECUTIVE_DEVIATIONS_TO_CONFIRM_EDGE) {
          return dist - (consecutiveDeviations - 1) * BOUNDARY_WALK_STEP_PX;
        }
      } else {
        consecutiveDeviations = 0;
      }
    }
    return null;
  }

  /** Finds the label's extent in all 4 directions from the QR (in the QR's
   * own rotated axes), seeded from the QR rather than searching the whole
   * frame - the QR already gives exact position/scale/rotation. Returns
   * {distNegU, distPosU, distNegV, distPosV} (all in pixels, measured from
   * the QR's own center) on success, or null if the label boundary couldn't
   * be confidently found in every direction (glare, low contrast, a
   * non-wood surface, the label partly out of frame, ...) - callers must
   * fall back to the QR-extrapolated crop rectangles when this returns null,
   * never fail outright. */
  function detectLabelBoundary(imageData, axes) {
    const startOffset = axes.qrSize / 2 + BOUNDARY_WALK_STEP_PX; // just outside the QR's own bounding box
    const maxDistance = axes.qrSize * BOUNDARY_MAX_WALK_QR_MULTIPLE;
    const minPlausible = axes.qrSize * BOUNDARY_MIN_PLAUSIBLE_QR_MULTIPLE;

    const directions = [
      { key: "distPosU", dir: axes.axisU },
      { key: "distNegU", dir: vecScale(axes.axisU, -1) },
      { key: "distPosV", dir: axes.axisV },
      { key: "distNegV", dir: vecScale(axes.axisV, -1) },
    ];

    const result = {};
    for (const { key, dir } of directions) {
      const start = vecAdd(axes.origin, vecScale(dir, startOffset));
      const found = walkToEdge(imageData, start, dir, maxDistance - startOffset);
      if (found === null) return null; // frame edge reached / label bigger than frame - can't trust this
      const totalFromCenter = startOffset + found;
      if (totalFromCenter < minPlausible) return null; // implausibly close - not a real edge
      result[key] = totalFromCenter;
    }
    return result;
  }

  /** Builds a normalised, axis-aligned canvas of the whole label from the
   * detected boundary, using an affine warp (rotation + independent per-axis
   * scale - not a full 4-point perspective, which canvas 2D can't do
   * natively; a roughly front-on phone shot is a reasonable enough
   * approximation, and the crops taken from it are padded generously anyway).
   * Returns { canvas, forwardMatrix, qrNormX, qrNormY } - forwardMatrix maps
   * ORIGINAL FRAME coordinates to NORMALISED coordinates; invertAffine(it)
   * maps back, which is how a recognised line's position gets back into the
   * same coordinate space qr_x/qr_y is already in (see runTesseractPath). */
  function buildNormalizedLabelCanvas(sourceCanvas, axes, boundary) {
    const p00 = vecAdd(
      axes.origin,
      vecAdd(vecScale(axes.axisU, -boundary.distNegU), vecScale(axes.axisV, -boundary.distNegV))
    );
    const p10 = vecAdd(
      axes.origin,
      vecAdd(vecScale(axes.axisU, boundary.distPosU), vecScale(axes.axisV, -boundary.distNegV))
    );
    const p01 = vecAdd(
      axes.origin,
      vecAdd(vecScale(axes.axisU, -boundary.distNegU), vecScale(axes.axisV, boundary.distPosV))
    );
    const forwardMatrix = solveAffine(
      p00,
      p10,
      p01,
      { x: 0, y: 0 },
      { x: NORMALIZED_LABEL_WIDTH, y: 0 },
      { x: 0, y: NORMALIZED_LABEL_HEIGHT }
    );
    if (!forwardMatrix) return null;

    const out = document.createElement("canvas");
    out.width = NORMALIZED_LABEL_WIDTH;
    out.height = NORMALIZED_LABEL_HEIGHT;
    const ctx = out.getContext("2d");
    ctx.setTransform(forwardMatrix.a, forwardMatrix.b, forwardMatrix.c, forwardMatrix.d, forwardMatrix.e, forwardMatrix.f);
    ctx.drawImage(sourceCanvas, 0, 0);
    ctx.setTransform(1, 0, 0, 0, 1, 0); // not reached on failure paths, but reset defensively

    const totalU = boundary.distNegU + boundary.distPosU;
    const totalV = boundary.distNegV + boundary.distPosV;
    return {
      canvas: out,
      forwardMatrix,
      qrNormX: (boundary.distNegU / totalU) * NORMALIZED_LABEL_WIDTH,
      qrNormY: (boundary.distNegV / totalV) * NORMALIZED_LABEL_HEIGHT,
    };
  }

  // -------------------------------------------------------------------------
  // Crop derivation - two strategies. Both return
  // { rect: {x,y,width,height}, toOriginalFrame(point) } - `rect` is in
  // whichever canvas is actually cropped (the normalised label, or the raw
  // captured frame for the fallback), and toOriginalFrame() maps a point in
  // that same space back into the ORIGINAL CAPTURED FRAME's coordinates, so
  // recognised-line positions always end up comparable to qr_x/qr_y
  // regardless of which strategy was used.
  // -------------------------------------------------------------------------

  function clampRect(rect, frameWidth, frameHeight) {
    const x = Math.max(0, Math.min(rect.x, frameWidth - 1));
    const y = Math.max(0, Math.min(rect.y, frameHeight - 1));
    const width = Math.max(1, Math.min(rect.width, frameWidth - x));
    const height = Math.max(1, Math.min(rect.height, frameHeight - y));
    return { x, y, width, height };
  }

  const IDENTITY_MATRIX = { a: 1, b: 0, c: 0, d: 1, e: 0, f: 0 };
  const identityToOriginalFrame = (p) => applyAffine(IDENTITY_MATRIX, p);

  /** The dimension crop, as a fraction of the normalised label - anchored
   * near the QR's own corner (see the NORM_FRACTION constants above).
   * Unchanged by hotfix 3 - this crop was already reported working in the
   * field; only the line label's own crop was removed (see HOTFIX 3). */
  function deriveDimensionRegionFromNormalizedLabel(normalized) {
    const W = NORMALIZED_LABEL_WIDTH;
    const H = NORMALIZED_LABEL_HEIGHT;
    const { qrNormX, qrNormY, forwardMatrix } = normalized;
    const inverse = invertAffine(forwardMatrix);

    const farX = qrNormX < W / 2 ? W : 0;
    const nearX = W - farX;
    const dimensionWidth = W * DIMENSION_NORM_WIDTH_FRACTION;
    const dimensionHeight = H * DIMENSION_NORM_HEIGHT_FRACTION;
    const inset = W * DIMENSION_NORM_INSET_FRACTION;
    const rect = clampRect(
      {
        x: nearX === 0 ? inset : nearX - inset - dimensionWidth,
        y: qrNormY - dimensionHeight / 2,
        width: dimensionWidth,
        height: dimensionHeight,
      },
      W,
      H
    );
    return { rect, toOriginalFrame: (p) => applyAffine(inverse, p), sourceCanvas: normalized.canvas };
  }

  /** The dimension crop's fallback when label-boundary detection fails -
   * extrapolate directly from the QR's corners against the raw captured
   * frame. Unchanged by hotfix 3 (see above). */
  function deriveDimensionRegionFromQrExtrapolation(sourceCanvas, qrBox, frameWidth, frameHeight) {
    const rect = clampRect(
      {
        x: qrBox.x + qrBox.size * 1.1,
        y: qrBox.centerY - qrBox.size / 2,
        width: qrBox.size * 3.2,
        height: qrBox.size,
      },
      frameWidth,
      frameHeight
    );
    return { rect, toOriginalFrame: identityToOriginalFrame, sourceCanvas };
  }

  /** HOTFIX 3 - "read the whole label": the region recognised for the line
   * label is now the ENTIRE normalised label (no crop of just one corner),
   * word-level, no whitelist - see the module docstring. When boundary
   * detection succeeded, that IS the normalised label canvas in full. */
  function deriveFullLabelRegionFromNormalizedLabel(normalized) {
    const inverse = invertAffine(normalized.forwardMatrix);
    return {
      rect: { x: 0, y: 0, width: NORMALIZED_LABEL_WIDTH, height: NORMALIZED_LABEL_HEIGHT },
      toOriginalFrame: (p) => applyAffine(inverse, p),
      sourceCanvas: normalized.canvas,
    };
  }

  /** HOTFIX 3 fallback: when label-boundary detection fails, a generous
   * (deliberately imprecise - "does not need to be precise, err large") box
   * spanning from the QR's own position out to its mirrored (estimated far
   * corner) point, plus a flat margin - derived from the QR exactly the way
   * the original Phase 9 crop was, just far larger and covering the whole
   * label rather than one corner. */
  function deriveFullLabelRegionFromQrExtrapolation(sourceCanvas, qrBox, frameWidth, frameHeight) {
    const mirroredX = frameWidth - qrBox.centerX;
    const mirroredY = frameHeight - qrBox.centerY;
    const margin = qrBox.size * FULL_LABEL_FALLBACK_MARGIN_QR_MULTIPLE;
    const x0 = Math.min(qrBox.centerX, mirroredX) - margin;
    const y0 = Math.min(qrBox.centerY, mirroredY) - margin;
    const x1 = Math.max(qrBox.centerX, mirroredX) + margin;
    const y1 = Math.max(qrBox.centerY, mirroredY) + margin;
    const rect = clampRect({ x: x0, y: y0, width: x1 - x0, height: y1 - y0 }, frameWidth, frameHeight);
    return { rect, toOriginalFrame: identityToOriginalFrame, sourceCanvas };
  }

  /** HOTFIX 4 - "select by corner proximity": the corner diagonally opposite
   * the QR, in NORMALISED space - a known constant fraction of the label
   * (one of its 4 literal corners), not an estimate. Reuses the exact same
   * "which side is the QR nearer" comparison already used to place the
   * dimension crop, so this is guaranteed to agree with it. */
  function expectedCornerInNormalizedSpace(normalized) {
    const W = NORMALIZED_LABEL_WIDTH;
    const H = NORMALIZED_LABEL_HEIGHT;
    return {
      x: normalized.qrNormX < W / 2 ? W : 0,
      y: normalized.qrNormY < H / 2 ? H : 0,
    };
  }

  /** HOTFIX 4 fallback: when label-boundary detection fails, the same
   * QR-position-mirrored-across-the-frame estimate the original Phase 9 crop
   * used - a real point, in ORIGINAL FRAME coordinates. */
  function expectedCornerFromQrExtrapolation(qrBox, frameWidth, frameHeight) {
    return { x: frameWidth - qrBox.centerX, y: frameHeight - qrBox.centerY };
  }

  /** HOTFIX 4 - the sparse-text pass's region: anchored AT the expected
   * corner (a true normalised-space vertex), extending inward - same
   * anchoring style as the pre-hotfix-3 line-label crop, just generously
   * sized and no longer whitelisted/depended upon alone. */
  function deriveSparseCornerRegionFromNormalizedLabel(normalized) {
    const W = NORMALIZED_LABEL_WIDTH;
    const H = NORMALIZED_LABEL_HEIGHT;
    const corner = expectedCornerInNormalizedSpace(normalized);
    const w = W * SPARSE_CORNER_NORM_FRACTION;
    const h = H * SPARSE_CORNER_NORM_FRACTION;
    const rect = clampRect(
      { x: corner.x === 0 ? 0 : corner.x - w, y: corner.y === 0 ? 0 : corner.y - h, width: w, height: h },
      W,
      H
    );
    const inverse = invertAffine(normalized.forwardMatrix);
    return { rect, toOriginalFrame: (p) => applyAffine(inverse, p), sourceCanvas: normalized.canvas, corner };
  }

  /** HOTFIX 4 fallback: centered on the mirrored expected-corner estimate
   * (unlike the normalised case, this isn't a true vertex, so centering
   * rather than anchoring-and-extending-inward is the safer bet against the
   * estimate being off in either direction). */
  function deriveSparseCornerRegionFromQrExtrapolation(sourceCanvas, qrBox, frameWidth, frameHeight) {
    const corner = expectedCornerFromQrExtrapolation(qrBox, frameWidth, frameHeight);
    const size = qrBox.size * SPARSE_CORNER_FALLBACK_QR_MULTIPLE;
    const rect = clampRect(
      { x: corner.x - size / 2, y: corner.y - size / 2, width: size, height: size },
      frameWidth,
      frameHeight
    );
    return { rect, toOriginalFrame: identityToOriginalFrame, sourceCanvas, corner };
  }

  /** Pads a crop rect generously (PROJECT_SPEC_PHASE9.md hotfix 2 - "the
   * letter sits at the extreme edge of the label, a crop sized exactly to the
   * expected position clips it when the photo is slightly off") and clamps it
   * back into the source canvas's own bounds. */
  function padRect(rect, sourceWidth, sourceHeight) {
    const extraW = rect.width * (CROP_PADDING_FACTOR - 1);
    const extraH = rect.height * (CROP_PADDING_FACTOR - 1);
    return clampRect(
      {
        x: rect.x - extraW / 2,
        y: rect.y - extraH / 2,
        width: rect.width + extraW,
        height: rect.height + extraH,
      },
      sourceWidth,
      sourceHeight
    );
  }

  function cropToCanvas(sourceCanvas, rect, scale) {
    const out = document.createElement("canvas");
    out.width = Math.max(1, Math.round(rect.width * scale));
    out.height = Math.max(1, Math.round(rect.height * scale));
    const ctx = out.getContext("2d");
    ctx.imageSmoothingEnabled = true;
    ctx.drawImage(sourceCanvas, rect.x, rect.y, rect.width, rect.height, 0, 0, out.width, out.height);
    return out;
  }

  /** Tesseract line bboxes come back in the CROP's own upscaled pixel space -
   * translate each line's center back into the crop-source canvas's
   * coordinates, then through toOriginalFrame() into the ORIGINAL captured
   * frame's coordinate space, so every line ranks against qr_x/qr_y exactly
   * like a cloud provider's lines already do (app/services/ocr_service.py
   * parse_line_label), regardless of which crop strategy produced it. Used
   * for the dimension crop (line-level Tesseract output). */
  function linesFromTesseractResult(data, rect, toOriginalFrame, scale) {
    const lines = (data && data.lines) || [];
    return lines
      .map((line) => {
        const text = (line.text || "").trim();
        if (!text) return null;
        const bbox = line.bbox || { x0: 0, x1: 0, y0: 0, y1: 0 };
        const localX = rect.x + (bbox.x0 + bbox.x1) / 2 / scale;
        const localY = rect.y + (bbox.y0 + bbox.y1) / 2 / scale;
        const framePoint = toOriginalFrame({ x: localX, y: localY });
        return { text, x: framePoint.x, y: framePoint.y, confidence: line.confidence };
      })
      .filter(Boolean);
  }

  /** HOTFIX 3 - "read the whole label": same idea as linesFromTesseractResult
   * above, but over Tesseract's WORD-level output (data.words) from the
   * whole-label pass - the line label is now just one word among many in that
   * result, told apart from the rest by app/services/ocr_service.py's
   * existing candidate/blocklist/distance-ranking logic once these are posted
   * to /api/v1/scan/parse-label, exactly like a cloud provider's lines
   * already are - no separate implementation. */
  function wordsFromTesseractResult(data, rect, toOriginalFrame, scale) {
    const words = (data && data.words) || [];
    return words
      .map((word) => {
        const text = (word.text || "").trim();
        if (!text) return null;
        const bbox = word.bbox || { x0: 0, x1: 0, y0: 0, y1: 0 };
        const localX = rect.x + (bbox.x0 + bbox.x1) / 2 / scale;
        const localY = rect.y + (bbox.y0 + bbox.y1) / 2 / scale;
        const framePoint = toOriginalFrame({ x: localX, y: localY });
        return { text, x: framePoint.x, y: framePoint.y, confidence: word.confidence };
      })
      .filter(Boolean);
  }

  // -------------------------------------------------------------------------
  // Public API
  // -------------------------------------------------------------------------

  /** Opens the camera against the given <video>/<canvas> elements and scans
   * until a QR code is found (or the caller calls the returned controller's
   * stop()). Callbacks:
   *   onOrderNumber(orderNumber) - QR decoded; fired once per startScan() call.
   *   onPreparingScanner() - Tesseract is about to initialise (this can take a
   *     moment on a slow connection/device) - show a "preparing scanner"
   *     indicator so the delay doesn't look like a hang.
   *   onLineLabelResult(result) - the parsed/validated result from either
   *     /parse-label or /diagnose (see app/schemas.py ScanDiagnosticOut) - may
   *     have line_label: null (nothing readable, or line_label_discarded).
   *   onOcrUnavailable(message) - OCR disabled or errored; line stays manual.
   *   onError(message) - camera/QR-level failure; manual entry is the only path.
   *   onScanComplete() - the QR has decoded AND the OCR attempt has resolved,
   *     one way or another (success, no-key-read, or error) - fired exactly
   *     once, the signal the caller uses to auto-close the scanner
   *     (PROJECT_SPEC_PHASE9.md hotfix 2, Step 5). Never fired if the operator
   *     closes the modal before a QR is ever found.
   *   onDebugFrame(info) - only meaningful with `?scandebug=1` (see
   *     isDebugEnabled) - a snapshot of exactly what this scan attempt
   *     recognised: the whole-label region and its recognised words (text +
   *     position), the dimension crop and its raw text/confidence, the
   *     detected label boundary (or that the QR-extrapolated fallback was
   *     used instead), the normalised label canvas if one was built, and the
   *     server's parsed/validated result (including the ranked line-label
   *     candidate list - see ocr_service.py parse_line_label). Fired once per
   *     OCR attempt whether it succeeded or not.
   *
   * Returns a controller ({ stop() }) SYNCHRONOUSLY and immediately - stop()
   * is fully functional the instant this returns, before camera permission has
   * even been requested, let alone granted. Closing must never depend on
   * anything else in this module succeeding.
   */
  function startScan(video, canvas, callbacks) {
    const cb = callbacks || {};
    const state = {
      stopped: false,
      stream: null,
      rafHandle: null,
      tesseractWorker: null,
      ocrFired: false,
      barcodeDetector: null,
      barcodeDetectorUnsupported: false,
    };

    // --- Teardown: built before any fallible setup runs, resilient to every
    // step below never having happened at all. Each piece is independent -
    // one failing/missing piece must never block the others. ---
    function stop() {
      if (state.stopped) return;
      state.stopped = true;

      if (state.rafHandle !== null) {
        try {
          cancelAnimationFrame(state.rafHandle);
        } catch (err) {
          logError("cancelAnimationFrame failed", err);
        }
        state.rafHandle = null;
      }
      if (state.stream) {
        try {
          state.stream.getTracks().forEach((track) => track.stop());
        } catch (err) {
          logError("stopping camera tracks failed", err);
        }
        state.stream = null;
      }
      try {
        video.srcObject = null;
      } catch (err) {
        logError("clearing video.srcObject failed", err);
      }
      if (state.tesseractWorker) {
        try {
          state.tesseractWorker.terminate();
        } catch (err) {
          logError("terminating the Tesseract worker failed", err);
        }
        state.tesseractWorker = null;
      }
    }

    const controller = { stop };

    // ------------------------------------------------------------------
    // Everything below is fallible async setup, run in the background.
    // `controller` above is already fully usable before any of it runs.
    // ------------------------------------------------------------------
    (async () => {
      try {
        state.stream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: "environment" },
          audio: false,
        });
      } catch (err) {
        logError("getUserMedia failed", err);
        safeCall(
          cb.onError,
          "Could not access the camera (" + err.message + "). Enter the order number and line manually."
        );
        return;
      }
      if (state.stopped) {
        // The operator closed the modal while the permission prompt was still
        // pending - stop() ran before `state.stream` existed to stop it.
        state.stream.getTracks().forEach((track) => track.stop());
        state.stream = null;
        return;
      }

      video.srcObject = state.stream;
      try {
        await video.play();
      } catch (err) {
        // A real, fairly common browser condition (e.g. an AbortError when
        // play() is interrupted) - this must degrade to manual entry, not
        // silently end the session with the modal stuck open.
        logError("video.play() failed", err);
        safeCall(
          cb.onError,
          "Could not start the camera preview (" + err.message + "). Enter the details manually."
        );
        return;
      }
      if (state.stopped) return;

      const ctx = canvas.getContext("2d", { willReadFrequently: true });

      async function handleQrFound(qr) {
        if (state.ocrFired) return;
        state.ocrFired = true;

        try {
          const orderNumber = extractOrderNumberFromQrText(qr.text);
          if (orderNumber) {
            safeCall(cb.onOrderNumber, orderNumber);
          } else {
            safeCall(
              cb.onError,
              "QR code didn't look like a work order label. Enter the order number manually."
            );
          }

          let scanConfig;
          try {
            scanConfig = await window.Api.getScanConfig();
          } catch (err) {
            logError("GET /api/v1/scan/config failed", err);
            safeCall(
              cb.onOcrUnavailable,
              "Could not reach the server to check OCR settings. Enter the line manually."
            );
            return;
          }
          if (state.stopped) return;
          if (!scanConfig.enabled) {
            safeCall(cb.onOcrUnavailable, "OCR is turned off on this server. Enter the line manually.");
            return;
          }

          const qrBox = qrBoundingBox(qr.corners);
          try {
            if (scanConfig.provider === "tesseract") {
              await runTesseractPath(qrBox, qr.corners, orderNumber);
            } else {
              await runCloudProviderPath(qrBox, orderNumber);
            }
          } catch (err) {
            logError("OCR path failed", err);
            safeCall(
              cb.onOcrUnavailable,
              "Couldn't read the line label automatically (" + err.message + "). Enter it manually."
            );
          }
        } finally {
          // Fires exactly once, on every exit path above (success, no OCR
          // configured, or an error) - see onScanComplete's docstring.
          // (PROJECT_SPEC_PHASE9.md hotfix 2, Step 5.)
          safeCall(cb.onScanComplete);
        }
      }

      async function runTesseractPath(qrBox, qrCorners, orderNumber) {
        canvas.width = video.videoWidth;
        canvas.height = video.videoHeight;
        ctx.drawImage(video, 0, 0, canvas.width, canvas.height);

        const axes = qrAxesFromCorners(qrCorners);
        let boundary = null;
        let normalized = null;
        try {
          boundary = detectLabelBoundary(ctx.getImageData(0, 0, canvas.width, canvas.height), axes);
          if (boundary) normalized = buildNormalizedLabelCanvas(canvas, axes, boundary);
        } catch (err) {
          log("label-boundary detection failed, falling back to QR extrapolation", err);
        }

        const method = normalized ? "label-boundary" : "qr-extrapolated";

        // Dimension crop: UNCHANGED strategy (already reported working) - a
        // small, targeted crop near the QR's own corner.
        const dimensionRegion = normalized
          ? deriveDimensionRegionFromNormalizedLabel(normalized)
          : deriveDimensionRegionFromQrExtrapolation(canvas, qrBox, canvas.width, canvas.height);
        const dimensionRect = padRect(
          dimensionRegion.rect,
          dimensionRegion.sourceCanvas.width,
          dimensionRegion.sourceCanvas.height
        );
        const dimensionCrop = cropToCanvas(dimensionRegion.sourceCanvas, dimensionRect, OCR_UPSCALE);

        // Line label: HOTFIX 3 - no longer its own tight corner crop. A
        // single, generous whole-label region, recognised in one pass and
        // ranked by distance from the QR using the SAME logic a line-level
        // cloud-provider read already goes through server-side (see the
        // module docstring).
        const fullLabelRegion = normalized
          ? deriveFullLabelRegionFromNormalizedLabel(normalized)
          : deriveFullLabelRegionFromQrExtrapolation(canvas, qrBox, canvas.width, canvas.height);
        const fullLabelCrop = cropToCanvas(fullLabelRegion.sourceCanvas, fullLabelRegion.rect, FULL_LABEL_UPSCALE);

        // HOTFIX 4 - "add a sparse-text recognition pass for the corner": a
        // second, smaller/upscaled/differently-segmented pass over the
        // EXPECTED corner specifically, to recover a letter the whole-label
        // block pass merged into a neighbouring token. Its results are just
        // MORE candidate entries in the same `lines` list sent to the
        // server - ocr_service.py dedupes/ranks/filters everything in one
        // place, this never decides anything on its own.
        const sparseCornerRegion = normalized
          ? deriveSparseCornerRegionFromNormalizedLabel(normalized)
          : deriveSparseCornerRegionFromQrExtrapolation(canvas, qrBox, canvas.width, canvas.height);
        const sparseCornerRect = padRect(
          sparseCornerRegion.rect,
          sparseCornerRegion.sourceCanvas.width,
          sparseCornerRegion.sourceCanvas.height
        );
        const sparseCornerCrop = cropToCanvas(sparseCornerRegion.sourceCanvas, sparseCornerRect, OCR_UPSCALE);
        const expectedCorner = sparseCornerRegion.toOriginalFrame(sparseCornerRegion.corner);

        const worker = await createTesseractWorker(cb.onPreparingScanner);
        if (state.stopped) {
          try {
            worker.terminate();
          } catch (err) {
            logError("terminating a just-created worker after close failed", err);
          }
          return;
        }
        state.tesseractWorker = worker;

        // Sequential, not concurrent, and each with its OWN page segmentation
        // mode + whitelist set explicitly on every call (see recognizeCrop's
        // docstring) - BLOCK_OF_TEXT with NO whitelist for the whole label
        // and SPARSE_TEXT with NO whitelist for the corner pass (both have
        // letters, digits, fractions and punctuation - restricting the
        // alphabet would corrupt everything else on them), SINGLE_LINE with
        // a digit whitelist for the dimension text. None of the three ever
        // leaks into another's call.
        const fullLabelData = await recognizeCrop(worker, fullLabelCrop, "", PSM_BLOCK_OF_TEXT).catch(
          (err) => {
            log("whole-label recognition failed", err);
            return null;
          }
        );
        if (state.stopped) return;
        const sparseCornerData = await recognizeCrop(
          worker,
          sparseCornerCrop,
          "",
          PSM_SPARSE_TEXT
        ).catch((err) => {
          log("sparse-corner recognition failed", err);
          return null;
        });
        if (state.stopped) return;
        const dimensionData = await recognizeCrop(
          worker,
          dimensionCrop,
          "0123456789.x",
          PSM_SINGLE_LINE
        ).catch((err) => {
          log("dimension recognition failed", err);
          return null;
        });
        if (state.stopped) return;

        const words = fullLabelData
          ? wordsFromTesseractResult(
              fullLabelData,
              fullLabelRegion.rect,
              fullLabelRegion.toOriginalFrame,
              FULL_LABEL_UPSCALE
            )
          : [];
        const sparseWords = sparseCornerData
          ? wordsFromTesseractResult(
              sparseCornerData,
              sparseCornerRect,
              sparseCornerRegion.toOriginalFrame,
              OCR_UPSCALE
            )
          : [];
        const dimensionLines = dimensionData
          ? linesFromTesseractResult(
              dimensionData,
              dimensionRect,
              dimensionRegion.toOriginalFrame,
              OCR_UPSCALE
            )
          : [];
        const lines = [...words, ...sparseWords, ...dimensionLines];

        // corner_x/corner_y/qr_size (HOTFIX 4) switch ocr_service.py's line-
        // label ranking from "furthest from the QR" to "nearest the expected
        // corner, with a distance cap and confidence floor" - see
        // app/services/ocr_service.py parse_line_label.
        const result = await window.Api.scanParseLabel({
          lines,
          qr_order_number: orderNumber,
          qr_x: qrBox.centerX,
          qr_y: qrBox.centerY,
          corner_x: expectedCorner.x,
          corner_y: expectedCorner.y,
          qr_size: qrBox.size,
        });
        if (state.stopped) return;
        safeCall(cb.onLineLabelResult, result);

        if (isDebugEnabled()) {
          safeCall(cb.onDebugFrame, {
            method,
            qrCorners,
            boundary,
            normalizedCanvas: normalized ? normalized.canvas : null,
            expectedCorner,
            fullLabelCropCanvas: fullLabelCrop,
            fullLabelRect: fullLabelRegion.rect,
            fullLabelText: (fullLabelData && fullLabelData.text) || "",
            fullLabelConfidence: fullLabelData ? fullLabelData.confidence : null,
            sparseCornerCropCanvas: sparseCornerCrop,
            sparseCornerRect,
            sparseCornerText: (sparseCornerData && sparseCornerData.text) || "",
            sparseCornerConfidence: sparseCornerData ? sparseCornerData.confidence : null,
            dimensionCropCanvas: dimensionCrop,
            dimensionRect,
            dimensionText: (dimensionData && dimensionData.text) || "",
            dimensionConfidence: dimensionData ? dimensionData.confidence : null,
            words,
            sparseWords,
            parseResult: result,
          });
        }
      }

      async function runCloudProviderPath(qrBox, orderNumber) {
        canvas.width = video.videoWidth;
        canvas.height = video.videoHeight;
        ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
        const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.9));
        if (!blob) throw new Error("could not capture a photo from the camera");
        if (state.stopped) return;

        const formData = new FormData();
        formData.append("image", blob, "label.jpg");
        formData.append("qr_order_number", orderNumber || "");
        formData.append("qr_x", String(qrBox.centerX));
        formData.append("qr_y", String(qrBox.centerY));
        const result = await window.Api.scanDiagnose(formData);
        if (state.stopped) return;
        safeCall(cb.onLineLabelResult, result);
      }

      // --- QR loop: scheduled first and runs independently of OCR - a
      // broken/slow text reader degrades the feature, it never disables
      // scanning altogether. ---
      function scheduleNextFrame() {
        if (state.stopped || state.ocrFired) return;
        state.rafHandle = requestAnimationFrame(onFrame);
      }

      async function onFrame() {
        if (state.stopped || state.ocrFired) return;
        try {
          const qr = await detectQrOnce(video, canvas, ctx, state);
          if (qr && qr.text) {
            await handleQrFound(qr);
            return; // ocrFired is now true - no more frames needed
          }
        } catch (err) {
          log("QR detect loop error (continuing)", err);
        }
        scheduleNextFrame();
      }

      scheduleNextFrame();
    })().catch((err) => {
      // Belt-and-braces only: every stage above already has its own try/catch
      // reporting through a callback, but an uncaught rejection here must
      // still be visible rather than a silently-dead session.
      logError("unexpected error starting the scanner", err);
      safeCall(cb.onError, "The scanner ran into an unexpected problem. Enter the details manually.");
    });

    return controller;
  }

  window.LabelScan = { startScan, isDebugEnabled };

  // Pure functions exposed for disposable Node-based verification harnesses
  // only (see the hotfix commit messages) - never used by any other browser
  // code path, and no browser code should ever call into `_internal`.
  window.LabelScan._internal = {
    vecSub,
    vecAdd,
    vecScale,
    vecLen,
    vecNormalize,
    qrAxesFromCorners,
    solveAffine,
    invertAffine,
    applyAffine,
    isWoodBackgroundColor,
    walkToEdge,
    detectLabelBoundary,
    buildNormalizedLabelCanvas,
    deriveDimensionRegionFromNormalizedLabel,
    deriveDimensionRegionFromQrExtrapolation,
    deriveFullLabelRegionFromNormalizedLabel,
    deriveFullLabelRegionFromQrExtrapolation,
    expectedCornerInNormalizedSpace,
    expectedCornerFromQrExtrapolation,
    deriveSparseCornerRegionFromNormalizedLabel,
    deriveSparseCornerRegionFromQrExtrapolation,
    padRect,
  };
})();
