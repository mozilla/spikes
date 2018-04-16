let isLoaded = false;

function loaded() {
    isLoaded = true;
}

function getParams() {
    let params = ["date", "channel", "product"].map(function(i) {
        let e = document.getElementById(i);
        return e.options[e.selectedIndex].value;
    });
    return params;
}

function setHref(date, channel, product) {
    location.search = "?date=" + date
                    + "&channel=" + channel
                    + "&product=" + product;
    location.reload(true);
}

function update() {
    let params = getParams();
    setHref(params[0], params[1], params[2]);
}

function toUTC(date) {
    return new Date(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate());
}

function checkDate() {
    if (isLoaded) {
        let today = toUTC(new Date());
        let e = document.getElementById("date");
        let date = new Date(e.options[0].value);
        if (date.getYear() != today.getYear() || date.getMonth() != today.getMonth() || date.getDay() != today.getDay()) {
            let newdate = today.toISOString().slice(0, 10);
            let params = getParams();
            setHref(newdate, params[1], params[2]);
            e.selectedIndex = 0;
        }
    }
}
