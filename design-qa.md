# Design QA

**Source visual truth**

- Path: `/tmp/mdesk-source-preview/index.html.png`
- Source page: pre-change `site/index.html`
- Pixel dimensions: 1400 × 1400
- CSS viewport: Quick Look desktop HTML preview, approximately 1400 px wide
- Density normalization: source and implementation captured by the same renderer at the same output size
- State: empty personal-plan state, data through 2026-08-19

**Implementation evidence**

- Home: `/tmp/mdesk-final-preview/index.html.png`
- Signals: `/tmp/mdesk-final-preview/signals.html.png`
- Plans: `/tmp/mdesk-final-preview/plans.html.png`
- Role transfer: `/tmp/mdesk-final-preview/role-transfer.html.png`
- Side-by-side comparison: `/tmp/mdesk-qa-preview/mdesk-qa-compare.html.png`
- Pixel dimensions: 1400 × 1400 for the four full-page previews; 1600 × 1600 for the comparison board
- State: empty personal-plan state; eight current market signals

**Findings**

- No remaining P0/P1/P2 visual findings.
- Information hierarchy now matches the product definition: market triggers and personal plans are primary; data validation is a secondary confidence layer.
- The existing typeface, color tokens, card radius, table density and restrained visual language were preserved.
- All four new pages use the same spacing rhythm and component vocabulary as the original site.
- Copy consistently distinguishes events, market signals, user-authored plans and data-confidence warnings.
- No new image assets were needed; this is a dense data application and the source design used no raster imagery.

**Comparison history**

1. First implementation pass
   - P2: the full data-backend navigation wrapped onto a second line and made the global header too tall.
   - Fix: collapsed the backend to one global entry and moved its six links into a secondary navigation visible only inside backend pages.
   - P2: the plan quote preview was blank before JavaScript execution.
   - Fix: added a server-rendered first-quote fallback; JavaScript still updates it after symbol selection.
2. Post-fix pass
   - Evidence: `/tmp/mdesk-qa-preview/mdesk-qa-compare.html.png` and `/tmp/mdesk-final-preview/plans.html.png`.
   - Result: navigation stays on one line at desktop width; the plan form has a complete initial state; no actionable visual mismatch remains.

**Required fidelity surfaces**

- Fonts and typography: preserved the original system font stack, weights, numeric alignment and hierarchy.
- Spacing and layout rhythm: preserved the 1180 px content width, 24 px gutters, 10 px radii and table/card rhythm; new action rows and forms follow the same density.
- Colors and visual tokens: reused the existing blue, red/green market convention, warning yellow, surface and border variables.
- Image quality and asset fidelity: not applicable; neither the source nor the implementation relies on visible raster imagery.
- Copy and content: changed from data-operations language to user-facing event, signal, plan and action language without inventing holdings or client data.

**Functional checks**

- Static generation completed: 55 HTML pages.
- All inline scripts on `site/index.html` and `site/plans.html` parse successfully.
- Python modules compile successfully.
- Generated links were checked against local targets; the only non-static href is the intentional JavaScript plan link template.
- Signal data check: 8 active basis signals, 0 single-day jumps, 0 volume spikes, 17 separate data-confidence notices for the current snapshot.
- Browser click automation was unavailable because the in-app browser blocks local `file://` pages; the form states and JavaScript paths were therefore checked through rendered previews, static target checks and script parsing.

**Follow-up polish**

- A future pass can make the 60-trading-day baseline configurable instead of fixed.
- Real browser testing should be repeated after the site is served or published, especially for localStorage persistence and the mobile breakpoint.

**final result: passed**
