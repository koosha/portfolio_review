"""Portfolio Review operations, all using the same services as the local UI."""

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from portfolio_lab.config import load_config, write_config
from portfolio_lab.demo import create_demo
from portfolio_lab.ingestion import inspect_database
from portfolio_lab.pipeline import export_report, replay_analysis, run_analysis

from .calendar import decision_context
from .service import ResearchService, default_config


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(
        prog="portfolio-review", description="Portfolio Review · local monthly research"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="Create a separate synthetic dataset and saved review")
    demo.add_argument("--directory", default="data/demo")
    demo.add_argument("--serve", action="store_true")
    demo.add_argument("--port", type=int, default=8766)
    inspect = sub.add_parser("inspect", help="Inspect source schema read-only")
    inspect.add_argument("--database", required=True)
    init = sub.add_parser("init", help="Write an unconfirmed real collector configuration")
    init.add_argument("--data-dir", default="data")
    init.add_argument("--out")
    restore = sub.add_parser("restore", help="Restore a verified backup into a NEW directory")
    restore.add_argument("--from", dest="source", required=True)
    restore.add_argument("--into", required=True)
    for command in (
        "serve",
        "run",
        "replay",
        "evaluate",
        "evaluate-ledgers",
        "export",
        "backup",
        "migrate",
        "forecast-template",
        "supplemental-template",
    ):
        item = sub.add_parser(command)
        item.add_argument("--config", default="data/research-config.json")
        if command in {"run", "forecast-template"}:
            item.add_argument("--as-of")
        if command == "run":
            item.add_argument("--refresh", action="store_true")
            item.add_argument(
                "--request-key",
                help="Stable monthly scheduler key; defaults to the decision month/date",
            )
        if command == "serve":
            item.add_argument("--port", type=int, default=8765)
            item.add_argument("--data-dir")
        if command in {"replay", "evaluate", "evaluate-ledgers", "export"}:
            item.add_argument("--run-id", required=True)
        if command == "replay":
            item.add_argument("--patch")
            item.add_argument("--save", action="store_true")
        if command == "evaluate":
            item.add_argument("--prices", required=True)
            item.add_argument("--evaluation-date", required=True)
        if command == "evaluate-ledgers":
            item.add_argument(
                "--inputs",
                required=True,
                help="JSON with events_by_arm, end_prices, end_date and coverage/execution confirmations",
            )
        if command in {
            "evaluate",
            "evaluate-ledgers",
            "export",
            "backup",
            "forecast-template",
            "supplemental-template",
        }:
            item.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            print(json.dumps(inspect_database(args.database), indent=2))
            return 0
        if args.command == "restore":
            from .operations import restore_backup

            print(
                f"Restored into {restore_backup(args.source, args.into)}. Review paths, then launch from that new data directory."
            )
            return 0
        if args.command == "init":
            from portfolio.storage import Store

            directory = Path(args.data_dir).resolve()
            target = Path(args.out or directory / "research-config.json")
            if target.exists():
                raise ValueError("Configuration already exists; edit it instead of overwriting it.")
            Store(directory)
            write_config(target, default_config(directory))
            print(f"Created unconfirmed research configuration: {target.resolve()}")
            return 0
        if args.command == "demo":
            config_path = create_demo(args.directory)
            config = load_config(config_path)
            from .demo import demo_workspace

            result, _ = run_analysis(config, "2026-08-31", workspace=demo_workspace())
            print(f"Portfolio Review · SYNTHETIC DEMO · Run {result['run_id']}")
            print(f"Configuration: {config_path}")
            if args.serve:
                from portfolio.server import main as serve

                serve(
                    [
                        "--data-dir",
                        str(config_path.parent / "collector"),
                        "--research-config",
                        str(config_path),
                        "--port",
                        str(args.port),
                    ]
                )
            return 0
        config = load_config(args.config)
        if args.command == "serve":
            from portfolio.server import main as serve

            serve(
                [
                    "--research-config",
                    str(Path(args.config).resolve()),
                    "--data-dir",
                    args.data_dir or str(Path(config["source"]["path"]).parent),
                    "--port",
                    str(args.port),
                ]
            )
            return 0
        service = ResearchService(config)
        try:
            if args.command == "migrate":
                print(
                    "Research schema is current; prior runs were preserved. Source migrations belong to collector startup."
                )
                return 0
            if args.command == "backup":
                from .operations import backup_project

                print(
                    f"Consistent private backup: {backup_project(service.resolved_config(), args.out, Path(config['source']['path']).parent)}"
                )
                return 0
            if args.command == "export":
                print(export_report(service.store.load_run(args.run_id), args.out))
                return 0
            if args.command == "supplemental-template":
                output = service.supplemental()
                if not output["supported"]:
                    raise ValueError(output["reason"])
                target = Path(args.out)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("x") as file:
                    json.dump(output["template"], file, indent=2)
                print(f"Blank, snapshot-bound supplemental template: {target}")
                return 0
            if args.command == "forecast-template":
                from portfolio_lab.pipeline import load_inputs

                day = decision_context(args.as_of)["decision_date"]
                bundle = load_inputs(
                    service.resolved_config(),
                    day,
                    supplemental=service.store.latest("supplemental"),
                )
                import pandas as pd

                rows = [
                    {
                        "security_id": sid,
                        "scenario": label,
                        "horizon_months": horizon,
                        "return_value": None,
                        "probability": None,
                        "basis": "subjective",
                        "forecast_date": day,
                        "source": "",
                        "calibration_id": None,
                    }
                    for sid in sorted(bundle["securities"]["security_id"].unique())
                    for horizon in (6, 12, 18)
                    for label in ("Adverse", "Central", "Favorable")
                ]
                target = Path(args.out)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("x") as file:
                    pd.DataFrame(rows).to_csv(file, index=False)
                print(f"Blank scenario template: {target}")
                return 0
            if args.command == "replay":
                patch = json.loads(Path(args.patch).read_text()) if args.patch else {}
                result, _, _ = replay_analysis(config, args.run_id, patch, args.save)
                print(
                    json.dumps(
                        {
                            "run_id": result.get("run_id"),
                            "parent_run_id": args.run_id,
                            "preview": not args.save,
                            "allocation": result["allocation"]["status"],
                        }
                    )
                )
                return 2 if result["quality_summary"]["error"] else 0
            if args.command == "evaluate-ledgers":
                payload = json.loads(Path(args.inputs).read_text())
                job = service.submit(
                    {
                        **payload,
                        "kind": "evaluate_ledgers",
                        "request_key": uuid.uuid4().hex,
                        "run_id": args.run_id,
                    }
                )
            elif args.command == "evaluate":
                job = service.submit(
                    {
                        "kind": "evaluate",
                        "request_key": uuid.uuid4().hex,
                        "run_id": args.run_id,
                        "prices_csv": Path(args.prices).read_text(),
                        "evaluation_date": args.evaluation_date,
                    }
                )
            else:
                day = decision_context(args.as_of)["decision_date"]
                job = service.submit(
                    {
                        "kind": "monthly",
                        "request_key": args.request_key or f"monthly-{day}",
                        "as_of": day,
                        "refresh": args.refresh,
                    }
                )
            finished = service.wait(job["job_id"])
            if finished["status"] != "complete":
                raise ValueError(finished["error"] or "Research job did not complete.")
            if args.command in {"evaluate", "evaluate-ledgers"}:
                target = Path(args.out)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("x") as file:
                    json.dump(finished["output"], file, indent=2, allow_nan=False)
                print(f"Evaluation saved: {target}")
                return 0
            result = finished["output"]["result"]
            print(
                json.dumps(
                    {
                        "run_id": result["run_id"],
                        "as_of": result["metadata"]["as_of"],
                        "allocation": result["allocation"]["status"],
                        "issues": result["quality_summary"],
                    },
                    indent=2,
                )
            )
            return 2 if result["quality_summary"]["error"] else 0
        finally:
            service.close()
    except (ValueError, FileNotFoundError, KeyError, FileExistsError) as exc:
        print(f"Portfolio Review: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
