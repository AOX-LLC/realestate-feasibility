# Pro-forma assumptions (Dallas pack)

Every value in `[cost_assumptions]` of `src/feasibility/markets/packs/dallas.toml` is **illustrative**: a labelled default, not a quote and not a builder's actuals (`status = "illustrative"`). Sources were read on **2026-10-02**. Most are aggregator and lender-blog pages: adequate for labelled defaults, not for quoting to a client. Only the zoning rows and the City of Dallas tax component were read from official pages; the other tax components came from search summaries, and demolition rests on aggregator pages. Everything else needs a builder's actuals before real use.

## Values

| Value | Choice | Basis |
| --- | --- | --- |
| `construction.hard_cost_per_sqft` | 190 | 2026 Dallas guide built on RSMeans: basic $104, standard $122, premium $165, luxury $214 per sqft ([costtobuildhouse.com/dallas-tx](https://www.costtobuildhouse.com/dallas-tx), updated May 2026). Another 2026 aggregate: $130-250 new residential, mid-range custom $190-280 ([oneestimate.ai/en/texas/dallas](https://oneestimate.ai/en/texas/dallas)). 190 is the bottom of mid-range custom and 15% above the RSMeans premium tier; inner-Dallas spec homes finish at or above that, so it is probably **low**. |
| `construction.soft_cost_pct` | 12 of hard cost | Soft costs "roughly 10-15% of the total budget" (same Dallas cost guides); 12% of hard is about 10.7% of hard plus soft. Dallas residential permit costs $2,000-5,500 (costtobuildhouse.com). |
| `construction.contingency_pct` | 5 | **No public source.** A convention for new construction; to be confirmed or replaced. |
| `demolition` | 2,500 flat + 8 per sqft; fallback 1,500 sqft | A house costs $8,700-18,200 to demolish in Dallas ([homeblue.com](https://www.homeblue.com/demolition/dallas-tx-cost-to-demolish-a-house.htm)); $6-15 per sqft basic ([contractorplus.app](https://contractorplus.app/resources/construction-costs/demolish-house/dallas-tx)); permits $500-1,500. 2,500 + 8 x 1,150 = 11,700 sits inside the range. |
| `financing.loan_to_cost_pct` | 80 | Houston-market 2026 lender quotes: 75-90% LTC for experienced borrowers, 70-80% for newer ones ([harbertgroup.com Texas hard money 2026](https://www.harbertgroup.com/blog/1051/Texas+Hard+Money+Loans+2026%3A+Houston+Fix-and-flip+Lender+Rates%2C+Terms%2C+And+Honest+Roi)). |
| `financing.rate_pct` | 10.0 | Same source: 9.0-13.5% interest-only; 9.0-10.5% for borrowers with 5+ completed flips. National lenders advertise 7.25-7.45% floors ([Kiavi, Lima One via theclose.com](https://theclose.com/best-fix-and-flip-loans)); those are best case, not Dallas spec construction. |
| `financing.points_pct` | 2.0 | 1.5-3 points at origination (same). |
| `financing.draw_count`, `draw_fee` | 5, 200 | 4-6 milestone draws, $150-250 per inspection (same). |
| `holding.hold_months`, `construction.build_share_pct` | 9, 70 | A 6-12 month spec build, midpoint; typical loan terms are 6-18 months (same). The 70/30 build/marketing split is **illustrative and unsourced**. |
| `holding.property_tax_rate_pct` | 2.226885 | City of Dallas $0.698800 per $100 ([dallasecodev.org/614/Tax-Rate](https://dallasecodev.org/614/Tax-Rate), FY2025, opened); Dallas County $0.215500 for TY2025 ([dallascounty.org notice](https://www.dallascounty.org/Assets/uploads/docs/budget/tax-rate-info/Notice-of-Adopted-Tax-Rate-with-Vote.pdf), seen in search results; the PDF itself returned 404 when fetched); Dallas ISD $0.993835, Dallas College $0.106575, Parkland $0.212000 (search-result summary, not independently opened). **Unresolved discrepancy:** these five components add up to 2.226710, but the pack carries 2.226885 (a difference of 0.000175 percentage points, about $0.55 a year per $300k of price). One component was probably mistranscribed or the sum was; the sources have to be re-read to say which. The value is left as is so the reference workbook and its worked figures stay consistent with the pack until that is settled. **Applies to Dallas ISD addresses in the city of Dallas only**; some buy-box zips touch other districts (Richardson ISD). |
| `holding.insurance_pct_of_hard_cost_per_year` | 1.5 | Builder's-risk premiums run 1-5% of project cost per policy ([Embroker](https://www.embroker.com/blog/builders-risk-insurance-cost/), [Insureon](https://www.insureon.com/small-business-insurance/builders-risk/cost)); a vacant Dallas dwelling policy is $4,000-6,200 a year (Insurify, latentinsure), which 1.5% of a $600k hard cost (about $9k) covers with room. |
| `selling.commission_pct`, `selling.closing_pct` | 5.0, 1.0 | Texas seller commission 5-6% and owner's title policy about 0.5% of price plus escrow, recording and survey ([neuhausre.com seller costs 2026](https://neuhausre.com/seller-closing-costs-texas-2026/), [harbertgroup.com closing costs](https://www.harbertgroup.com/blog/926/Closing+Costs+In+Texas+2026%3A+Real+Numbers+For+Buyers+And+Sellers)). TDI cut basic title premiums 6.2% effective 2026-03-01 (Order 2025-9697, per harbertgroup.com). That article's own premium formula does not reproduce its own table ($300k: formula 1,768 vs table 1,737), so no formula is used. |
| `acquisition.closing_pct` | 1.0 | Same closing-cost sources; illustrative. |
| `sizing.rules` R-7.5(A), R-5(A), R-10(A) | coverage 45%, 2 stories, living share 55% | City of Dallas district pages: maximum lot coverage 45% for residential structures, maximum height 30 ft, no floor-area ratio, for R-7.5(A) ([page](https://dallascityhall.com/departments/sustainabledevelopment/planning/Pages/R75A.aspx)), R-5(A) ([page](https://dallascityhall.com/departments/sustainabledevelopment/planning/Pages/R5A.aspx)) and R-10(A) ([page](https://dallascityhall.com/departments/sustainabledevelopment/planning/Pages/R10A.aspx)). Two stories is inferred from the 30 ft limit (not stated). `living_share_pct` (garage, porch, stairs and walls are covered footprint but not finished living area) and the 1,500 / 3,500 sqft clamp are **illustrative and unsourced**. Setbacks and platted building lines are not modelled; they can shrink a narrow lot's footprint. |
| `sizing.default` | coverage 40%, 2 stories, living share 50% | Deliberately below the R districts: an unknown zoning (CD, PD) is assumed to be tighter. Unsourced. |
| `target.margin_pct` | 15 | Policy, not a market cost: profit as a share of ARV. |
| `arv`, `sensitivity` | min 3 comps of at least 600 sqft, no new-build premium, estimates up to 30 days old; grid of ARV -10..+10%, hard cost -10..+20%, hold 6/9/12 months | Engine settings, not market facts. |

## Reference spreadsheet

`docs/proforma-reference.xlsx` is an independent check on the pro-forma formulas, built from the pro-forma formulas alone (not from any engine code) and recalculated so its cached values are present. The formulas, not the spreadsheet's numbers, are the authority; the worked figures in `tests/test_proforma_reference.py` were hand-computed from them. The engine that implements the formulas ships in a later change.

Recalculation runs in a throwaway container; nothing is installed on the host. Image, resolved on 2026-10-02:

```
ubuntu:24.04 -> ubuntu@sha256:a853f94d226358a79c740cfc7bce0c289748f3fe3488d921d038ccd752c61b60
```

Exact command, run from the directory holding the un-recalculated `proforma-reference.xlsx` (`$SCRATCH`); the result in `$SCRATCH/out/` replaces the source file:

```
mkdir -p "$SCRATCH/out" && docker run --rm -v "$SCRATCH":/w -w /w ubuntu@sha256:a853f94d226358a79c740cfc7bce0c289748f3fe3488d921d038ccd752c61b60 sh -c 'apt-get update -qq && apt-get install -y -qq --no-install-recommends libreoffice-calc-nogui >/dev/null && soffice --headless --convert-to xlsx --outdir /w/out /w/proforma-reference.xlsx && chown -R 1000:1000 /w/out'
```

The committed workbook can be fed straight back through this command to refresh its cached values. About 600 MB is pulled once and discarded with the container (`--rm`). The package versions installed are whatever Ubuntu 24.04's archive serves on the day, so a rebuild months later may differ in the LibreOffice patch version.
