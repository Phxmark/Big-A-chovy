#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
20样本量化影子验证系统 (Shadow Tracking System) - 权威升级版
来源：《选股框架.md》与《CLAUDE.md》2026-08-21 优化待验证项

负责采集与追踪 4 大待验证创新机制的实战样本：
1. coalition: 机构游资合力主升（超大单≥2000万、大单>0、主力≥5000万、
   20%<=超单/主力<50%、5分增量≥1000万、连续两期主力与超大单均未衰减、
   主买比≥1.5）；采样只接受生产报告明确标签 `✓(合力)`，不在采集端数值推导
2. breakout: 明日观察池突破升级状态机 (CONFIRMED / B_BREAKOUT / A_STRICT)
3. sector_boost: 主线板块协同加分器 (20亿锚点 + 3只共振 + 协同加15分)
4. divergence: 龙头分歧识别(divergence_leader·待验证项⑨)，由
   detect_divergence_leader.py --record 全日序列判定后写入；本模块只负责
   保留与 T+1 结算，不做采集端数值推导

严格仅用于模拟仓影子验证，记录 T+1 09:45 收益、最大浮盈、最大回撤与假突破率。
"""

import os
import sys
import json
import re
import argparse
import importlib
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "daily-stock-analysis" / "scripts"
for import_path in (PROJECT_ROOT, TOOLS_DIR, SCRIPTS_DIR):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from tools.report_parser import parse_screening_report, get_report_files
from tools.rule_config import (
    RULE_CONFIG,
    hhmm_to_minutes,
    is_complete_shadow_result,
    shadow_targets,
)

SHADOW_DATA_DIR = Path(__file__).resolve().parent / "shadow_data"
SHADOW_DB_FILE = SHADOW_DATA_DIR / "shadow_samples.json"
T1_SOURCE_DAILY_KLINE = "daily_kline"
T1_SOURCE_REPORT_SNAPSHOTS_ONLY = "report_snapshots_only"
T1_SOURCE_UNAVAILABLE = "unavailable"
SETTLEMENT_VERSION = RULE_CONFIG["shadow"]["settlement_version"]


def init_db() -> Dict[str, Any]:
    """初始化或加载影子样本数据库。"""
    SHADOW_DATA_DIR.mkdir(parents=True, exist_ok=True)
    if SHADOW_DB_FILE.exists():
        try:
            data = json.loads(SHADOW_DB_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("targets", {})
                data.setdefault("samples", {})
                # 迁移：为新配置登记的类别补齐空结构，但不覆盖已有样本
                # 或旧目标值；目标值漂移由 validate_consistency.py 报告。
                for category, meta in shadow_targets().items():
                    data["targets"].setdefault(category, dict(meta))
                    data["samples"].setdefault(category, [])
                return data
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"影子样本库无法读取，已停止以避免覆盖：{SHADOW_DB_FILE}") from exc
        raise ValueError(f"影子样本库结构无效，已停止以避免覆盖：{SHADOW_DB_FILE}")
    return {
        "version": "2.0",
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "targets": shadow_targets(),
        "samples": {category: [] for category in shadow_targets()},
    }


def save_db(db: Dict[str, Any]) -> None:
    """持久化保存影子样本数据库。"""
    SHADOW_DATA_DIR.mkdir(parents=True, exist_ok=True)
    db["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload = json.dumps(db, ensure_ascii=False, indent=2)
    if SHADOW_DB_FILE.exists():
        backup = SHADOW_DB_FILE.with_name(SHADOW_DB_FILE.name + ".bak")
        shutil.copy2(SHADOW_DB_FILE, backup)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=SHADOW_DATA_DIR,
                                         prefix=".shadow-", suffix=".tmp", delete=False) as fp:
            tmp_path = Path(fp.name)
            fp.write(payload)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_path, SHADOW_DB_FILE)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def is_number(val: Any) -> bool:
    if val is None or val in ("", "-", "--", "None"):
        return False
    try:
        float(val)
        return True
    except (TypeError, ValueError):
        return False


def parse_val(val_str: Any) -> float:
    if val_str is None or val_str in ("-", "None", ""):
        return 0.0
    s = str(val_str).replace("+", "").replace("%", "").replace("pct", "").replace(",", "").strip()
    if "亿" in s:
        try:
            return float(s.replace("亿", "")) * 10000.0
        except ValueError:
            return 0.0
    if "万" in s:
        try:
            return float(s.replace("万", ""))
        except ValueError:
            return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def collect_samples_from_report(filepath: str, db: Dict[str, Any]) -> int:
    """从单份筛选报告中提取合力主升、观察池突破与板块协同影子样本。"""
    rep = parse_screening_report(filepath)
    date_str = rep["date"]
    time_str = rep["time"]

    for category in ("coalition", "breakout", "sector_boost"):
        db.setdefault("samples", {}).setdefault(category, [])
    existing_keys = {
        cat: {f"{s['code']}_{s['date']}" for s in db["samples"][cat]}
        for cat in ("coalition", "breakout", "sector_boost")
    }

    added_count = 0
    tables = rep.get("tables") or {}

    # 1. 采集 合力主升 (coalition) 样本（严格互斥：20% <= 超单占比 < 50%）
    low_short = tables.get("low_absorb_short") or []
    for r in low_short:
        code = r.get("代码", "")
        name = r.get("名称", "")
        super_lead = r.get("超单主导", "")
        super_str = r.get("超大单", "0")
        super_wan = parse_val(super_str)
        main_pct = parse_val(r.get("主力净占比", "0"))
        amt_wan = parse_val(r.get("成交额", "0"))
        main_net_wan = parse_val(r.get("主力净额", "0"))
        if main_net_wan == 0.0 and main_pct > 0 and amt_wan > 0:
            main_net_wan = amt_wan * (main_pct / 100.0)
        inc5_wan = parse_val(r.get("5分钟增量", "0"))
        price = parse_val(r.get("现价", "0"))

        super_ratio = (super_wan / main_net_wan * 100) if main_net_wan > 0 else 0.0
        # 影子样本只接受生产报告明确写出的严格合力标签。数值字段仅
        # 用于记录样本，不得在采集端重新推导标签并绕过主买比、历史快照
        # 与大单条件。
        is_pure_coalition = super_lead == "✓(合力)"

        sample_key = f"{code}_{date_str}"
        if is_pure_coalition and sample_key not in existing_keys["coalition"]:
            sample = {
                "id": f"COAL_{date_str}_{code}",
                "code": code,
                "name": name,
                "date": date_str,
                "trigger_time": time_str,
                "report_file": rep["file"],
                "trigger_price": price,
                "plate": r.get("板块", "-"),
                "super_wan": round(super_wan, 1),
                "main_net_wan": round(main_net_wan, 1),
                "super_ratio": round(super_ratio, 1),
                "inc5_wan": round(inc5_wan, 1),
                "t1_result": None,
            }
            db["samples"]["coalition"].append(sample)
            existing_keys["coalition"].add(sample_key)
            added_count += 1

    # 2. 采集 观察池突破状态机 (breakout) 样本
    watchlist = tables.get("tomorrow_watchlist") or []
    for r in watchlist:
        code = r.get("代码", "")
        name = r.get("名称", "")
        state = r.get("突破状态", "")
        price = parse_val(r.get("当前价", r.get("现价", "0")))
        trigger = parse_val(r.get("触发价", "0"))
        dom = r.get("超单主导", "")

        is_breakout = (
            state in ("CONFIRMED", "B_BREAKOUT", "A_STRICT") or
            ("已站稳" in r.get("状态说明", "") and price >= trigger and trigger > 0)
        )
        sample_key = f"{code}_{date_str}"
        if is_breakout and sample_key not in existing_keys["breakout"]:
            sample = {
                "id": f"BRK_{date_str}_{code}",
                "code": code,
                "name": name,
                "date": date_str,
                "trigger_time": time_str,
                "report_file": rep["file"],
                "trigger_price": price,
                "benchmark_trigger": trigger,
                "plate": r.get("板块", "-"),
                "state": state,
                "confirm_count": r.get("确认次数", "2次"),
                "dominance": dom,
                "t1_result": None,
            }
            db["samples"]["breakout"].append(sample)
            existing_keys["breakout"].add(sample_key)
            added_count += 1

    # 3. 采集 主线板块协同 (sector_boost) 样本
    cap_rank = tables.get("capital_ranking") or []
    for r in cap_rank:
        code = r.get("代码", "")
        name = r.get("名称", "")
        reason = (
            r.get("评分依据") or r.get("理由") or r.get("资金理由") or
            r.get("capital_reason") or r.get("capital_reason", "") or ""
        )
        price = parse_val(r.get("现价", "0"))
        boost_val = parse_val(r.get("sector_boost", 0))

        is_boosted = "主线板块协同" in reason or "20亿锚点" in reason or "锚点带动" in reason or boost_val > 0
        sample_key = f"{code}_{date_str}"
        if is_boosted and sample_key not in existing_keys["sector_boost"]:
            sample = {
                "id": f"BOOST_{date_str}_{code}",
                "code": code,
                "name": name,
                "date": date_str,
                "trigger_time": time_str,
                "report_file": rep["file"],
                "trigger_price": price,
                "plate": r.get("板块", "-"),
                "score": parse_val(r.get("评分", "0")),
                "boost_points": 15,
                "t1_result": None,
            }
            db["samples"]["sector_boost"].append(sample)
            existing_keys["sector_boost"].add(sample_key)
            added_count += 1

    return added_count


def next_trading_day(date_str: str) -> Optional[str]:
    """从上证指数日 K 确认 T+1；数据未覆盖目标时不猜测。"""
    try:
        day = datetime.strptime(date_str, "%Y%m%d")
        tencent_kline = importlib.import_module("tencent_kline")
        payload, _ = tencent_kline.fetch_kline_json(
            "sh000001", 40,
            start=(day - timedelta(days=7)).strftime("%Y-%m-%d"),
            end=(day + timedelta(days=30)).strftime("%Y-%m-%d"),
        )
        dates = sorted({str(row[0]).replace("-", "") for row in
                        tencent_kline.kline_rows(payload, "sh000001")
                        if row and re.fullmatch(r"\d{4}-?\d{2}-?\d{2}", str(row[0]))})
        if date_str not in dates:
            return None
        index = dates.index(date_str)
        return dates[index + 1] if index + 1 < len(dates) else None
    except (ValueError, RuntimeError, OSError, IndexError):
        return None


def find_next_trading_day_reports(reports_dir: str, date_str: str,
                                  expected_date: Optional[str] = None) -> List[str]:
    """只返回指数日 K 确认的 T+1 当日报告；漏采不顺延。"""
    next_date = expected_date or next_trading_day(date_str)
    return get_report_files(reports_dir, next_date) if next_date else []


def pending_t1_label(reports_dir: str, date_str: str,
                     expected_date: Optional[str] = None) -> str:
    """日期由指数日 K 确认，不依赖是否存在报告。"""
    next_date = expected_date or next_trading_day(date_str)
    return f"待补数据({next_date})" if next_date else "交易日期待确认"


def fetch_t1_day_kline_extremes(code: str, t1_date: str) -> Optional[Tuple[float, float]]:
    """尝试从日K数据获取该股票在次日交易日的真实全日最高价与最低价。"""
    try:
        a_share_daily_screen = importlib.import_module("a_share_daily_screen")
        k_rows, _source = a_share_daily_screen.fetch_kline(code)
        target_fmt = f"{t1_date[:4]}-{t1_date[4:6]}-{t1_date[6:]}" if len(t1_date) == 8 else t1_date
        for kr in (k_rows or []):
            row_date = str(kr.get("date") or kr.get("day") or "")[:10]
            if row_date.replace("/", "-") == target_fmt.replace("/", "-"):
                h = float(kr.get("high", 0))
                l = float(kr.get("low", 0))
                if h > 0 and l > 0:
                    return h, l
    except Exception:
        pass
    return None


def calculate_t1_for_sample(sample: Dict[str, Any], t1_reports: List[str],
                            expected_date: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """用已确认的交易日、目标窗口快照和当日日 K 结算影子样本。"""
    expected_date = expected_date or next_trading_day(sample.get("date", ""))
    if not expected_date:
        return None
    code = sample["code"]
    trigger_price = float(sample["trigger_price"])
    if trigger_price <= 0:
        return None

    target = hhmm_to_minutes(RULE_CONFIG["shadow"]["snapshot_target"])
    latest = hhmm_to_minutes(RULE_CONFIG["shadow"]["snapshot_latest"])
    candidates: Dict[int, Dict[float, str]] = {}
    for r_file in t1_reports:
        try:
            rep = parse_screening_report(r_file)
        except (OSError, ValueError):
            continue
        if rep.get("date") != expected_date:
            continue
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", rep.get("time", ""))
        if not match:
            continue
        minute = int(match[1]) * 60 + int(match[2])
        if not target <= minute <= latest:
            continue
        for rows in rep.get("tables", {}).values():
            for row in rows:
                if row.get("代码") == code:
                    price = parse_val(row.get("现价") or row.get("当前价") or row.get("价格"))
                    if price > 0:
                        candidates.setdefault(minute, {}).setdefault(price, rep.get("filepath") or r_file)

    snapshot_time = snapshot_file = snapshot_price = None
    reason = "snapshot_missing"
    if candidates:
        first = min(candidates)
        if len(candidates[first]) == 1:
            snapshot_price, snapshot_file = next(iter(candidates[first].items()))
            snapshot_time = f"{first // 60:02d}:{first % 60:02d}"
            reason = None
        else:
            reason = "snapshot_conflict"

    day_extremes = fetch_t1_day_kline_extremes(code, expected_date) if candidates else None
    extremes_complete = day_extremes is not None
    if extremes_complete:
        high, low = day_extremes
        max_gain = round((high - trigger_price) / trigger_price * 100, 2)
        max_dd = round((low - trigger_price) / trigger_price * 100, 2)
        stop_pct = float(RULE_CONFIG["shadow"]["false_breakout_stop_pct"])
        is_false_breakout = low < trigger_price * (1.0 - stop_pct / 100.0)
    else:
        max_gain = max_dd = is_false_breakout = None
        if reason is None:
            reason = "daily_kline_missing"
    complete = snapshot_price is not None and extremes_complete
    return {
        "settlement_version": SETTLEMENT_VERSION,
        "checked": complete,
        "date_confirmed": True,
        "calendar_source": "tencent_sh000001_daily_kline",
        "t1_date": expected_date,
        "snapshot_valid": snapshot_price is not None,
        "snapshot_time": snapshot_time,
        "snapshot_file": snapshot_file,
        "snapshot_precision": "minute" if snapshot_time else None,
        "snapshot_target": RULE_CONFIG["shadow"]["snapshot_target"],
        "snapshot_latest": RULE_CONFIG["shadow"]["snapshot_latest"],
        "missing_reason": reason,
        "t1_0945_price": round(snapshot_price, 2) if snapshot_price is not None else None,
        "t1_0945_return_pct": round((snapshot_price - trigger_price) / trigger_price * 100, 2)
        if snapshot_price is not None else None,
        "t1_max_gain_pct": max_gain,
        "t1_max_drawdown_pct": max_dd,
        "is_false_breakout": is_false_breakout,
        "extremes_complete": extremes_complete,
        "source": T1_SOURCE_DAILY_KLINE if extremes_complete else T1_SOURCE_REPORT_SNAPSHOTS_ONLY,
    }


def update_all_t1_metrics(db: Dict[str, Any], reports_dir: Optional[str] = None) -> None:
    """自动核算所有样本的 T+1 次日真实表现（含 divergence 等全部机制类别）。"""
    reports_dir = reports_dir or os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "筛选结果"))
    date_cache: Dict[str, Optional[str]] = {}
    for category in sorted(db["samples"].keys()):
        for sample in db["samples"][category]:
            date_str = sample["date"]
            if date_str not in date_cache:
                date_cache[date_str] = next_trading_day(date_str)
            expected_date = date_cache[date_str]
            t1_reports = find_next_trading_day_reports(reports_dir, date_str, expected_date) if expected_date else []
            if t1_reports:
                res = calculate_t1_for_sample(sample, t1_reports, expected_date)
                if res:
                    sample["t1_result"] = res
            if sample.get("t1_result") is None:
                sample["t1_result"] = {
                    "settlement_version": SETTLEMENT_VERSION,
                    "checked": False,
                    "date_confirmed": expected_date is not None,
                    "calendar_source": "tencent_sh000001_daily_kline" if expected_date else None,
                    "t1_date": pending_t1_label(reports_dir, date_str, expected_date),
                    "snapshot_valid": False,
                    "snapshot_time": None,
                    "snapshot_file": None,
                    "missing_reason": "report_missing" if expected_date else "date_unconfirmed",
                    "t1_0945_price": None,
                    "t1_0945_return_pct": None,
                    "t1_max_gain_pct": None,
                    "t1_max_drawdown_pct": None,
                    "is_false_breakout": None,
                    "extremes_complete": False,
                    "source": T1_SOURCE_UNAVAILABLE,
                }


def generate_report(db: Dict[str, Any]) -> str:
    """生成 20 样本影子验证进度与指标汇总报告。"""
    lines = []
    lines.append("# 📊 20 样本量化影子验证系统进度报表 (权威定版)")
    lines.append(f"**更新时点**：`{db.get('last_updated', '-')}` ｜ **风控状态**：`待验证 · 仅模拟仓权限`\n")

    lines.append("## 一、四大待验证项目进度汇总")
    lines.append("09:45 附近快照收益使用 09:45–09:47 的首个有效快照，不代表实际成交收益。最大不利波动是相对触发价的日内最低价跌幅，不是账户最大回撤。")
    lines.append("| 待验证项目 | 目标样本 | 已收集 | 完整结算 | 待补数据 | 收集完成度 | 胜率 (快照收益>0) | 平均快照收益 | 平均最大浮盈 | 平均最大不利波动 | 假突破率 | 当前状态 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")

    for cat, meta in db["targets"].items():
        samples = db["samples"].get(cat, [])
        count = len(samples)
        target = meta["target_samples"]
        pct = count / target * 100

        # 计算指标
        evaluated_samples = [s for s in samples if is_complete_shadow_result(s.get("t1_result"))]
        complete_count = len(evaluated_samples)
        if evaluated_samples:
            wins = sum(1 for s in evaluated_samples if (s["t1_result"].get("t1_0945_return_pct") or 0) > 0)
            win_rate = f"{wins / len(evaluated_samples) * 100:.1f}%"
            avg_ret = f"{sum(s['t1_result']['t1_0945_return_pct'] for s in evaluated_samples) / len(evaluated_samples):+.2f}%"
            avg_gain = f"{sum(s['t1_result']['t1_max_gain_pct'] for s in evaluated_samples) / len(evaluated_samples):+.2f}%"
            avg_dd = f"{sum(s['t1_result']['t1_max_drawdown_pct'] for s in evaluated_samples) / len(evaluated_samples):+.2f}%"
            false_bo = f"{sum(1 for s in evaluated_samples if s['t1_result'].get('is_false_breakout')) / len(evaluated_samples) * 100:.1f}%"
        else:
            win_rate = avg_ret = avg_gain = avg_dd = false_bo = "待补数据"

        status = (
            "🟢 验证达标"
            if count >= target and len(evaluated_samples) >= target
            else "🟡 影子数据采集中"
        )
        lines.append(f"| **{meta['name']}** | {target} | **{count}** | {complete_count} | {count - complete_count} | {pct:.1f}% | {win_rate} | {avg_ret} | {avg_gain} | {avg_dd} | {false_bo} | {status} |")

    lines.append("\n## 二、当前已入库影子样本明细")
    for cat, meta in db["targets"].items():
        samples = db["samples"].get(cat, [])
        lines.append(f"\n### 📌 {meta['name']} 样本池 ({len(samples)}/{meta['target_samples']})")
        if samples:
            lines.append("| 样本ID | 代码 | 名称 | 入库日期 | 触发时点 | 触发价 | 板块 | 关键量化特征 | T+1 09:45 附近快照收益 | 结算状态 |")
            lines.append("|---|---|---|---|---|---|---|---|---|---|")
            for s in samples:
                feat = (
                    f"超单:{s.get('super_wan',0):.0f}万/主力:{s.get('main_net_wan',0):.0f}万(占比{s.get('super_ratio',0):.1f}%)"
                    if cat == "coalition"
                    else (f"状态:{s.get('state')} 触发基准:{s.get('benchmark_trigger')}" if cat == "breakout"
                          else (f"场景:{s.get('scenario','-')} 主力:{s.get('mainp_pct',0)}% 超单:{s.get('xl_wan',0):+.0f}万 回落:{s.get('pullback_pct',0)}%"
                                if cat == "divergence"
                                else f"协同评分:{s.get('score')} (+15分)"))
                )
                t1_res = s.get("t1_result") or {}
                t1_txt = t1_res.get("t1_0945_return_pct")
                t1_disp = f"{t1_txt:+.2f}%" if isinstance(t1_txt, (int, float)) else "待补数据"
                if is_complete_shadow_result(t1_res):
                    status_disp = f"✅ 已核算({t1_res.get('t1_date')} {t1_res.get('snapshot_time')})"
                elif t1_res.get("settlement_version") != SETTLEMENT_VERSION and t1_res:
                    t1_disp = "旧口径待重算"
                    status_disp = "⏳ 旧口径待重算"
                else:
                    status_disp = f"⏳ 待补数据({t1_res.get('missing_reason', 'unavailable')})"
                lines.append(f"| `{s['id']}` | {s['code']} | {s['name']} | {s['date']} | {s['trigger_time']} | {s['trigger_price']:.2f} | {s['plate']} | {feat} | {t1_disp} | {status_disp} |")
        else:
            lines.append("*（暂无样本）*")

    return "\n".join(lines)


def scan_and_update(date_str: Optional[str] = None) -> None:
    # 强制重构并重新从报告解析以保证核心三类样本严格互斥。
    # divergence 等旁路类样本由各自检测器（如 detect_divergence_leader.py --record）
    # 全日序列判定维护，本扫描原样保留，避免重建核心三类时被覆盖清除。
    previous = init_db()
    db = {
        "version": "3.0",
        "last_updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "targets": shadow_targets(),
        "samples": {category: [] for category in ("coalition", "breakout", "sector_boost")},
    }
    for cat, arr in previous.get("samples", {}).items():
        if cat not in db["samples"] and arr:
            db["targets"][cat] = previous.get("targets", {}).get(
                cat, {"name": cat, "target_samples": 20})
            db["samples"][cat] = arr
    reports_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "筛选结果"))
    files = get_report_files(reports_dir, date_str)
    
    total_added = 0
    for f in files:
        added = collect_samples_from_report(f, db)
        total_added += added

    for category in ("coalition", "breakout", "sector_boost"):
        existing_keys = {(row.get("code"), row.get("date")) for row in db["samples"][category]}
        for old in previous.get("samples", {}).get(category, []):
            if isinstance(old, dict) and (old.get("code"), old.get("date")) not in existing_keys:
                db["samples"][category].append(old)
                existing_keys.add((old.get("code"), old.get("date")))
        old_results = {
            (row.get("code"), row.get("date")): row.get("t1_result")
            for row in previous.get("samples", {}).get(category, [])
            if isinstance(row, dict)
        }
        for row in db["samples"][category]:
            row["t1_result"] = old_results.get((row.get("code"), row.get("date")))

    update_all_t1_metrics(db)
    save_db(db)
    print(f"=== 影子系统扫描完成：新增/更新 {total_added} 个样本，当前总样本库状态已更新 ===")
    print(generate_report(db))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="20样本量化影子验证系统")
    parser.add_argument("--date", type=str, default=None, help="日期 YYYYMMDD")
    parser.add_argument("--report", action="store_true", help="输出当前影子验证报表")
    args = parser.parse_args()

    if args.report:
        db = init_db()
        print(generate_report(db))
    else:
        scan_and_update(args.date)
