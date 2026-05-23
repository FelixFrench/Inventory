# Spike C — Dev Report

## 1. Screens Produced

| Screen | Description |
|--------|-------------|
| Screen 1 — No active session | Feed page at `/feed` with neutral banner and two full-width CTA buttons replacing the empty feed area. |
| Screen 2 — Active scan-in session | Green-tinted banner showing session type, item count, and End session button; feed rows with inline ± quantity controls and one unresolved "loading…" row. |
| Screen 3 — End session strip | Same as Screen 2 with feed content dimmed and an inline bottom strip containing summary text, Confirm, and Discard buttons. |
| Screen 4 — Discard confirmation modal | Centred overlay modal with title, body text, and "Yes, discard" / "Keep session" buttons over dimmed feed content. |
| Screen 5 — End session strip (lookups pending) | Same as Screen 3 but with Confirm button greyed out and showing "loading…", illustrating the blocked state while product lookups are still in progress. |

---

## 2. Design Decisions Implemented

All seven pre-agreed decisions (D1–D7) were implemented. No deviations.

| Decision | Status | Notes |
|----------|--------|-------|
| D1 — Feed page = session control page | ✓ Implemented | Single `/feed` surface shown across all four screens. |
| D2 — Status banner | ✓ Implemented | Thin banner sits between the nav and content on every screen; always visible. |
| D3 — Start buttons when idle | ✓ Implemented | Two full-width primary CTAs replace the empty feed area in Screen 1. |
| D4 — End session: inline bottom strip | ✓ Implemented | Strip anchors to the bottom of the feed content area; nav and banner remain undimmed. |
| D5 — Discard: centred overlay modal | ✓ Implemented | Centred via `display: flex; align-items: center` on the modal overlay; destructive left, safe right. |
| D6 — Quantity override controls | ✓ Implemented | Inline `− count +` controls on each feed row; delta displayed as a signed integer. |
| D7 — Unknown/loading products | ✓ Implemented | Italic muted (`#aaa`) "loading…" for both name and brand on the unresolved row (Screen 2 & 3). |

---

## 3. Open Questions

1. **Nav change required.** The mockup adds a "Feed" link to the nav bar (`Mode | Inventory | Low Stock | Feed`). This nav update is not present in the current codebase. Phase 1 must add the link and the `/feed` route.

2. **Scan-out colour usage.** The coral/orange palette (`#FAECE7 / #993C1D`) is defined in the spec and used for the "Start scan out" button in Screen 1, but no active scan-out session screen is included in this mockup. Phase 1 should confirm that an active scan-out banner mirrors Screen 2's layout using the coral palette, and that the `qty-count` text colour for scan-out deltas is `#993C1D` (not `#3B6D11`).

3. **Quantity controls at zero delta.** If the user taps − on a row already at a delta of 0 within a scan-in session, the resulting −1 delta may be confusing. The mockup does not address this. Phase 1 should decide whether the − button is disabled at 0, or whether negative deltas are permitted within a scan-in session (e.g. to correct a double-scan).

4. **Crash recovery surface.** If a session is active at unexpected shutdown and resumes at power-up, does the feed page need a visual indicator distinguishing a *recovered* session from a newly started one? The mockup does not model this state. The answer may affect the session model and banner copy in Phase 1.

5. **Banner count semantics: units vs. products.** The mockup uses "units" throughout (banner: "Scan in — 5 units"; strip: "Add 5 units to inventory?") where 5 is the sum of all session deltas across 3 distinct barcodes. An alternative is to show distinct product count in the banner (e.g. "Scan in — 3 products") and total units only in the strip. Phase 1 must settle the canonical count shown in the banner before implementing the session model.

---

## 4. Recommended Actions Before Phase 1

1. **Agree the nav update** — confirm the "Feed" link label, position in the nav bar, and route path (`/feed`) before Phase 1 touches the frontend skeleton.

2. **Add a scan-out active session screen** — the four screens here cover scan-in only. A single additional screen (or a revised Screen 2 in the coral palette) should be signed off before Phase 1 begins to confirm scan-out styling end-to-end.

3. **Define zero-delta behaviour** — decide whether negative adjustments are permitted in a scan-in session before the quantity controls are implemented. This affects both the backend session model and the frontend button state.

4. **Address crash recovery** — a design decision on the recovered-session indicator is needed before Phase 1 can fully spec the session state machine.

---

## 5. Files Created

| File | Description |
|------|-------------|
| `frontend/spike_c_mockup.html` | Static annotated HTML/CSS mockup — four phone-frame screens, no JavaScript. |
| `Spike_C_dev_report.md` | This file. |
