/* label-scan.js - "Scan label" on the New Defect form.
 *
 * Decodes the QR code printed on a drawer production work order label and
 * hands the RAW decoded text to the caller - nothing else. Decoded via the
 * native BarcodeDetector where available, vendored jsQR otherwise (iOS Safari
 * has no native BarcodeDetector).
 *
 * PHASE 10 (PROJECT_SPEC_PHASE10.md Part 2): this module no longer extracts
 * the order number itself. Labels now come in two generations - old
 * order-only, and unique-ID ones carrying "#drawer=<order_detail_id>-<unit>" -
 * and the payload is parsed in exactly one place, server-side
 * (app/services/label_service.py, via POST /api/v1/labels/resolve, which the
 * caller in app/templates/defect_entry.html posts the text to).
 *
 * PHASE 9 REMOVAL (2026-09-09): this file used to also read the work order
 * line and dimensions off the label's printed text via Tesseract.js (vendored,
 * running entirely in the browser) or an optional cloud OCR provider. Five
 * attempts to read the line label's corner letter reliably did not succeed
 * against real printed labels (see git history / PROJECT_SPEC_PHASE9.md for
 * the full sequence) - that entire OCR path (client-side Tesseract, the
 * server-side parsing/validation in app/services/ocr_service.py, the
 * /api/v1/scan/* endpoints, and the vendored Tesseract assets) has been
 * removed. The work order line is now entered by tapping an always-present
 * A-Z letter picker on the New Defect form, or by typing - see
 * app/templates/defect_entry.html. QR decoding here is unchanged; it worked
 * perfectly the whole time.
 *
 * Manual entry always works, at every step - this module only ever reports
 * what it decoded via the callbacks the caller supplies; it never fills or
 * submits anything itself.
 */

(function () {
  "use strict";

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
  // QR decoding
  // -------------------------------------------------------------------------

  /** Returns the QR's raw decoded text, or null - via the native
   * BarcodeDetector where available, vendored jsQR otherwise. */
  async function detectQrOnce(video, canvas, ctx, state) {
    if (window.BarcodeDetector && !state.barcodeDetectorUnsupported) {
      try {
        if (!state.barcodeDetector) {
          state.barcodeDetector = new window.BarcodeDetector({ formats: ["qr_code"] });
        }
        const results = await state.barcodeDetector.detect(video);
        if (results && results.length) {
          return results[0].rawValue;
        }
        return null;
      } catch (err) {
        // A genuine "not supported on this platform" failure (e.g. some
        // desktop Chrome builds advertise BarcodeDetector without a working
        // backend) would otherwise retry - and fail - on every single frame.
        // Fall back to jsQR for the rest of this session instead.
        state.barcodeDetectorUnsupported = true;
      }
    }
    if (window.jsQR) {
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
      const imageData = ctx.getImageData(0, 0, canvas.width, canvas.height);
      const result = window.jsQR(imageData.data, imageData.width, imageData.height);
      return result ? result.data : null;
    }
    return null;
  }

  // -------------------------------------------------------------------------
  // Public API
  // -------------------------------------------------------------------------

  /** Opens the camera against the given <video>/<canvas> elements and scans
   * until a QR code is found (or the caller calls the returned controller's
   * stop()). Callbacks:
   *   onQrText(text) - a QR was decoded; `text` is its raw content, unparsed
   *     (the caller resolves it server-side). Fired once per startScan() call.
   *   onError(message) - camera-level failure; manual entry is the only path.
   *   onScanComplete() - the QR has been found (and onQrText already called
   *     for it) - the signal the caller uses to auto-close the
   *     scanner. Never fired if the operator closes the modal before a QR is
   *     ever found.
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
      qrFired: false,
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
          "Could not access the camera (" + err.message + "). Enter the order number manually."
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
          "Could not start the camera preview (" + err.message + "). Enter the order number manually."
        );
        return;
      }
      if (state.stopped) return;

      const ctx = canvas.getContext("2d", { willReadFrequently: true });

      function handleQrFound(text) {
        if (state.qrFired) return;
        state.qrFired = true;

        safeCall(cb.onQrText, text);
        safeCall(cb.onScanComplete);
      }

      function scheduleNextFrame() {
        if (state.stopped || state.qrFired) return;
        state.rafHandle = requestAnimationFrame(onFrame);
      }

      async function onFrame() {
        if (state.stopped || state.qrFired) return;
        try {
          const text = await detectQrOnce(video, canvas, ctx, state);
          if (text) {
            handleQrFound(text);
            return; // qrFired is now true - no more frames needed
          }
        } catch (err) {
          logError("QR detect loop error (continuing)", err);
        }
        scheduleNextFrame();
      }

      scheduleNextFrame();
    })().catch((err) => {
      // Belt-and-braces only: every stage above already has its own try/catch
      // reporting through a callback, but an uncaught rejection here must
      // still be visible rather than a silently-dead session.
      logError("unexpected error starting the scanner", err);
      safeCall(cb.onError, "The scanner ran into an unexpected problem. Enter the order number manually.");
    });

    return controller;
  }

  /* 2026-10-07: read the QR code from a PHOTO of the label. The live camera view
   * (startScan) needs a secure (https) page; on eagle-vm the tracker is served
   * over plain http, where browsers refuse it. Taking a picture with the camera
   * app (<input type="file" capture>) works on http, and this decodes it.
   * Resolves the QR text, or null when no code could be read. */
  async function loadImage(file) {
    if (window.createImageBitmap) {
      try {
        return await createImageBitmap(file, { imageOrientation: "from-image" });
      } catch (err) {
        /* older browsers: fall through to an <img> */
      }
    }
    return await new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => {
        URL.revokeObjectURL(url);
        resolve(img);
      };
      img.onerror = (e) => {
        URL.revokeObjectURL(url);
        reject(e);
      };
      img.src = url;
    });
  }

  async function decodeImageFile(file) {
    const image = await loadImage(file);
    const width = image.width || image.naturalWidth;
    const height = image.height || image.naturalHeight;
    if (window.BarcodeDetector) {
      try {
        const detector = new window.BarcodeDetector({ formats: ["qr_code"] });
        const codes = await detector.detect(image);
        if (codes.length && codes[0].rawValue) return codes[0].rawValue;
      } catch (err) {
        /* not supported on this device - jsQR below */
      }
    }
    if (!window.jsQR) return null;
    // A full-size tablet photo is too big for jsQR and a label far away is too
    // small when shrunk, so try a few sizes.
    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    for (const target of [1600, 1000, 2400]) {
      const scale = Math.min(1, target / Math.max(width, height));
      canvas.width = Math.round(width * scale);
      canvas.height = Math.round(height * scale);
      ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
      const data = ctx.getImageData(0, 0, canvas.width, canvas.height);
      const result = window.jsQR(data.data, data.width, data.height, { inversionAttempts: "attemptBoth" });
      if (result && result.data) return result.data;
      if (scale === 1) break;
    }
    return null;
  }

  /** True when this page may use the live camera view at all. */
  function liveCameraAvailable() {
    return Boolean(window.isSecureContext && navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  }

  window.LabelScan = { startScan, decodeImageFile, liveCameraAvailable };
})();
