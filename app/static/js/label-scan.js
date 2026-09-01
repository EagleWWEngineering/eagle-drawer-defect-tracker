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
 * NOTE ON THE CROP GEOMETRY BELOW: this repo's label layout ("line label sits
 * diagonally opposite the QR code", "ship code sits beside it") comes from the
 * phase's written spec, not a physical sample label measured in this
 * environment (no camera/hardware access here). deriveCrops() below is a
 * documented first-pass heuristic - expect to tune LINE_LABEL_CROP_* /
 * DIMENSION_CROP_* against a real printed label during pilot rollout.
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

  let tesseractWorkerPromise = null;
  let preparingIndicatorShown = false;

  function log(...args) {
    if (window.LABEL_SCAN_DEBUG) console.log("[label-scan]", ...args);
  }

  // -------------------------------------------------------------------------
  // Tesseract.js worker (lazy singleton - created once per page load, reused
  // across scans so only the FIRST scan pays the "preparing scanner" delay)
  // -------------------------------------------------------------------------

  function getTesseractWorker(onPreparing) {
    if (!tesseractWorkerPromise) {
      if (onPreparing && !preparingIndicatorShown) {
        preparingIndicatorShown = true;
        onPreparing();
      }
      tesseractWorkerPromise = window.Tesseract.createWorker("eng", 1, {
        workerPath: VENDOR_BASE + "tesseract-worker.min.js",
        corePath: VENDOR_BASE + "tesseract-core-lstm.wasm.js",
        langPath: VENDOR_BASE,
        logger: (m) => log("tesseract", m.status, m.progress),
      }).catch((err) => {
        // Let the next scan attempt retry from scratch instead of being stuck
        // on a permanently-rejected promise.
        tesseractWorkerPromise = null;
        throw err;
      });
    }
    return tesseractWorkerPromise;
  }

  async function recognizeCrop(canvas, whitelist) {
    const worker = await getTesseractWorker();
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
  async function detectQrOnce(video, canvas, ctx) {
    if (window.BarcodeDetector) {
      try {
        if (!detectQrOnce._detector) {
          detectQrOnce._detector = new window.BarcodeDetector({ formats: ["qr_code"] });
        }
        const results = await detectQrOnce._detector.detect(video);
        if (results && results.length) {
          const r = results[0];
          return { text: r.rawValue, corners: r.cornerPoints };
        }
        return null;
      } catch (err) {
        log("BarcodeDetector failed, falling back to jsQR", err);
        // fall through to jsQR below
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
   * until a QR code is found (or the caller calls stop()). Callbacks:
   *   onOrderNumber(orderNumber) - QR decoded; fired once per open() call.
   *   onPreparingScanner() - first-ever Tesseract use on this device/session;
   *     show a "preparing scanner" indicator so the delay doesn't look like a hang.
   *   onLineLabelResult(result) - the parsed/validated result from either
   *     /parse-label or /diagnose (see app/schemas.py ScanDiagnosticOut) - may
   *     have line_label: null (nothing readable, or line_label_discarded).
   *   onOcrUnavailable(message) - OCR disabled or errored; line stays manual.
   *   onError(message) - camera/QR-level failure; manual entry is the only path.
   * Returns a controller: { stop() }.
   */
  async function startScan(video, canvas, callbacks) {
    const cb = callbacks || {};
    let stopped = false;
    let stream = null;
    let ocrFired = false;

    try {
      stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: "environment" },
        audio: false,
      });
    } catch (err) {
      (cb.onError || function () {})(
        "Could not access the camera (" + err.message + "). Enter the order number and line manually."
      );
      return { stop() {} };
    }

    video.srcObject = stream;
    await video.play();
    const ctx = canvas.getContext("2d", { willReadFrequently: true });

    function stop() {
      stopped = true;
      stream.getTracks().forEach((t) => t.stop());
      video.srcObject = null;
    }

    async function handleQrFound(qr) {
      if (ocrFired) return;
      ocrFired = true;

      const orderNumber = extractOrderNumberFromQrText(qr.text);
      if (orderNumber) {
        (cb.onOrderNumber || function () {})(orderNumber);
      } else {
        (cb.onError || function () {})(
          "QR code didn't look like a work order label. Enter the order number manually."
        );
      }

      let scanConfig;
      try {
        scanConfig = await window.Api.getScanConfig();
      } catch (err) {
        (cb.onOcrUnavailable || function () {})("Could not reach the server to check OCR settings.");
        return;
      }
      if (!scanConfig.enabled) {
        (cb.onOcrUnavailable || function () {})("OCR is turned off on this server. Type the line.");
        return;
      }

      const qrBox = qrBoundingBox(qr.corners);

      try {
        if (scanConfig.provider === "tesseract") {
          await runTesseractPath(qrBox, orderNumber, cb);
        } else {
          await runCloudProviderPath(qrBox, orderNumber, cb);
        }
      } catch (err) {
        (cb.onOcrUnavailable || function () {})(
          "Couldn't read the line label automatically (" + err.message + "). Type it in."
        );
      }
    }

    async function runTesseractPath(qrBox, orderNumber, cb) {
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);

      const lineLabelRect = deriveLineLabelCrop(qrBox, canvas.width, canvas.height);
      const dimensionRect = deriveDimensionCrop(qrBox, canvas.width, canvas.height);
      const lineLabelCrop = cropToCanvas(canvas, lineLabelRect);
      const dimensionCrop = cropToCanvas(canvas, dimensionRect);

      const [lineLabelData, dimensionData] = await Promise.all([
        recognizeCrop(lineLabelCrop, "ABCDEFGHIJKLMNOPQRSTUVWXYZ").catch((err) => {
          log("line-label recognition failed", err);
          return null;
        }),
        recognizeCrop(dimensionCrop, "0123456789.x").catch((err) => {
          log("dimension recognition failed", err);
          return null;
        }),
      ]);

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
      (cb.onLineLabelResult || function () {})(result);
    }

    async function runCloudProviderPath(qrBox, orderNumber, cb) {
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.9));
      if (!blob) throw new Error("could not capture a photo from the camera");

      const formData = new FormData();
      formData.append("image", blob, "label.jpg");
      formData.append("qr_order_number", orderNumber || "");
      formData.append("qr_x", String(qrBox.centerX));
      formData.append("qr_y", String(qrBox.centerY));
      const result = await window.Api.scanDiagnose(formData);
      (cb.onLineLabelResult || function () {})(result);
    }

    (async function loop() {
      while (!stopped && !ocrFired) {
        try {
          const qr = await detectQrOnce(video, canvas, ctx);
          if (qr && qr.text) {
            await handleQrFound(qr);
            break;
          }
        } catch (err) {
          log("QR detect loop error (continuing)", err);
        }
        await new Promise((r) => requestAnimationFrame(r));
      }
    })();

    return { stop };
  }

  window.LabelScan = {
    startScan,
    getTesseractWorker, // exposed so the New Defect form can pre-warm it on page load if it wants
  };
})();
