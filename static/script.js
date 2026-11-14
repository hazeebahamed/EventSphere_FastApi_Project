
/* =========================================
   EventSphere - Main JavaScript File
   ========================================= */

document.addEventListener("DOMContentLoaded", function () {

    /* =====================================
       Add Ticket Categories
       ===================================== */

    const addCategoryButton = document.getElementById(
        "add-category"
    );

    const ticketRows = document.getElementById(
        "ticket-rows"
    );

    if (addCategoryButton && ticketRows) {

        addCategoryButton.addEventListener("click", function () {

            const row = document.createElement("div");

            row.className =
                "ticket-row border rounded p-3 mb-3";

            row.innerHTML = `

                <label class="form-label">
                    Ticket Name
                </label>

                <input
                    type="text"
                    name="ticket_name"
                    class="form-control mb-3"
                    placeholder="VIP or General"
                    required>

                <label class="form-label">
                    Ticket Price
                </label>

                <input
                    type="number"
                    name="ticket_price"
                    class="form-control mb-3"
                    min="0"
                    step="0.01"
                    required>

                <label class="form-label">
                    Ticket Quantity
                </label>

                <input
                    type="number"
                    name="ticket_quantity"
                    class="form-control mb-3"
                    min="1"
                    required>

                <button
                    type="button"
                    class="btn btn-danger btn-sm remove-category">

                    Remove Category

                </button>

            `;

            ticketRows.appendChild(row);

        });


        /* Remove Ticket Category */

        ticketRows.addEventListener("click", function (event) {

            const button = event.target.closest(
                ".remove-category"
            );

            if (button) {

                const row = button.closest(".ticket-row");

                row.remove();

            }

        });

    }


    /* =====================================
       Local Date Formatting
       ===================================== */

    function formatLocalDate(date) {

        const offset = date.getTimezoneOffset() * 60000;

        return new Date(date.getTime() - offset)
            .toISOString()
            .slice(0, 16);

    }


    /* =====================================
       Event Date and Time Validation
       ===================================== */

    document.querySelectorAll("form[data-dates]")
        .forEach(function (form) {

            const startInput = form.querySelector(
                '[name="start_time"]'
            );

            const endInput = form.querySelector(
                '[name="end_time"]'
            );

            if (!startInput || !endInput) {

                return;

            }

            const now = formatLocalDate(new Date());

            startInput.min = now;

            endInput.min = now;


            /* Update Minimum End Time */

            startInput.addEventListener("change", function () {

                endInput.min = startInput.value;

                if (
                    endInput.value &&
                    endInput.value <= startInput.value
                ) {

                    endInput.value = "";

                }

            });


            /* Validate Before Submission */

            form.addEventListener("submit", function (event) {

                const start = new Date(startInput.value);

                const end = new Date(endInput.value);

                if (
                    !Number.isFinite(start.getTime()) ||
                    !Number.isFinite(end.getTime())
                ) {

                    event.preventDefault();

                    alert("Please enter valid dates and times.");

                    return;

                }

                if (start <= new Date()) {

                    event.preventDefault();

                    alert("Start time must be in the future.");

                    return;

                }

                if (end <= start) {

                    event.preventDefault();

                    alert("End time must be after start time.");

                    return;

                }

            });


            /* Convert Local Time to UTC */

            form.addEventListener("formdata", function (event) {

                const start = new Date(startInput.value);

                const end = new Date(endInput.value);

                if (
                    Number.isFinite(start.getTime()) &&
                    Number.isFinite(end.getTime())
                ) {

                    event.formData.set(
                        "start_time",
                        start.toISOString()
                    );

                    event.formData.set(
                        "end_time",
                        end.toISOString()
                    );

                }

            });

        });


    /* =====================================
       Event Cancellation Confirmation
       ===================================== */

    document.querySelectorAll("form")
        .forEach(function (form) {

            const action = form.getAttribute("action") || "";

            if (
                action.startsWith("/admin/events/") &&
                action.endsWith("/cancel")
            ) {

                form.addEventListener("submit", function (event) {

                    const confirmed = confirm(

                        "Are you sure you want to cancel this event? " +
                        "All confirmed bookings will be refunded."

                    );

                    if (!confirmed) {

                        event.preventDefault();

                    }

                });

            }

        });


    /* =====================================
       Prevent Duplicate Form Submission
       ===================================== */

    document.querySelectorAll("form")
        .forEach(function (form) {

            if (form.method.toLowerCase() !== "post") {

                return;

            }

            form.addEventListener("submit", function (event) {

                if (event.defaultPrevented) {

                    return;

                }

                if (form.dataset.submitting === "true") {

                    event.preventDefault();

                    return;

                }

                form.dataset.submitting = "true";

                setTimeout(function () {

                    form.dataset.submitting = "false";

                }, 3000);

            });

        });


    /* =====================================
       Restore Forms After Back Navigation
       ===================================== */

    window.addEventListener("pageshow", function () {

        document.querySelectorAll("form")
            .forEach(function (form) {

                form.dataset.submitting = "false";

            });

    });

});
