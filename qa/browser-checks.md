# Browser verification — 21 October 2026

Run with `test_full_product.py` against real server subprocesses (isolated data dirs) in headless
Chrome 131 via Playwright, at 1440×1000 and 390×844. Result: **33 checks, 0 failed**
(`qa/full-run-summary.txt`). Screenshots in this folder come from that run.

Covered per viewport:
- Full journey: invalid amount rejected, instant settle, retry decision, receipt + copy ID, delayed → refund pending → new payment → original refund, offline/reconnect, CSV export, responsive widths 320–1440 with no page-level horizontal overflow, server stop (banner) and restart (banner cleared, totals preserved).
- Lost response: the server completes a payment, the browser response is dropped, the user resubmits → exactly one payment (this check **failed on the originally supplied build**; fixed by keeping an acknowledged request pinned while its dialog is open).
- Live updates: the page opens one `/api/events` stream; idle for 5 s causes ≤ 1 `/api/state` request (the old build polled every 1.5 s); a payment created by another client appears without reload; server kill shows the banner and restart clears it.
- Reliability agent: a delayed payment shows an explanation and an `Approve: credit` button; the payment stays `DEBITED` until the button is pressed, then settles, and exactly one `EXECUTED` audit record is written.

Not covered: real devices, other browsers, screen readers beyond role/name queries, real providers.
