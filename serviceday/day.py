"""命令行入口：构建并输出某个运营日的交割内容或对照。

用法::

    python3 -m serviceday.day --day 2026-11-29 --facts data/facts.json
    python3 -m serviceday.day --day 2026-11-29 --facts data/facts.json --operator A
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from .delivery import audit, DeliveryStore
from .engine import build_day
from .loader import load_facts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建运营日交割内容")
    parser.add_argument("--day", required=True, help="运营日 YYYY-MM-DD")
    parser.add_argument(
        "--facts", default="data/facts.json", help="资料文件路径"
    )
    parser.add_argument("--operator", default=None, help="只输出该运营方")
    parser.add_argument(
        "--audit",
        action="store_true",
        help="同时交付后重算，输出与当前更正结果的对照",
    )
    parser.add_argument("--out", default="-", help="输出文件，默认标准输出")
    args = parser.parse_args(argv)

    try:
        service_day = date.fromisoformat(args.day)
    except ValueError:
        parser.error(f"非法运营日: {args.day}")
    facts = load_facts(args.facts)

    if args.audit:
        store = DeliveryStore()
        store.deliver(facts, service_day)
        payload = audit(store, facts, service_day)
    else:
        payload = build_day(facts, service_day, args.operator).as_json()

    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out == "-":
        sys.stdout.write(text + "\n")
    else:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
