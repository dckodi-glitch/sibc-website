"use strict";
document.documentElement.classList.add("js");

const menu = document.querySelector(".menu-toggle");
const nav = document.getElementById("primary-nav");
menu?.addEventListener("click", () => {
  const expanded = menu.getAttribute("aria-expanded") !== "true";
  menu.setAttribute("aria-expanded", String(expanded));
  nav.classList.toggle("is-open", expanded);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && menu?.getAttribute("aria-expanded") === "true") {
    menu.setAttribute("aria-expanded", "false");
    nav.classList.remove("is-open");
    menu.focus();
  }
});

document.querySelectorAll("[data-calendar]").forEach((button) => {
  button.addEventListener("click", () => {
    const frame = document.createElement("iframe");
    frame.src = button.dataset.calendar;
    frame.title = "Existing SIBC Google Calendar, America/Los_Angeles";
    frame.loading = "lazy";
    button.parentElement.querySelector(".calendar-frame").append(frame);
    button.hidden = true;
  }, { once: true });
});

const form = document.getElementById("donation-form");
if (form) {
  const amount = form.elements.amount;
  const terms = document.getElementById("monthly-terms");
  const consent = form.elements.monthly_consent;
  const summary = document.getElementById("donation-summary");
  const error = document.getElementById("donation-error");
  const submit = form.querySelector("button[type=submit]");
  const submitLabel = submit.textContent;
  const updateSummary = () => {
    const monthly = form.elements.frequency.value === "monthly";
    terms.hidden = !monthly;
    consent.required = monthly;
    const valid = /^\d{1,7}(?:\.\d{1,2})?$/.test(amount.value);
    summary.textContent = valid
      ? `${new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(Number(amount.value))} USD · ${monthly ? "every month" : "one-time"}`
      : "Enter an amount to review your gift.";
    error.hidden = true;
  };
  form.querySelectorAll("[name=preset]").forEach((input) => {
    input.addEventListener("change", () => {
      if (input.value === "custom") {
        amount.value = "";
        amount.focus();
      } else {
        amount.value = input.value;
      }
      updateSummary();
    });
  });
  amount.addEventListener("input", () => {
    form.querySelector("[name=preset][value=custom]").checked = true;
    updateSummary();
  });
  form.querySelectorAll("[name=frequency]").forEach((input) => input.addEventListener("change", updateSummary));
  updateSummary();
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (form.dataset.enabled !== "true" || submit.disabled) return;
    submit.disabled = true;
    submit.textContent = "Opening Stripe test checkout…";
    form.setAttribute("aria-busy", "true");
    error.hidden = true;
    try {
      const response = await fetch(form.action, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": form.elements.csrf.value },
        body: JSON.stringify({
          amount: amount.value,
          frequency: form.elements.frequency.value,
          monthly_consent: consent.checked,
          attempt_id: form.elements.attempt_id.value,
        }),
      });
      const data = await response.json();
      if (!response.ok || !data.url) {
        error.textContent = data.error || "Checkout could not start. Please try again later or use PayPal.";
        error.hidden = false;
        error.focus();
        return;
      }
      const destination = new URL(data.url);
      if (destination.protocol !== "https:" || destination.hostname !== "checkout.stripe.com") {
        throw new Error("Unverified checkout destination");
      }
      window.location.assign(destination.href);
    } catch {
      error.textContent = "We could not confirm that checkout started. Check before retrying if you already completed a payment, or contact SIBC.";
      error.hidden = false;
      error.focus();
    } finally {
      submit.disabled = false;
      submit.textContent = submitLabel;
      form.removeAttribute("aria-busy");
    }
  });
}
