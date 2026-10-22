from __future__ import annotations

import argparse

# find (+ per-city discovery) → parse (every place on every unread page) → translate → resolve (entities, units, pins) → verify → audit.
# find is the only stage that knows the unit; parse and resolve work on the whole pool. The old names still work.
STAGES = ["find", "parse", "translate", "resolve", "verify", "audit"]
ALIASES = {"crawl": "find", "extract": "parse", "load": "resolve"}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="mlm", description="Missionary Legacy Map pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="Run pipeline stages for a coverage unit (or a raw sources.yaml region)")
    run.add_argument("--unit", default=None, help="Coverage unit id, e.g. CN-ZJ (sets region, subregions, unit tag)")
    run.add_argument("--region", default=None, help="sources.yaml region key (default: the unit's region)")
    run.add_argument("--stage", choices=STAGES + list(ALIASES) + ["all"], default="all")
    run.add_argument("--no-network-geocode", action="store_true")
    run.add_argument("--audit-sample", type=int, default=10)
    run.add_argument("--max-pages", type=int, default=None, help="parse at most this many unread pages (default MLM_PARSE_MAX_PAGES, 200)")

    seed = sub.add_parser("seed", help="Publish the editorial seed (data/seed) to Supabase")
    seed.add_argument("--region", default=None, help="One seed file (e.g. hangzhou); default: all files")
    seed.add_argument("--sql", action="store_true", help="Print SQL instead of writing via API")

    sub.add_parser("submissions", help="Turn new visitor submissions into GitHub issues (no model calls)")
    apl = sub.add_parser("apply", help="Apply the approved proposal in a GitHub issue")
    apl.add_argument("--issue", type=int, required=True)

    dec = sub.add_parser("decisions", help="Open needs-decision issues for unresolved audit disagreements (no model)")
    dec.add_argument("action", choices=["sync", "rejected", "reconsider"],
                     help="sync: audit disagreements → issues; rejected: refresh the rejected-records review issue(s); "
                          "reconsider: open decision issues for boxes ticked in a review issue")
    dec.add_argument("--unit", default=None, help="rejected: one unit (default: every started unit)")
    dec.add_argument("--issue", type=int, default=None, help="reconsider: the rejected-review issue")
    dcd = sub.add_parser("decide", help="Apply the owner's decision label on a needs-decision issue")
    dcd.add_argument("--issue", type=int, required=True)

    rg = sub.add_parser("regeocode", help="Re-place a unit's centre-pinned sites in their cities (no model)")
    rg.add_argument("--unit", required=True)
    lc = sub.add_parser("locate", help="Find real pins for city-centre sites in OSM / Wikidata / Amap (dry run unless --apply)")
    lc.add_argument("--apply", action="store_true")
    lc.add_argument("--limit", type=int, default=None)
    pl = sub.add_parser("pool", help="The resource pool: what has been fetched and extracted (no model)")
    pl.add_argument("action", choices=["status", "backfill"])
    sub.add_parser("refresh-site", help="Tell the live map to reload its data now (after publishing)")
    sub.add_parser("actions-usage", help="Approximate GitHub Actions minutes over 30 days, per workflow (no model)")
    cp = sub.add_parser("cache", help="Model-result cache maintenance (no model calls)")
    cp.add_argument("action", choices=["prune"])
    cp.add_argument("--days", type=int, default=180)
    rc = sub.add_parser("verify-recheck", help="Re-verify claims rejected only for an excerpt the current matcher accepts")
    rc.add_argument("--dry-run", action="store_true")
    dd = sub.add_parser("dedupe", help="Remove exact duplicate events and claims (one-off cleanup, no model)")
    dd.add_argument("--dry-run", action="store_true")
    cov = sub.add_parser("coverage", help="Coverage tracking")
    cov.add_argument("action", choices=["sync", "status", "next", "publish", "report", "sweep"])
    cov.add_argument("--unit", default=None)

    args = ap.parse_args(argv)

    if args.cmd == "seed":
        from . import seed as seed_mod
        if args.sql:
            print(seed_mod.to_sql(args.region))
        else:
            from .config import notice
            from .store import Store
            notice(f"seed: {seed_mod.load_via_api(Store(), args.region)}")
        return
    if args.cmd == "regeocode":
        from . import regeocode
        from .store import Store
        regeocode.run(Store(), args.unit)
        return
    if args.cmd == "locate":
        from . import locate
        from .store import Store
        locate.run(Store(), apply=args.apply, limit=args.limit)
        return
    if args.cmd == "pool":
        from . import pool as pool_mod
        from .store import Store
        if args.action == "backfill":
            pool_mod.backfill(Store())
        print(pool_mod.Pool(Store()).summary())
        return
    if args.cmd == "refresh-site":
        from .site import refresh
        from .store import Store
        refresh(Store())
        return
    if args.cmd == "actions-usage":
        from . import actions_usage
        actions_usage.report()
        return
    if args.cmd == "cache":
        from . import llm
        from .store import Store
        llm.prune(Store(), args.days)
        return
    if args.cmd == "verify-recheck":
        from . import verify
        from .store import Store
        verify.recheck_excerpts(Store(), dry_run=args.dry_run)
        return
    if args.cmd == "dedupe":
        from . import dedupe
        from .store import Store
        dedupe.run(Store(), dry_run=args.dry_run)
        return
    if args.cmd == "submissions":
        from . import submissions
        submissions.sync()
        return
    if args.cmd == "apply":
        from . import apply
        apply.run(args.issue)
        return
    if args.cmd in ("decisions", "decide"):
        from . import decisions
        from .store import Store
        if args.cmd == "decisions" and args.action == "rejected":
            from . import rejected
            if args.unit:
                rejected.refresh(Store(), args.unit)
            else:
                rejected.refresh_active(Store())
        elif args.cmd == "decisions" and args.action == "reconsider":
            from . import rejected
            if not args.issue:
                raise SystemExit("--issue is required")
            rejected.reconsider(Store(), args.issue)
        elif args.cmd == "decisions":
            decisions.sync(Store())
        else:
            open("/tmp/decide-summary.txt", "w").write(decisions.decide(Store(), args.issue))
        return
    if args.cmd == "coverage":
        from . import coverage
        if args.action == "sync":
            coverage.sync_issues()
        elif args.action == "next":
            u = coverage.active_unit()
            print(u["id"] if u else "")
        else:
            from .store import Store
            store = Store()
            if args.action == "status":
                print(coverage.status(store))
            elif args.action == "sweep":
                coverage.sweep_done(store)
            elif args.action == "publish":
                coverage.publish(store, args.unit)
            elif args.action == "report":
                print(coverage.report(store, args.unit))
        return

    unit = None
    region = args.region
    if args.unit:
        from . import coverage
        u = coverage.unit(args.unit)
        unit, region = u["id"], region or u["region"]
    region = region or "zhejiang"

    coverage_unit = None
    if args.unit:
        from . import coverage as cov
        coverage_unit = cov.unit(args.unit)
    stages = STAGES if args.stage == "all" else [ALIASES.get(args.stage, args.stage)]
    for stage in stages:
        print(f"=== {stage} ({unit or region}) ===")
        if stage == "find":
            from . import crawl
            crawl.run(region, discover_for=coverage_unit)
        elif stage == "parse":
            from . import extract
            extract.run(_pool(), limit=args.max_pages, prefer=region)
        elif stage == "translate":
            from . import translate
            translate.run(_pool())
        elif stage == "resolve":
            from . import load
            load.resolve_pending(use_network=not args.no_network_geocode)
        elif stage == "verify":
            from . import verify
            from .store import Store
            verify.run(Store())
        elif stage == "audit" and unit:
            from . import audit
            from . import coverage as cov_mod
            from .store import Store
            audit.run(Store(), unit, cov_mod.regions_of(cov_mod.unit(unit)), sample=args.audit_sample)


def _pool():
    from .pool import pool_or_none
    pool = pool_or_none()
    if pool is None:
        raise SystemExit("parse/translate/resolve work on the resource pool: SUPABASE_URL and SUPABASE_SECRET_KEY are required")
    return pool


def entry() -> None:
    """Console entry point: surface crashes as a GitHub annotation so they're readable without the raw log."""
    import os
    import sys
    import traceback
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        tb = traceback.format_exc()
        if os.environ.get("GITHUB_ACTIONS"):
            print("::error::" + tb[-1500:].replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A"))
        print(tb, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    entry()
