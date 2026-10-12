from __future__ import annotations

import argparse

STAGES = ["crawl", "extract", "translate", "load", "review"]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="mlm", description="Missionary Legacy Map pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="Run pipeline stages for a region")
    run.add_argument("--region", default="zhejiang")
    run.add_argument("--stage", choices=STAGES + ["all"], default="all")
    run.add_argument("--no-network-geocode", action="store_true")

    seed = sub.add_parser("seed", help="Load the editorial seed for a region")
    seed.add_argument("--region", default=None, help="One seed file (e.g. hangzhou); default: all files")
    seed.add_argument("--sql", action="store_true", help="Print SQL instead of writing via API")

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

    stages = STAGES if args.stage == "all" else [args.stage]
    for stage in stages:
        print(f"=== {stage} ({args.region}) ===")
        if stage == "crawl":
            from . import crawl
            crawl.run(args.region)
        elif stage == "extract":
            from . import extract
            extract.run(args.region)
        elif stage == "translate":
            from . import translate
            translate.run(args.region)
        elif stage == "load":
            from . import load
            load.load_extractions(args.region, use_network=not args.no_network_geocode)
        elif stage == "review":
            from . import review
            review.run()


if __name__ == "__main__":
    main()
