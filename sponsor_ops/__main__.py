"""命令行入口。

示例（见 README 的中文说明）：
    python -m sponsor_ops init
    python -m sponsor_ops contract C-drink-a
    python -m sponsor_ops trace C-drink-a
    python -m sponsor_ops break B135
    python -m sponsor_ops invoice INV-A
    python -m sponsor_ops plan C-drink-a --level conservative
    python -m sponsor_ops reach C-drink-a
    python -m sponsor_ops accept Q-A B135 --on 2026-08-30
    python -m sponsor_ops ingest <资料目录>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .export import export_all
from .materials import MaterialError
from .service import Service
from .store import Ledger


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def _emit(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sponsor_ops", description="电竞直播赞助履约核对")
    parser.add_argument("--db", help="账本路径，默认 state/ledger.sqlite3")
    parser.add_argument("--as-of", dest="as_of", help="截止业务日（只使用该日及以前到达的资料）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="首次接收 data 目录并建立账本")
    sub.add_parser("status", help="显示账本批次与资料行数")

    p = sub.add_parser("ingest", help="接收外部资料目录（含 catalog.json 与 jsonl）")
    p.add_argument("directory")

    p = sub.add_parser("contract", help="合同交付核对")
    p.add_argument("contract")

    p = sub.add_parser("trace", help="合同逐条合格曝光")
    p.add_argument("contract")

    p = sub.add_parser("break", help="广告段已交付或占用情况")
    p.add_argument("break_id")

    p = sub.add_parser("invoice", help="账单可支持金额与差额")
    p.add_argument("invoice")

    p = sub.add_parser("reach", help="触达口径与范围说明")
    p.add_argument("contract")

    p = sub.add_parser("plan", help="补量计划")
    p.add_argument("contract", nargs="?")
    p.add_argument("--level", default="balanced", choices=["conservative", "balanced", "aggressive"])

    p = sub.add_parser("accept", help="客户接受补量广告段")
    p.add_argument("request")
    p.add_argument("breaks", nargs="+")
    p.add_argument("--on", required=True)

    p = sub.add_parser("export", help="导出合同、补量与回传口径（只读）")
    p.add_argument("--out", default="outputs", help="导出目录，默认 outputs/")

    p = sub.add_parser("adjust", help="登记账款调整建议")
    p.add_argument("contract")
    p.add_argument("--on", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--amount-minor", type=int, required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = _root()
    db = Path(args.db) if args.db else root / "state" / "ledger.sqlite3"
    ledger = Ledger(root, db)
    try:
        if args.command == "init":
            batch = ledger.bootstrap()
            _emit({"batch": batch, "db": str(db), "tables": {n: len(ledger.table(n)) for n in ledger.catalog["tables"]}})
            return 0
        if args.command == "ingest":
            ledger.bootstrap()
            _emit(ledger.ingest_dir(Path(args.directory)))
            return 0
        ledger.bootstrap()
        service = Service(ledger, as_of=args.as_of)
        if args.command == "status":
            _emit(
                {
                    "db": str(db),
                    "batches": ledger.batches(),
                    "tables": {n: len(ledger.table(n)) for n in ledger.catalog["tables"]},
                    "as_of": service.snapshot().as_of,
                }
            )
        elif args.command == "contract":
            _emit(service.contract_report(args.contract))
        elif args.command == "trace":
            _emit(service.contract_trace(args.contract))
        elif args.command == "break":
            _emit(service.ad_break_report(args.break_id))
        elif args.command == "invoice":
            _emit(service.invoice_report(args.invoice))
        elif args.command == "reach":
            _emit(service.reach_explanation(args.contract))
        elif args.command == "plan":
            plans = service.plans(args.level, args.contract)
            _emit([plan.to_dict() for plan in plans])
        elif args.command == "accept":
            _emit(service.accept_makegood(args.request, args.breaks, args.on))
        elif args.command == "adjust":
            _emit(service.adjust_billing(args.contract, args.on, args.reason, args.amount_minor))
        elif args.command == "export":
            _emit(export_all(service, Path(args.out)))
        return 0
    except (MaterialError, KeyError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    finally:
        ledger.close()


if __name__ == "__main__":
    raise SystemExit(main())
