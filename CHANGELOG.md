# Changelog

## 0.2.1 — 2026-10-16

- Bind Nature readers to the requested article ID and APS readers to the requested DOI, excluding other retained readers on the same publisher domain.
- Observe a successful response following a temporary HTTP 403 without submitting another PDF navigation.
- Add fontTools for PDF font decoding.
- Validate a seven-paper download exercise across JMPS, PRL, Nature Physics, Nature, and Nature Communications; revalidate all seven through cache.
- Pass 31 regression tests, including reader identity binding and recovery from a temporary denial response.

## 0.2.0 — 2026-10-16

- Provide a portable JSON CLI for DOI, title, URL, and batch retrieval.
- Add configurable CSTCloud, CARSI, publisher, WebVPN, EZProxy, and API profiles.
- Capture original browser responses and checkpoint native downloads; reuse a dedicated system browser after manual login.
- Validate PDF identity, parse all pages, attach SHA-256 receipts, and persist cache and publisher cooldowns.

The real download exercises used Windows and an existing browser session. Linux deployment and all real-account institutional login flows have not yet been accepted end to end.
