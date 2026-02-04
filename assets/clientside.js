if (!window.dash_clientside) {
    window.dash_clientside = {};
}

window.dash_clientside.clientside = {

    attachClickHandler: function(value) {
        let button = document.getElementById("submit-button");

        if (button && !button.dataset.bound) {
            button.dataset.bound = "1";
            button.addEventListener("click", function() {
                button.disabled = true;
                if (typeof startProgress === "function") startProgress();
            });
        }
        return value;
    },

    setButtonState: function(data) {
        let button = document.getElementById("submit-button");
        if (!button) return data;

        if (data === 0) {
            button.disabled = false;
            if (window.progress > 0) window.progress = 100;
        } else if (data === 1) {
            button.disabled = true;
        }
        return data;
    }
};
