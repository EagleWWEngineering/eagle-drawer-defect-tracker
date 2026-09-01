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
 *      server round trip for recognition itself - only the already-recognised
 *      TEXT is posted to POST /api/v1/scan/parse-label for parsing/validation
 *      (app/services/ocr_service.py - one implementation, tested in Python,
 *      never duplicated here). If OCR_PROVIDER is a cloud option instead
 *      (azure/google/anthropic), the captured photo is posted to
 *      POST /api/v1/scan/diagnose instead of running Tesseract locally.
 *
 * Manual entry always works, at every step - this module only ever fills form
 * fields via the callbacks the caller supplies; it never submits anything, and
 * every field it touches stays a normal, editable input afterward.
 *
 * HOTFIX (2026-09-01) - "scan modal will not close, label is never read":
 * startScan() used to be a single `async function` that did camera setup, QR
 * looping, and OCR all in one linear sequence, returning its {stop} controller
 * only as the RESOLVED VALUE of the promise it implicitly returned. The caller
 * (app/templates/defect_entry.html) never awaited that promise, so
 * `scanController` was bound to the pending/rejecting PROMISE itself, not the
 * controller - `scanController.stop()` then threw `TypeError: scanController.
 * stop is not a function` on every close attempt, which aborted
 * closeScanModal() before it could hide the modal, restore body.overflow, or
 * stop the camera. Worse: because everything (including starting the QR loop)
 * was sequenced strictly after `await video.play()`, ANY rejection there (a
 * real, fairly common browser condition - e.g. an AbortError when play() is
 * interrupted) silently killed the entire session, QR included, before the
 * loop ever started - reproduced and confirmed via a Node-based simulation
 * harness (no camera/browser available in the environment this was fixed in).
 *
 * Fixed by restructuring startScan() to build and return a fully-working
 * `{stop}` controller SYNCHRONOUSLY, before any fallible async setup runs at
 * all (see startScan below) - closing never depends on anything else
 * succeeding, and every fallible stage reports failure via a callback instead
 * of an unhandled rejection that silently ends the session.
 *
 * NOTE ON THE CROP GEOMETRY BELOW: this repo's label layout ("line label sits
 * diagonally opposite the QR code", "ship code sits beside it") comes from the
 * phase's written spec, not a physical sample label measured in this
 * environment (no camera/hardware access here). deriveLineLabelCrop/
 * deriveDimensionCrop below are a documented first-pass heuristic - expect to
 * tune LINE_LABEL_CROP_SIZE_FACTOR / DIMENSION_CROP_* against a real printed label during
 * pilot rollout.
 */

(function () {
  "use strict";

  const VENDOR_BASE = "/static/js/vendor/";

  // --- Crop geometry heuristic (see NOTE above) --------------------------
  // Sized/positioned relative to the QR's own pixel size in the frame (qrSize),
  // so it scales naturally whether the label fills the frame or sits far away.
  const LINE_LABEL_CROP_SIZE_FACTOR = 1.6; // crop side length, in multiples of qrSize
  const DIMENSION_CROP_WIDTH_FACTOR = 3.2;
  const DIMENSION_CROP_HEIGHT_FACTOR = 1.0;
  const OCR_UPSCALE = 3; // Tesseract reads small, tightly-cropped text far better upscaled

  function log(...args) {
    if (window.LABEL_SCAN_DEBUG) console.log("[label-scan]", ...args);
  }

  /** Every failure in this module goes through here at least once - console
   * output is UNCONDITIONAL (never gated behind a debug flag), so a real
   * problem is always diagnosable from DevTools even when nobody is watching
   * for it (fail-loud, matching the rest of this app - see the hotfix note
   * above: a silently-swallowed failure is exactly what caused "nothing is
   * ever read" to go unnoticed). */
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

  // -------------------------------------------------------------------------
  // Tesseract.js worker
  // -------------------------------------------------------------------------
  //
  // Deliberately NOT a page-level singleton (it used to be, to amortise the
  // ~6MB first-load cost across repeated scans) - the hotfix above requires
  // every scan session's worker to be fully terminated on close, with no
  // shared state that could outlive one "Scan label" attempt. The underlying
  // vendored files are still served from this app's own static handler and
  // cached by the browser's normal HTTP cache after the first load, so a
  // fresh worker on the next scan is still fast in practice.

  async function createTesseractWorker(onPreparing) {
    safeCall(onPreparing);
    return window.Tesseract.createWorker("eng", 1, {
      workerPath: VENDOR_BASE + "tesseract-worker.min.js",
      corePath: VENDOR_BASE + "tesseract-core-lstm.wasm.js",
      langPath: VENDOR_BASE,
      logger: (m) => log("tesseract", m.status, m.progress),
    });
  }

  /** Sequential, not Promise.all'd: tessedit_char_whitelist is a WORKER-wide
   * parameter (setParameters), not a per-call option, so recognising the two
   * crops "concurrently" on one worker would race two different whitelists
   * against each other and could silently corrupt either result. This is a
   * second, independent correctness bug found auditing this code path during
   * the same hotfix - not just a performance choice. */
  async function recognizeCrop(worker, canvas, whitelist) {
    await worker.setParameters({
      tessedit_char_whitelist: whitelist,
      tessedit_pageseg_mode: "7", // PSM.SINGLE_LINE - a tight, single-line crop
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
   * { text, corners: [{x,y} x4] } (corners in video-pixel coordinates), or null. */
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
  // Crop derivation - see NOTE at the top of this file
  // -------------------------------------------------------------------------

  function clampRect(rect, frameWidth, frameHeight) {
    const x = Math.max(0, Math.min(rect.x, frameWidth - 1));
    const y = Math.max(0, Math.min(rect.y, frameHeight - 1));
    const width = Math.max(1, Math.min(rect.width, frameWidth - x));
    const height = Math.max(1, Math.min(rect.height, frameHeight - y));
    return { x, y, width, height };
  }

  /** The label's corner diagonally opposite the QR: reflect the QR's centroid
   * through the frame's own center, then crop a square around that mirrored
   * point. Sized off qrSize so it scales with how close the label is to the
   * camera. See NOTE at the top of this file. */
  function deriveLineLabelCrop(qrBox, frameWidth, frameHeight) {
    const mirroredX = frameWidth - qrBox.centerX;
    const mirroredY = frameHeight - qrBox.centerY;
    const size = qrBox.size * LINE_LABEL_CROP_SIZE_FACTOR;
    return clampRect(
      { x: mirroredX - size / 2, y: mirroredY - size / 2, width: size, height: size },
      frameWidth,
      frameHeight
    );
  }

  /** The dimension line: placed alongside the QR (same general area as the
   * order number/quantity/ship code text), wide and short. See NOTE above. */
  function deriveDimensionCrop(qrBox, frameWidth, frameHeight) {
    const width = qrBox.size * DIMENSION_CROP_WIDTH_FACTOR;
    const height = qrBox.size * DIMENSION_CROP_HEIGHT_FACTOR;
    return clampRect(
      { x: qrBox.x + qrBox.size * 1.1, y: qrBox.centerY - height / 2, width, height },
      frameWidth,
      frameHeight
    );
  }

  function cropToCanvas(sourceCanvas, rect) {
    const out = document.createElement("canvas");
    out.width = Math.max(1, Math.round(rect.width * OCR_UPSCALE));
    out.height = Math.max(1, Math.round(rect.height * OCR_UPSCALE));
    const ctx = out.getContext("2d");
    ctx.imageSmoothingEnabled = true;
    ctx.drawImage(
      sourceCanvas,
      rect.x,
      rect.y,
      rect.width,
      rect.height,
      0,
      0,
      out.width,
      out.height
    );
    return out;
  }

  /** Tesseract line bboxes come back in the CROP's own upscaled pixel space -
   * translate each line's center back into the ORIGINAL frame's coordinate
   * space (undo the crop offset and the upscale factor) so they rank against
   * qr_x/qr_y exactly like a cloud provider's lines already do
   * (app/services/ocr_service.py parse_line_label). */
  function linesFromTesseractResult(data, rect) {
    const lines = (data && data.lines) || [];
    return lines
      .map((line) => {
        const text = (line.text || "").trim();
        if (!text) return null;
        const bbox = line.bbox || { x0: 0, x1: 0, y0: 0, y1: 0 };
        const cx = rect.x + (bbox.x0 + bbox.x1) / 2 / OCR_UPSCALE;
        const cy = rect.y + (bbox.y0 + bbox.y1) / 2 / OCR_UPSCALE;
        return { text, x: cx, y: cy };
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
   *
   * Returns a controller ({ stop() }) SYNCHRONOUSLY and immediately - stop()
   * is fully functional the instant this returns, before camera permission has
   * even been requested, let alone granted. Closing must never depend on
   * anything else in this module succeeding (see the hotfix note at the top
   * of this file).
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
        // silently end the session with the modal stuck open (see hotfix note).
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
            await runTesseractPath(qrBox, orderNumber);
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
      }

      async function runTesseractPath(qrBox, orderNumber) {
        canvas.width = video.videoWidth;
        canvas.height = video.videoHeight;
        ctx.drawImage(video, 0, 0, canvas.width, canvas.height);

        const lineLabelRect = deriveLineLabelCrop(qrBox, canvas.width, canvas.height);
        const dimensionRect = deriveDimensionCrop(qrBox, canvas.width, canvas.height);
        const lineLabelCrop = cropToCanvas(canvas, lineLabelRect);
        const dimensionCrop = cropToCanvas(canvas, dimensionRect);

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

        // Sequential, not concurrent - see recognizeCrop's docstring above.
        const lineLabelData = await recognizeCrop(
          worker,
          lineLabelCrop,
          "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        ).catch((err) => {
          log("line-label recognition failed", err);
          return null;
        });
        if (state.stopped) return;
        const dimensionData = await recognizeCrop(worker, dimensionCrop, "0123456789.x").catch(
          (err) => {
            log("dimension recognition failed", err);
            return null;
          }
        );
        if (state.stopped) return;

        const lines = [
          ...(lineLabelData ? linesFromTesseractResult(lineLabelData, lineLabelRect) : []),
          ...(dimensionData ? linesFromTesseractResult(dimensionData, dimensionRect) : []),
        ];

        const result = await window.Api.scanParseLabel({
          lines,
          qr_order_number: orderNumber,
          qr_x: qrBox.centerX,
          qr_y: qrBox.centerY,
        });
        if (state.stopped) return;
        safeCall(cb.onLineLabelResult, result);
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
      // scanning altogether (PROJECT_SPEC_PHASE9.md hotfix, Step 2C). ---
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
      // still be visible rather than a silently-dead session (see hotfix note).
      logError("unexpected error starting the scanner", err);
      safeCall(cb.onError, "The scanner ran into an unexpected problem. Enter the details manually.");
    });

    return controller;
  }

  window.LabelScan = { startScan };
})();
