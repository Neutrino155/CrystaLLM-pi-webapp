// This is part of a workaround that enables easy rotation of the structure
// in the StructureMoleculeComponent canvas on mobile/touchscreen devices.

var hasRunBefore = false;

function addTouchHandler() {
    if (hasRunBefore) {
        return;
    }

    // Select the canvas and add the 'allow-touch' class
    var canvas = document.querySelector('canvas');
    canvas.classList.add('allow-touch');

    function touchHandler(event) {
        var touch = event.changedTouches[0];

        var simulatedEvent = document.createEvent("MouseEvent");
            simulatedEvent.initMouseEvent({
            touchstart: "mousedown",
            touchmove: "mousemove",
            touchend: "mouseup"
        }[event.type], true, true, window, 1,
            touch.screenX, touch.screenY,
            touch.clientX, touch.clientY, false,
            false, false, false, 0, null);

        if(touch.target.classList.contains("allow-touch")){
            touch.target.dispatchEvent(simulatedEvent);
            event.preventDefault();
        }
    }

    document.addEventListener("touchstart", touchHandler, true);
    document.addEventListener("touchmove", touchHandler, true);
    document.addEventListener("touchend", touchHandler, true);
    document.addEventListener("touchcancel", touchHandler, true);

    hasRunBefore = true;
}
