(function () {
  "use strict";

  var state = {
    services: [], barbers: [], dates: [],
    selected: {},            // service_id -> true
    barberId: null, date: null, time: "", endTime: "",
    slots: [], idemKey: null, submitting: false, seq: 0
  };

  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }
  function money(v) {
    return "₸" + Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function newKey() {
    if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
    return "k" + Date.now().toString(36) + Math.random().toString(36).slice(2, 12);
  }
  function selectedServices() {
    return state.services.filter(function (s) { return state.selected[s.id]; });
  }
  function selectedBarber() {
    return state.barbers.find(function (b) { return b.id === state.barberId; });
  }
  function selectedDate() {
    return state.dates.find(function (d) { return d.date === state.date; });
  }

  function api(url, options) {
    return fetch(url, options).then(function (res) {
      return res.json().catch(function () { return null; }).then(function (data) {
        if (!res.ok || (data && data.ok === false)) {
          var err = new Error((data && data.error) || ("Request failed (" + res.status + ")"));
          err.code = data && data.code;
          throw err;
        }
        return data;
      });
    });
  }

  // ---------- Rendering ----------
  function renderServices() {
    var box = $("service-list");
    box.innerHTML = "";
    state.services.forEach(function (s) {
      var b = el("button", "service-button");
      b.type = "button";
      if (s.description) b.title = s.description;
      b.setAttribute("aria-pressed", state.selected[s.id] ? "true" : "false");
      if (state.selected[s.id]) b.classList.add("selected");
      b.appendChild(el("strong", "", s.name));
      var meta = el("span", "service-meta");
      meta.appendChild(el("span", "", s.duration_minutes + " min"));
      meta.appendChild(el("span", "", money(s.price)));
      b.appendChild(meta);
      b.appendChild(el("span", "check", "✓"));
      b.addEventListener("click", function () {
        if (state.selected[s.id]) delete state.selected[s.id]; else state.selected[s.id] = true;
        state.time = ""; state.endTime = ""; state.idemKey = null;
        $("service-error").textContent = "";
        renderServices();
        updateSummary();
        refreshSlots(false);
      });
      box.appendChild(b);
    });

    var ov = $("services-overview");
    ov.innerHTML = "";
    state.services.forEach(function (s) {
      var li = el("li");
      li.appendChild(el("span", "", s.name));
      li.appendChild(el("span", "", s.duration_minutes + " min · " + money(s.price)));
      ov.appendChild(li);
    });
  }

  function renderBarbers() {
    var box = $("barber-list");
    box.innerHTML = "";
    state.barbers.forEach(function (br) {
      var b = el("button", "barber-button");
      b.type = "button";
      if (br.id === state.barberId) b.classList.add("selected");
      b.appendChild(el("span", "initials", br.initials));
      var t = el("span", "barber-text");
      t.appendChild(el("strong", "", br.full_name));
      t.appendChild(el("span", "", br.specialization || "Barber"));
      b.appendChild(t);
      b.appendChild(el("span", "check", "✓"));
      b.addEventListener("click", function () {
        state.barberId = br.id;
        state.time = ""; state.endTime = ""; state.idemKey = null;
        renderBarbers();
        updateSummary();
        refreshSlots(false);
      });
      box.appendChild(b);
    });
  }

  function renderDates() {
    var box = $("date-list");
    box.innerHTML = "";
    state.dates.forEach(function (d) {
      var b = el("button", "date-button");
      b.type = "button";
      b.title = d.label;
      if (d.date === state.date) b.classList.add("selected");
      b.appendChild(el("span", "day", d.dow));
      b.appendChild(el("span", "day-number", String(d.day)));
      b.addEventListener("click", function () {
        state.date = d.date;
        state.time = ""; state.endTime = ""; state.idemKey = null;
        renderDates();
        updateSummary();
        refreshSlots(false);
      });
      box.appendChild(b);
    });
  }

  function renderNote(text) {
    var box = $("time-list");
    box.innerHTML = "";
    box.appendChild(el("p", "state-note", text));
  }

  function renderSlots() {
    var box = $("time-list");
    box.innerHTML = "";
    state.slots.forEach(function (s) {
      var b = el("button", "time-button", s.time);
      b.type = "button";
      if (!s.available) {
        b.classList.add("booked");
        b.disabled = true;
        b.title = s.reason === "past" ? "Time has passed"
                : s.reason === "slot_conflict" ? "Already booked"
                : "Unavailable";
      } else if (s.time === state.time) {
        b.classList.add("selected");
      }
      b.addEventListener("click", function () {
        state.time = s.time;
        state.endTime = s.end_time;
        $("time-error").textContent = "";
        renderSlots();
        updateSummary();
      });
      box.appendChild(b);
    });
  }

  function updateSummary() {
    var list = $("summary-services");
    list.innerHTML = "";
    var chosen = selectedServices();
    var price = 0, minutes = 0;
    if (!chosen.length) {
      list.appendChild(el("li", "summary-empty", "No services selected"));
    }
    chosen.forEach(function (s) {
      price += Number(s.price);
      minutes += s.duration_minutes;
      var li = el("li");
      var line = el("div", "line");
      line.appendChild(el("span", "", s.name));
      line.appendChild(el("span", "", money(s.price)));
      li.appendChild(line);
      li.appendChild(el("div", "sub", s.duration_minutes + " min"));
      list.appendChild(li);
    });
    var br = selectedBarber(), d = selectedDate();
    $("summary-barber").textContent = br ? br.full_name : "—";
    $("summary-date").textContent = d ? d.label : "—";
    $("summary-time").textContent = state.time || "—";
    $("summary-end").textContent = state.time && state.endTime ? state.endTime : "—";
    $("summary-duration").textContent = minutes + " min";
    $("summary-total").textContent = money(price);
  }

  // ---------- Availability (always asks the server) ----------
  function refreshSlots(silent) {
    var seq = ++state.seq;
    var ids = selectedServices().map(function (s) { return s.id; });
    if (!ids.length || state.barberId === null || !state.date) {
      state.slots = [];
      renderNote("Select services to see available times.");
      return;
    }
    if (!silent) renderNote("Loading times…");
    var url = "/api/availability?barber_id=" + encodeURIComponent(state.barberId) +
              "&date=" + encodeURIComponent(state.date) +
              "&service_ids=" + ids.join(",");
    api(url).then(function (data) {
      if (seq !== state.seq) return;           // stale response
      state.slots = data.slots || [];
      var cur = state.slots.find(function (s) { return s.time === state.time && s.available; });
      if (!cur) { state.time = ""; state.endTime = ""; } else { state.endTime = cur.end_time; }
      if (!data.works_that_day) {
        renderNote("This barber does not work on the selected day.");
      } else if (!state.slots.some(function (s) { return s.available; })) {
        renderSlots();
        $("time-error").textContent = "No free times on this day. Try another date or barber.";
      } else {
        $("time-error").textContent = "";
        renderSlots();
      }
      updateSummary();
    }).catch(function (err) {
      if (seq !== state.seq || silent) return;
      renderNote("Could not load times: " + err.message);
    });
  }

  // ---------- Submit ----------
  function validate() {
    var ok = true;
    var first = $("first-name").value.trim();
    var last = $("last-name").value.trim();
    var digits = $("phone").value.replace(/\D/g, "");
    $("first-name-error").textContent = "";
    $("last-name-error").textContent = "";
    $("phone-error").textContent = "";
    $("service-error").textContent = "";
    $("time-error").textContent = "";
    $("form-error").textContent = "";
    if (!selectedServices().length) { $("service-error").textContent = "Select at least one service."; ok = false; }
    if (!state.time) { $("time-error").textContent = "Select an available time."; ok = false; }
    if (first.length < 2) { $("first-name-error").textContent = "Enter your first name (min. 2 characters)."; ok = false; }
    if (last.length < 2) { $("last-name-error").textContent = "Enter your last name (min. 2 characters)."; ok = false; }
    if (digits.length < 10) { $("phone-error").textContent = "Enter a valid phone number."; ok = false; }
    return ok;
  }

  function setBusy(busy) {
    state.submitting = busy;
    var btn = $("confirm-button");
    btn.disabled = busy;
    btn.textContent = busy ? "SAVING…" : "CONFIRM BOOKING";
  }

  function showConfirmation(b) {
    var box = $("message-text");
    box.innerHTML = "";
    box.appendChild(el("p", "", "Booking #" + b.booking_id + " · " + b.customer.first_name + " " + b.customer.last_name));
    box.appendChild(el("p", "", "Barber: " + b.barber.name));
    box.appendChild(el("p", "", b.date + ", " + b.start_time + " – " + b.end_time));
    b.services.forEach(function (s) {
      var line = el("div", "m-line");
      line.appendChild(el("span", "", s.name));
      line.appendChild(el("span", "", money(s.price)));
      box.appendChild(line);
    });
    var total = el("div", "m-line m-total");
    total.appendChild(el("span", "", "Total · " + b.total_duration_minutes + " min"));
    total.appendChild(el("span", "", money(b.total_price)));
    box.appendChild(total);
    $("message").classList.add("show");
  }

  function onSubmit(event) {
    event.preventDefault();
    if (state.submitting || !validate()) return;
    if (!state.idemKey) state.idemKey = newKey();
    setBusy(true);
    api("/api/book", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        first_name: $("first-name").value.trim(),
        last_name: $("last-name").value.trim(),
        phone: $("phone").value.trim(),
        barber_id: state.barberId,
        service_ids: selectedServices().map(function (s) { return s.id; }),
        date: state.date,
        time: state.time,
        idempotency_key: state.idemKey
      })
    }).then(function (data) {
      setBusy(false);
      state.idemKey = null;
      showConfirmation(data.booking);
      state.time = ""; state.endTime = "";
      refreshSlots(true);
      updateSummary();
    }).catch(function (err) {
      setBusy(false);
      $("form-error").textContent = err.message;
      if (err.code === "slot_conflict" || err.code === "time_in_past" ||
          err.code === "break_or_time_off" || err.code === "outside_working_hours") {
        state.time = ""; state.endTime = ""; state.idemKey = null;
        refreshSlots(false);
        updateSummary();
      }
    });
  }

  // ---------- Init ----------
  function init() {
    $("load-error").hidden = true;
    Promise.all([api("/api/config"), api("/api/services"), api("/api/barbers")])
      .then(function (r) {
        state.dates = r[0].dates;
        state.services = r[1].services;
        state.barbers = r[2].barbers;
        if (!state.date && state.dates.length) state.date = state.dates[0].date;
        if (state.barberId === null && state.barbers.length) state.barberId = state.barbers[0].id;
        renderServices(); renderBarbers(); renderDates();
        updateSummary();
        refreshSlots(false);
      })
      .catch(function (err) {
        $("load-error-text").textContent = "Could not load the booking data: " + err.message;
        $("load-error").hidden = false;
        $("service-list").innerHTML = "";
        $("barber-list").innerHTML = "";
      });
  }

  $("booking-form").addEventListener("submit", onSubmit);
  $("retry-button").addEventListener("click", init);
  $("close-button").addEventListener("click", function () {
    $("message").classList.remove("show");
    state.selected = {};
    ["first-name", "last-name", "phone"].forEach(function (id) { $(id).value = ""; });
    renderServices(); updateSummary(); refreshSlots(false);
  });

  // Keep times in sync with bookings made by other visitors.
  setInterval(function () {
    if (!document.hidden && !state.submitting) refreshSlots(true);
  }, 30000);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) refreshSlots(true);
  });

  init();
})();