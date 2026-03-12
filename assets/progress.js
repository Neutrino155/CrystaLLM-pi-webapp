window.progress = 0;

function startProgress() {
    let progressBarContainer = document.getElementById("progress");
    if (!progressBarContainer) return;

    progressBarContainer.style.display = "block";

    let progressBar = document.querySelector(".progress-bar-inner");
    if (!progressBar) return;

    window.progress = 0;

    // Use a simple and robust timing model for the progress animation.
    let isLarge = false;

    let hasZ = false;
    try {
        hasZ = !!document.querySelector("#formula-unit .Select-value-label");
    } catch (e) {
        hasZ = false;
    }

    let phase1Percent = hasZ ? 80 : 60;

    let phase1Upper = hasZ ? 3.4 : 2.0;
    let phase1Lower = 1.0;
    let phase2Upper = hasZ ? 0.4 : 0.6;
    let phase2Lower = hasZ ? 0.2 : 0.3;

    if (isLarge) {
        phase1Upper = hasZ ? 1.8 : 1.0;
        phase1Lower = 0.5;
        phase2Upper = hasZ ? 0.3 : 0.4;
        phase2Lower = hasZ ? 0.1 : 0.2;
    }

    function getRandomIncrement(isPhase1) {
        if (isPhase1) {
            return Math.random() * phase1Upper + phase1Lower;
        } else {
            return Math.random() * phase2Upper + phase2Lower;
        }
    }

    function updateProgress() {
        let updateInterval;

        if (window.progress < phase1Percent) {
            window.progress += getRandomIncrement(true);
            updateInterval = 500;
        } else if (window.progress < 100) {
            window.progress += getRandomIncrement(false);
            updateInterval = 1200;
        }

        progressBar.style.width = window.progress + "%";

        if (window.progress < 100) {
            setTimeout(updateProgress, updateInterval);
        } else {
            progressBarContainer.style.display = "none";
            progressBar.style.width = "0%";
        }
    }

    updateProgress();
}
