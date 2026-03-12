(function () {
  function dispatchMouseEvent(target, type, touch) {
    const event = new MouseEvent(type, {
      bubbles: true,
      cancelable: true,
      view: window,
      screenX: touch.screenX,
      screenY: touch.screenY,
      clientX: touch.clientX,
      clientY: touch.clientY,
      button: 0,
      buttons: 1,
    });
    target.dispatchEvent(event);
  }

  function bindCanvas(canvas) {
    if (!canvas || canvas.dataset.touchBound === "1") {
      return;
    }

    canvas.dataset.touchBound = "1";
    canvas.classList.add("allow-touch");

    const handler = function (event) {
      if (!event.changedTouches || event.changedTouches.length !== 1) {
        return;
      }

      const touch = event.changedTouches[0];
      const target = canvas;
      const map = {
        touchstart: "mousedown",
        touchmove: "mousemove",
        touchend: "mouseup",
        touchcancel: "mouseup",
      };

      const mappedType = map[event.type];
      if (!mappedType) {
        return;
      }

      dispatchMouseEvent(target, mappedType, touch);
      event.preventDefault();
    };

    canvas.addEventListener("touchstart", handler, { passive: false });
    canvas.addEventListener("touchmove", handler, { passive: false });
    canvas.addEventListener("touchend", handler, { passive: false });
    canvas.addEventListener("touchcancel", handler, { passive: false });
  }

  function attachTouchHandler() {
    const frame = document.querySelector("#structure-viewer canvas, .ctk-viewer-frame canvas");
    if (frame) {
      bindCanvas(frame);
    }
  }

  const observer = new MutationObserver(function () {
    attachTouchHandler();
  });

  function start() {
    attachTouchHandler();
    observer.observe(document.body, { childList: true, subtree: true });
    window.addEventListener("resize", attachTouchHandler, { passive: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }

  window.addTouchHandler = attachTouchHandler;
})();
