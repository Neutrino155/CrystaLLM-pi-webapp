if (!window.dash_clientside) {
    window.dash_clientside = {};
}

window.dash_clientside.clientside = {

    attachClickHandler: function(value) {
        let button = document.getElementById("submit-button");

        if (button) {
            button.addEventListener("click", function() {
                button.disabled = true;
                startProgress();
            });
        }

        return value;
    },

    setButtonState: function(data) {
        let button = document.getElementById("submit-button");
        if (data === 0 && button) {
            button.disabled = false;
            if (window.progress > 0) {
                window.progress = 100;
            }
        } else if (data === 1 && button) {
            button.disabled = true;
        }
        return data;
    }

};
