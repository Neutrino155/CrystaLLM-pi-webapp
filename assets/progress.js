window.progress = 0;
window.progressTimerId = null;

function setProgressDisplay(percent, stageText) {
    const progressBar = document.querySelector(".progress-bar-inner");
    const stage = document.getElementById("progress-stage");
    const percentNode = document.getElementById("progress-percent");

    if (progressBar) {
        progressBar.style.width = percent + "%";
    }
    if (stage) {
        stage.textContent = stageText || "Preparing request…";
    }
    if (percentNode) {
        percentNode.textContent = Math.round(percent) + "%";
    }
}

function stopProgress(options) {
    const opts = options || {};
    const complete = !!opts.complete;

    if (window.progressTimerId) {
        clearTimeout(window.progressTimerId);
        window.progressTimerId = null;
    }

    const progressBarContainer = document.getElementById("progress");
    if (!progressBarContainer) {
        window.progress = complete ? 100 : 0;
        return;
    }

    if (complete) {
        window.progress = 100;
        setProgressDisplay(100, "Complete");
        setTimeout(function() {
            progressBarContainer.style.display = "none";
            window.progress = 0;
            setProgressDisplay(0, "Preparing request…");
        }, 180);
    } else {
        progressBarContainer.style.display = "none";
        window.progress = 0;
        setProgressDisplay(0, "Preparing request…");
    }
}

function progressStageFor(elapsedMs, timeoutMs) {
    const t = elapsedMs / Math.max(timeoutMs, 1);

    if (t < 0.08) {
        return { label: "Submitting request…", percent: 6 + (t / 0.08) * 10 };
    }
    if (t < 0.45) {
        const local = (t - 0.08) / 0.37;
        return { label: "Generating candidates…", percent: 16 + local * 50 };
    }
    if (t < 0.72) {
        const local = (t - 0.45) / 0.27;
        return { label: "Checking candidate structures…", percent: 66 + local * 18 };
    }
    if (t < 0.9) {
        const local = (t - 0.72) / 0.18;
        return { label: "Finalising result…", percent: 84 + local * 10 };
    }
    if (t < 1.0) {
        const local = (t - 0.90) / 0.10;
        return { label: "Still working…", percent: 94 + local * 6 };
    }

    return { label: "Generation timed out", percent: 100 };
}

function startProgress(options) {
    const opts = options || {};
    const timeoutMs = opts.timeoutMs || 90000;
    const progressBarContainer = document.getElementById("progress");
    if (!progressBarContainer) return;

    stopProgress({ complete: false });
    progressBarContainer.style.display = "block";

    const startedAt = Date.now();

    function updateProgress() {
        if (window.crystallmUi && !window.crystallmUi.pending) {
            return;
        }

        const elapsedMs = Date.now() - startedAt;

        if (elapsedMs >= timeoutMs) {
            window.progress = 100;
            setProgressDisplay(100, "Generation timed out");
            if (typeof crystallmHandleTimeout === "function") {
                crystallmHandleTimeout();
            } else {
                stopProgress({ complete: false });
            }
            return;
        }

        const stage = progressStageFor(elapsedMs, timeoutMs);
        window.progress = Math.min(100, stage.percent);
        setProgressDisplay(window.progress, stage.label);
        window.progressTimerId = setTimeout(updateProgress, 100);
    }

    updateProgress();
}
