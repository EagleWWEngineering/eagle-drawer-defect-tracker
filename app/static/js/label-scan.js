/* label-scan.js - "Scan label" on the New Defect form.
 *
 * Decodes the QR code printed on a drawer production work order label to fill
 * the work order number field - nothing else. Decoded via the native
 * BarcodeDetector where available, vendored jsQR otherwise (iOS Safari has no
 * native BarcodeDetector).
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
 * Manual entry always works, at every step - this module only ever fills the
 * work order number field via the callback the caller supplies; it never
 * submits anything.
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

  //: The literal double backslash in `.../WorkOrderPDFs/\\178414.pdf` is
  //: malformed at source - tolerate one or more slashes/backslashes before the
  //: six-digit order number.
  const QR_ORDER_NUMBER_RE = /[\\/]+(\d{6})\.pdf/i;

  function extractOrderNumberFromQrText(text) {
    const match = QR_ORDER_NUMBER_RE.exec(text || "");
    return match ? match[1] : null;
  }

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
   *   onOrderNumber(orderNumber) - QR decoded and looked like a work order
   *     label; fired once per startScan() call.
   *   onError(message) - camera/QR-level failure, or a QR that decoded but
   *     didn't look like a work order label; manual entry is the only path.
   *   onScanComplete() - the QR has been found (and onOrderNumber or onError
   *     already called for it) - the signal the caller uses to auto-close the
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

        const orderNumber = extractOrderNumberFromQrText(text);
        if (orderNumber) {
          safeCall(cb.onOrderNumber, orderNumber);
        } else {
          safeCall(
            cb.onError,
            "QR code didn't look like a work order label. Enter the order number manually."
          );
        }
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

  window.LabelScan = { startScan };
})();
