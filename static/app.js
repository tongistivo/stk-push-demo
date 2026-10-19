(function () {
    "use strict";

    var POLL_INTERVAL_MS = 3000;
    var WAIT_LIMIT_MS = 120000;   // stop waiting after 2 minutes
    var MAX_POLL_FAILURES = 5;

    var form = document.getElementById("paymentForm");
    var amountInput = document.getElementById("amount");
    var phoneInput = document.getElementById("phone");
    var payButton = document.getElementById("payButton");
    var buttonText = document.getElementById("buttonText");

    var status = document.getElementById("status");
    var statusTitle = document.getElementById("statusTitle");
    var statusMessage = document.getElementById("statusMessage");
    var checkAgain = document.getElementById("checkAgain");

    var activeId = null;      // checkout ID we are waiting on
    var pollTimer = null;
    var waitStarted = 0;
    var pollFailures = 0;


    function showStatus(state, title, message) {
        status.classList.remove("hidden");
        status.setAttribute("data-state", state);
        statusTitle.textContent = title;      // textContent: never parsed as HTML
        statusMessage.textContent = message;
        checkAgain.classList.add("hidden");
    }

    function setBusy(busy) {
        payButton.disabled = busy;
        buttonText.textContent = busy ? "Please wait..." : "Pay Now";
    }

    function stopPolling() {
        if (pollTimer) {
            clearTimeout(pollTimer);
            pollTimer = null;
        }
    }

    function validPhone(raw) {
        var p = raw.replace(/[\s-]/g, "");
        if (p.charAt(0) === "+") { p = p.slice(1); }
        if (p.length === 10 && p.charAt(0) === "0") { p = "254" + p.slice(1); }
        return /^254[17]\d{8}$/.test(p);
    }


    form.addEventListener("submit", function (event) {
        event.preventDefault();
        submitPayment();
    });

    checkAgain.addEventListener("click", function () {
        if (activeId) {
            showStatus("pending", "Checking again", "Looking for the payment result...");
            beginPolling(activeId);
        }
    });


    async function submitPayment() {
        var amountText = amountInput.value.trim();
        var amount = Number(amountText);
        var phone = phoneInput.value.trim();

        if (!/^\d+$/.test(amountText) || !Number.isInteger(amount) || amount < 1) {
            showStatus("error", "Invalid amount", "Enter a whole-number amount of at least KSh 1.");
            return;
        }
        if (!validPhone(phone)) {
            showStatus("error", "Invalid phone number", "Enter a valid Safaricom number, e.g. 0712345678.");
            return;
        }

        stopPolling();
        activeId = null;
        setBusy(true);
        showStatus("pending", "Contacting M-Pesa", "Sending your payment request...");

        try {
            var response = await fetch("/api/pay", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ amount: amount, phone: phone })
            });
            var result = await response.json().catch(function () { return null; });

            if (response.ok && result && result.success && result.checkoutRequestId) {
                showStatus("pending", "Check your phone", result.message);
                beginPolling(result.checkoutRequestId);   // button stays disabled
            } else {
                var text = (result && result.message) || "The payment request could not be processed.";
                showStatus("error", "Request failed", text);
                setBusy(false);
            }
        } catch (error) {
            showStatus("error", "Connection failed", "Could not reach the server. Check your internet and try again.");
            setBusy(false);
        }
    }


    function beginPolling(checkoutId) {
        stopPolling();
        activeId = checkoutId;
        waitStarted = Date.now();
        pollFailures = 0;
        pollTimer = setTimeout(function () { poll(checkoutId); }, POLL_INTERVAL_MS);
    }

    async function poll(checkoutId) {
        if (checkoutId !== activeId) { return; }

        try {
            var response = await fetch(
                "/api/payment-status/" + encodeURIComponent(checkoutId),
                { cache: "no-store" }
            );
            var data = await response.json().catch(function () { return null; });

            if (response.status === 404) {
                showStatus("error", "Payment not found", "We could not find this payment. Please try again.");
                finish();
                return;
            }
            if (!response.ok || !data || !data.success) {
                throw new Error("bad status response");
            }

            pollFailures = 0;

            if (data.status === "SUCCESS") {
                var receipt = data.receipt ? " Receipt: " + data.receipt + "." : "";
                showStatus("success", "Payment successful", data.message + receipt);
                finish();
                return;
            }
            if (data.status === "CANCELLED") {
                showStatus("cancelled", "Payment cancelled", data.message);
                finish();
                return;
            }
            if (data.status === "FAILED") {
                showStatus("failed", "Payment failed", data.message);
                finish();
                return;
            }
            // PENDING: keep waiting
        } catch (error) {
            pollFailures += 1;
            if (pollFailures >= MAX_POLL_FAILURES) {
                showStatus(
                    "error",
                    "Connection problem",
                    "We can't check the payment right now. If you entered your PIN, check your M-Pesa messages before paying again."
                );
                finish();
                return;
            }
        }

        if (Date.now() - waitStarted > WAIT_LIMIT_MS) {
            showStatus(
                "timeout",
                "Still waiting for confirmation",
                "We have not received a result yet. If you already paid, check your M-Pesa messages. Please do not pay again until you are sure."
            );
            checkAgain.classList.remove("hidden");
            setBusy(false);
            stopPolling();
            return;
        }

        pollTimer = setTimeout(function () { poll(checkoutId); }, POLL_INTERVAL_MS);
    }

    function finish() {
        stopPolling();
        activeId = null;
        setBusy(false);
    }
})();
