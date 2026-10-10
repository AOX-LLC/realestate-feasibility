# What the recorded runs cost

All figures are in US dollars at agent-core's packaged prices (the small tier is Haiku 4.5, the mid
tier Sonnet 5.5). The first table is read from the `llm_call` ledger after replaying day 1 and day 2
from the committed recordings on a fresh database; a replayed call carries the cost of the call
that was recorded. The second and third are the paid recording sessions.

## A day's run, by stage (replayed from the committed recordings)

| Day | Stage | Calls | Input tokens | Output tokens | Cost |
| --- | --- | ---: | ---: | ---: | ---: |
| 2026-10-01 | signals | 10 | 21,723 | 705 | $0.025248 |
| 2026-10-01 | narratives (5 computed candidates) | 5 | 11,754 | 2,884 | $0.052348 |
| 2026-10-02 | signals | 6 | 13,022 | 397 | $0.015007 |
| 2026-10-02 | narratives | 2 | 4,761 | 2,560 | $0.035122 |
| | **Both days** | 23 | 51,260 | 6,546 | $0.127725 |

Before the narrative re-record the same replay read 24 calls and $0.163707 (day 1's narratives took
6 calls, one of them a repair). Day 2 calls only for what changed: six remarks day 1 never sent, and
the narratives of the two candidates whose inputs changed. A same-day re-run makes no call. Three
of the seven narrative calls (one on day 1, two on day 2) replay recordings made before the
effort change, which is why day 2's two narratives still show about 1,280 output tokens each.

## The narrative re-record (2026-10-10)

| Step | Calls | Cost |
| --- | ---: | ---: |
| Narrative eval, 13 cases, effort `low`: 30,133 input and 7,331 output tokens | 13 | $0.133576 |

Every call finished, none needed a repair, and none was cut off, so the ledger's figure is the
recorded usage priced, not a reservation. It is agent-core's price calculation; the console's own
spend figure was not read.

## The Phase 4e recording session (2026-10-09)

| Step | Calls | Cost |
| --- | ---: | ---: |
| Extraction eval, dev and holdout | 56 | $0.139176 |
| Day 1 (10 signals, 5 narratives) | 15 | $0.103806 |
| Day 2 (6 signals, 2 narratives) | 8 | $0.050129 |
| Narrative eval, 13 cases (9 scored, 4 cut off) and one repair | 14 (cost known for 10) | $0.153672 |
| Ledger total, calls with a known cost | 89 | $0.446783 |
| Four narrative calls cut off at `max_tokens` (cost unknown to the ledger; it counts the $0.05 reservation each) | 4 | $0.200000 as counted |

The four cut-off calls each ended at 1,500 output tokens, the mid tier's `max_tokens`. Their
recorded usage prices them at about $0.0789 together, so that session's true cost is about $0.5257
and the ledger, which never under-counts, shows $0.6468. The narrative eval of that session is the
one recorded again above; its recordings are replaced, and its extraction and day-run recordings
that were not overwritten are the ones in use.

## A live day, narratives only (projection)

Live data has no remarks, so a live day makes narrative calls only, for computed candidates whose
inputs changed. At effort `low` the 13 eval calls averaged $0.0103 a narrative call ($0.133576,
about 2,300 input and 560 output tokens, no repair); the seven calls of the two replayed days
averaged $0.0125 because three of them come from the earlier recordings. A first day with 5 computed
candidates is about $0.05 to $0.06; a day with 2 changed candidates is about $0.02 to $0.03. These
are projections from synthetic facts sheets and two snapshot days, not measurements of live data,
and a live reply could still be cut off or need a repair, which this recording did not test.
