if (!window.dash_clientside) {
    window.dash_clientside = {};
}

if (!window.crystallmUi) {
    window.crystallmUi = {
        timeoutMs: 90000,
        timeoutTimerId: null,
        pending: false,
        timedOut: false,
    };
}

function crystallmGet(id) {
    return document.getElementById(id);
}

function crystallmClearTimeoutFallback() {
    if (window.crystallmUi.timeoutTimerId) {
        clearTimeout(window.crystallmUi.timeoutTimerId);
        window.crystallmUi.timeoutTimerId = null;
    }
}

function crystallmHideFallbackMessage() {
    const node = crystallmGet("ui-timeout-message");
    if (node && node.parentNode) {
        node.parentNode.removeChild(node);
    }
}

function crystallmResetProgressUi() {
    if (typeof stopProgress === "function") {
        stopProgress({ complete: false });
        return;
    }

    const progressWrap = crystallmGet("progress");
    const progressBar = crystallmGet("progress-bar-inner");
    const stage = crystallmGet("progress-stage");
    const percentNode = crystallmGet("progress-percent");

    if (progressWrap) {
        progressWrap.style.display = "none";
    }
    if (progressBar) {
        progressBar.style.width = "0%";
    }
    if (stage) {
        stage.textContent = "Preparing request…";
    }
    if (percentNode) {
        percentNode.textContent = "0%";
    }
}

function crystallmShowFallbackMessage(title, body) {
    const button = crystallmGet("submit-button");
    const emptyState = crystallmGet("empty-state");

    crystallmResetProgressUi();
    crystallmHideFallbackMessage();

    if (button) {
        button.disabled = false;
    }

    if (emptyState) {
        const wrapper = document.createElement("div");
        wrapper.id = "ui-timeout-message";
        wrapper.style.marginBottom = "14px";
        wrapper.innerHTML = [
            '<div class="error-message">' + title + '</div>',
            '<div class="help-text" style="margin-top: 6px;">' + body + '</div>'
        ].join("");
        emptyState.insertBefore(wrapper, emptyState.firstChild);
    }

    window.crystallmUi.pending = false;
    window.crystallmUi.timedOut = true;
    crystallmClearTimeoutFallback();
}

function crystallmHandleTimeout() {
    if (!window.crystallmUi.pending || window.crystallmUi.timedOut) {
        return;
    }

    crystallmShowFallbackMessage(
        "Generation timed out",
        "CrystaLLM-pi did not produce a result before the timeout.<br>Please try again with less conditions or on a simpler composition.<br>If this keeps happening, contact support@psdi.ac.uk."
    );
}

window.dash_clientside.clientside = {
    attachClickHandler: function(value) {
        const button = crystallmGet("submit-button");
        if (button && !button.dataset.bound) {
            button.dataset.bound = "1";
            button.addEventListener("click", function() {
                crystallmHideFallbackMessage();
                window.crystallmUi.pending = true;
                window.crystallmUi.timedOut = false;
                button.disabled = true;
                if (typeof startProgress === "function") {
                    startProgress({ timeoutMs: window.crystallmUi.timeoutMs });
                }
            });
        }
        return value;
    },

    setButtonState: function(data) {
        const button = crystallmGet("submit-button");
        if (data === 0) {
            if (button) {
                button.disabled = false;
            }
            crystallmClearTimeoutFallback();

            if (!window.crystallmUi.timedOut) {
                crystallmHideFallbackMessage();
                crystallmResetProgressUi();
            }

            window.crystallmUi.pending = false;
        } else if (data === 1 && button) {
            button.disabled = true;
        }
        return data;
    }
};
