# Historical bid/ask fill sensitivity — ESTIMATE

Paired valid rows only. Recorded history remains unchanged.

| Account | Included | Excluded | Recorded PNL | Estimated PNL | Change |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 47 | 3 | $39,744.87 | $1,744.18 | $-38,000.69 |
| 4 | 20 | 0 | $-18,927.90 | $-31,161.34 | $-12,233.44 |
| 5 | 25 | 1 | $468.30 | $-443.35 | $-911.65 |
| TOTAL | 92 | 4 | $21,285.27 | $-29,860.51 | $-51,145.78 |

Separate sensitivity retaining recorded cash expiry proceeds (entry still crosses ask): total estimated PNL $-29,791.47.

## Exclusions

{"open_position": 4}

## ASSUMPTIONS

- Contract counts, selected contracts and exit times stay recorded; no sizing or stop replay.
- Entry fill is recorded ask; exit midpoint is recorded exit_premium.
- Exit relative spread equals (entry_ask-entry_bid)/entry_midpoint; estimated bid is exit midpoint times (1-relative spread/2).
- Entry fees are cost_basis minus recorded entry premium times 100 times contracts; exit fees are recorded exit premium times 100 times contracts minus exit_value.
- Primary estimates apply the same exit spread haircut to expiry rows, as required by the design; recorded cash settlement proceeds are a separate sensitivity.
- Only valid closed legacy rows enter the paired comparison; excluded rows do not contribute either PNL total.
- Already bid_ask rows are excluded to avoid applying a spread haircut twice.
