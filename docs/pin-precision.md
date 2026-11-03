# Pin precision: how accurate is each pin?

Every site has a `geo_precision` (database enum, shown to visitors in the site card). The map never pretends a
city-centre pin is exact (rule 4 in `CLAUDE.md`). Pins drawn at city/province precision are shown faded on the map.

| `geo_precision` | Meaning | Shown to visitors (EN) | How a site gets it |
|---|---|---|---|
| `exact` | The building itself, from a source that gives coordinates | Exact location | Seed data or a visitor's pin, approved by the owner |
| `street` | The named place found by a place database, confirmed | Location accurate to the street | `mlm locate --apply` / `mlm replace --apply`: Amap (China), OpenStreetMap or Wikidata, when two sources agree or the name is specific and exact |
| `district` | Right city, partial name match; within a few km | Approximate location (district) | `mlm locate --approx`: an unambiguous partial name match inside the correct city |
| `city` | Only the city is known; pin sits at the city centre | Location not yet pinned; shown near the city centre | Default when a record names a city but no findable place |
| `province` | Only the province/region is known; pin at its centre | Location not yet pinned; shown at province centre | Default when a record names only a province |

## Where the evidence is
Every pin move writes a `location` claim (source, found name, coordinates, whether two sources agreed, precision) so
the pin can be audited. City and province centres come from the gazetteer or from `places.ensure_city`
(`regions.lat/lng`), not from a claim.

## Rules for changing a pin
- A pin only moves to a more precise level, never back. Only confident matches move to `street` (`locate.confident`);
  generic names ("the church", "mission school") never do (`locate.GENERIC`).
- Amap returns GCJ-02 coordinates; they are converted to WGS-84 before storing. Results that are only a city, district
  or development-zone centre are ignored, since they are no better than what we have.
- OpenHistoricalMap was evaluated and is not used (1 match across ~1,057 named China features).
- Visitors can correct a pin through the in-app edit card; the change goes live only after the owner approves it.

## Current numbers (public sites, 2026-11-03)
| Precision | Sites |
|---|---|
| street | 302 |
| district | 152 |
| city | 960 |
| province | 382 |
| **total** | **1,796** |

Street pins were 67 before the pin-expansion work. Most remaining city/province pins belong to records with only a
generic or historical name; improving them needs better source names or visitor corrections.

## Re-running
`maintenance.yml` tasks: `pins-dry-run` (report only) and `pins-apply` (writes). Locally:
`mlm locate [--apply] [--approx]`, `mlm replace [--apply --limit N]`, then `mlm refresh-site`.
