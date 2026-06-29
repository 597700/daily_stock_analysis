#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多因子量化选股模块 — 中证800+上证优选策略

功能：基于12因子多维度打分，从中证800+上证成分股中精选5-8只推荐
策略：技术面(40%) + 基本面(35%) + 资金面(25%)

两阶段因子计算：
  Pass 1 (粗筛): 利用实时行情快照快速估算因子 → 筛选 Top 50 候选
  Pass 2 (精算): 对候选股获取历史K线，计算真实技术因子 → 最终精选

用法：
  python3 scripts/quant_daily_picks.py              # 仅生成报告
  python3 scripts/quant_daily_picks.py --publish    # 生成 + 润色 + 发布
  python3 scripts/quant_daily_picks.py --dry-run    # 仅测试数据采集

输出：reports/quant_picks_YYYYMMDD.md

依赖：复用 scripts/_shared.py（日志/DeepSeek/.env）
      复用 scripts/wechat_mp_publish.py（公众号发布）
"""

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# ── 项目路径 ──────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
SRC_QUANT_DIR = PROJECT_DIR / "src_quant"

sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(SRC_QUANT_DIR))
sys.path.insert(0, str(SRC_QUANT_DIR / "strategy"))

# 复用服务器共享库
from scripts._shared import (
    load_env, setup_script_logging, get_project_root,
    call_deepseek,
)
import logging

logger = logging.getLogger("quant_picks")

# ── 导入策略模块 ──────────────────────────────────────────
from strategy.factor_builder import (
    compute_all_technical_factors,
)
from strategy.factor_weights import get_default_weights
from strategy.risk_filter import apply_all_hard_filters, MarketTiming
from strategy.stock_screener import StockScreener

# 数据采集
os.environ.setdefault('TQDM_DISABLE', '1')  # 禁用 tqdm 进度条，避免非 TTY 环境下 BrokenPipeError
import akshare as ak


# ============================================================
# 配置常量
# ============================================================

# 两阶段筛选参数
COARSE_TOP_N = 80       # Pass 1 粗筛后保留数量
FINE_KLINE_DAYS = 90    # Pass 2 获取K线天数（覆盖约60个交易日）
MAX_BAOSTOCK_BATCH = 80 # Baostock 单次批量上限
BAOSTOCK_SLEEP = 0.3    # Baostock 请求间隔（秒）

# 服务器资源限制（1.6G RAM）
MAX_MEMORY_MB = 1200    # 内存警告阈值
BAOSTOCK_TIMEOUT = 180  # Baostock 总超时（秒）


# ============================================================
# 数据采集
# ============================================================

def fetch_pool_stocks() -> set:
    """获取中证800+上证成分股（沪深300 + 中证500 + 上证50 + 上证180）"""
    pool = set()
    for idx, name in [("000300", "沪深300"), ("000905", "中证500"),
                      ("000016", "上证50"), ("000010", "上证180")]:
        try:
            df = ak.index_stock_cons(symbol=idx)
            if df is not None and not df.empty:
                for c in ["品种代码", "成分券代码", "constituent_code", "代码"]:
                    if c in df.columns:
                        codes = df[c].astype(str).tolist()
                        pool.update(codes)
                        logger.info(f"{name}成分股: {len(codes)}只")
                        break
        except Exception as e:
            logger.warning(f"获取{name}成分股失败: {e}")

    if len(pool) < 100:
        logger.warning(f"成分股不足({len(pool)})，使用默认Top200市值股")
        try:
            try:
                df = ak.stock_zh_a_spot_em()
            except Exception:
                logger.warning("EastMoney API 失败, 降级到 Sina 源 (PE/PB/量比数据不可用)")
                df = ak.stock_zh_a_spot()
            if df is not None and not df.empty:
                mc_col = "总市值" if "总市值" in df.columns else None
                if mc_col:
                    top200 = df.nlargest(200, mc_col)
                    code_col = "代码" if "代码" in df.columns else "symbol"
                    pool = set(top200[code_col].astype(str).str.replace(".SH", "").str.replace(".SZ", ""))
                    logger.info(f"退用Top200市值股: {len(pool)}只")
        except Exception:
            pass

    return pool


def fetch_market_data():
    """获取市场全貌数据"""
    logger.info("获取A股全市场实时行情 (EastMoney)...")
    try:
        spot = ak.stock_zh_a_spot_em()
    except Exception:
        logger.warning("EastMoney API 失败, 降级到 Sina 源 (PE/PB/量比/换手率数据不可用)")
        spot = ak.stock_zh_a_spot()
    logger.info(f"获取到 {len(spot)} 只股票")

    # 列名适配（兼容不同 AkShare 版本）
    def _find_col(*candidates: str) -> str:
        """在 DataFrame 列中按优先级查找列名，找不到则返回 None"""
        for c in candidates:
            if c in spot.columns:
                return c
        return None

    pct_col = _find_col("涨跌幅", "pct_chg")
    code_col = _find_col("代码", "symbol")
    name_col = _find_col("名称", "name")
    vol_col = _find_col("成交量", "volume")
    price_col = _find_col("最新价", "close")
    amt_col = _find_col("成交额", "amount")

    # 安全读取：列名不存在时返回安全默认值
    if pct_col:
        up = int((spot[pct_col] > 0).sum())
        down = int((spot[pct_col] < 0).sum())
        limit_up = int((spot[pct_col] >= 9.8).sum())
        limit_down = int((spot[pct_col] <= -9.8).sum())
    else:
        up = down = limit_up = limit_down = 0
    total_amt = spot[amt_col].sum() / 1e8 if amt_col and amt_col in spot.columns else 0

    market_breadth = {
        "up_count": up, "down_count": down,
        "limit_up": limit_up, "limit_down": limit_down,
        "total_amount": round(total_amt, 0),
        "breadth_ratio": round(up / max(len(spot), 1), 4),
    }

    # 指数数据（用于市场择时）
    try:
        time.sleep(1)
        index_df = ak.stock_zh_index_daily(symbol="sh000300")
    except Exception:
        index_df = pd.DataFrame()

    return spot, market_breadth, index_df, code_col, name_col, pct_col, vol_col, price_col


# ============================================================
# Pass 1: 粗筛因子（基于实时行情快照）
# ============================================================

def compute_coarse_factors(filtered: pd.DataFrame, pct_col: str) -> pd.DataFrame:
    """基于行情快照快速估算因子值，用于初筛

    使用 spot 数据中可用的字段构建代理因子：
    - ma_trend_strength: 涨跌幅（短期动能代理）
    - volume_anomaly: 量比 - 1（真实成交量异动）
    - pe_percentile: PE 归一化值（估值代理）
    - pb_percentile: PB 归一化值（估值代理）
    - 其余因子: 中性值 0

    Returns:
        factor_df: index=股票代码, columns=12个因子
    """
    codes = filtered["_symbol"].values
    n = len(filtered)

    # ── 技术面因子 ──
    # MA趋势强度: 用涨跌幅作为短期趋势代理（标准化到 [-0.5, 0.5] 范围）
    if pct_col and pct_col in filtered.columns:
        pct_raw = filtered[pct_col].fillna(0).values
        ma_trend = np.clip(pct_raw / 100.0, -0.5, 0.5)
    else:
        ma_trend = np.zeros(n)

    # 成交量异动: 量比 - 1（量比是当日成交量/5日均量，正是我们要的）
    vol_ratio_col = None
    for c in ["量比", "volume_ratio"]:
        if c in filtered.columns:
            vol_ratio_col = c
            break
    if vol_ratio_col:
        vol_ratio = filtered[vol_ratio_col].fillna(1.0).values
        vol_anomaly = np.clip(vol_ratio - 1.0, -0.5, 2.0)
    else:
        vol_anomaly = np.zeros(n)

    # RSI/MACD/布林带: 快照数据无法计算，取中性值
    rsi_recovery = np.zeros(n)
    macd_cross = np.zeros(n)
    boll_position = np.full(n, 0.3)  # 中性偏低位

    # ── 基本面因子 ──
    pe_col = None
    for c in ["市盈率-动态", "市盈率", "pe"]:
        if c in filtered.columns:
            pe_col = c
            break
    pb_col = None
    for c in ["市净率", "pb"]:
        if c in filtered.columns:
            pb_col = c
            break

    if pe_col:
        pe_vals = filtered[pe_col].fillna(np.nan).values
        # PE 越低越好，归一化：PE/200（200倍PE=极值），最终 direction=-1 反转
        pe_factor = np.where((~np.isnan(pe_vals)) & (pe_vals > 0), pe_vals / 200.0, np.nan)
    else:
        pe_factor = np.full(n, np.nan)

    if pb_col:
        pb_vals = filtered[pb_col].fillna(np.nan).values
        # PB 越低越好，归一化：PB/20（20倍PB=极值）
        pb_factor = np.where((~np.isnan(pb_vals)) & (pb_vals > 0), pb_vals / 20.0, np.nan)
    else:
        pb_factor = np.full(n, np.nan)

    # 财报因子: 快照数据无法获取，取中性值
    roe_yoy = np.zeros(n)
    revenue_growth = np.zeros(n)
    profit_growth = np.zeros(n)

    # ── 资金面因子 ──
    # 尝试从快照获取资金流向（主力净流入字段通常不在 spot API 中）
    inflow_col = None
    for c in ["主力净流入", "main_net_inflow"]:
        if c in filtered.columns:
            inflow_col = c
            break

    # 换手率列（EastMoney 提供，用于构建资金流代理信号）
    turnover_col = None
    for c in ["换手率", "turnover_rate", "turn"]:
        if c in filtered.columns:
            turnover_col = c
            break

    if inflow_col:
        # 有真实主力净流入数据时直接使用
        inflow_raw = filtered[inflow_col].fillna(0).values
        major_inflow = np.clip(inflow_raw / 1e8, -0.5, 0.5)
        logger.info(f"资金流因子使用真实主力净流入数据, 非零比例={int((major_inflow != 0).sum()/n*100)}%")
    elif vol_ratio_col and turnover_col:
        # 无真实数据时用换手率+量比构建代理信号
        # 高换手率+高量比 → 资金活跃度高 → 大资金进出概率大
        vol_ratio_vals = filtered[vol_ratio_col].fillna(1.0).values
        turnover_vals = filtered[turnover_col].fillna(0).values
        flow_proxy = (vol_ratio_vals - 1.0) * np.clip(turnover_vals / 100.0, 0, 0.1)
        major_inflow = np.clip(flow_proxy, -0.5, 0.5)
        logger.info(f"资金流因子使用代理信号 (换手率*量比), 非零比例={int((major_inflow != 0).sum()/n*100)}%")
    else:
        major_inflow = np.zeros(n)
        logger.info("资金流因子无可用数据，全部置零")

    # 北向资金/融资余额: 无批量 API，保留中性值
    north_bound = np.zeros(n)
    margin_change = np.zeros(n)

    # ── 构建因子 DataFrame ──
    factor_data = {
        "ma_trend_strength": ma_trend,
        "rsi_oversold_recovery": rsi_recovery,
        "volume_anomaly": vol_anomaly,
        "macd_golden_cross": macd_cross,
        "bollinger_position": boll_position,
        "pe_percentile": pe_factor,
        "pb_percentile": pb_factor,
        "roe_yoy": roe_yoy,
        "revenue_growth": revenue_growth,
        "profit_growth": profit_growth,
        "major_net_inflow": major_inflow,
        "north_bound_change": north_bound,
        "margin_balance_change": margin_change,
    }

    factor_df = pd.DataFrame(factor_data, index=codes)
    logger.info(f"粗筛因子计算完成: {len(factor_df)}只, "
                f"PE有效={factor_df['pe_percentile'].notna().sum()}, "
                f"量比有效={factor_df['volume_anomaly'].notna().sum()}")

    return factor_df


# ============================================================
# Pass 2: 精算技术因子（基于历史K线）
# ============================================================

def fetch_kline_batch_baostock(codes: list, days: int = 90) -> dict:
    """通过 Baostock 批量获取历史K线数据

    Args:
        codes: 股票代码列表（6位数字字符串）
        days: 获取最近多少天的数据

    Returns:
        {code: DataFrame} 映射，DataFrame 含 close/open/high/low/volume 列
    """
    import baostock as bs

    kline_map = {}
    if not codes:
        return kline_map

    # 计算日期范围
    end_date = date.today().strftime("%Y-%m-%d")
    start_date = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")

    try:
        lg = bs.login()
        if lg.error_code != "0":
            logger.error(f"Baostock 登录失败: {lg.error_msg}")
            return kline_map

        success_count = 0
        fail_count = 0
        baostock_start = time.time()

        for i, code in enumerate(codes):
            # 超时保护
            if time.time() - baostock_start > BAOSTOCK_TIMEOUT:
                logger.warning(f"Baostock 批量获取超时 ({BAOSTOCK_TIMEOUT}s)，已获取 {success_count} 只")
                break

            # Baostock 代码格式: sh.600000 / sz.000001 / bj.920xxx
            # 6xxxxx=上海主板, 9xxxxx 中 920xxx=北交所(应排除), 其余=上海B股
            if code.startswith("6"):
                bs_code = f"sh.{code}"
            elif code.startswith("92"):  # 北交所 (920xxx)
                bs_code = f"bj.{code}"
            elif code.startswith("9"):
                bs_code = f"sh.{code}"  # 上海B股
            else:
                bs_code = f"sz.{code}"

            try:
                rs = bs.query_history_k_data_plus(
                    bs_code,
                    "date,code,open,high,low,close,preclose,volume,amount,turn,pctChg",
                    start_date=start_date, end_date=end_date,
                    frequency="d", adjustflag="2"  # 前复权
                )
                if rs.error_code != "0":
                    fail_count += 1
                    continue

                rows = []
                while (rs.error_code == "0") & rs.next():
                    rows.append(rs.get_row_data())

                if rows:
                    df = pd.DataFrame(rows, columns=rs.fields)
                    # 类型转换
                    numeric_cols = ["open", "high", "low", "close", "preclose",
                                    "volume", "amount", "turn", "pctChg"]
                    for col in numeric_cols:
                        if col in df.columns:
                            df[col] = pd.to_numeric(df[col], errors="coerce")
                    kline_map[code] = df
                    success_count += 1
                else:
                    fail_count += 1

            except Exception as e:
                logger.debug(f"获取 {code} K线失败: {e}")
                fail_count += 1
                continue

            # 请求间隔
            if i < len(codes) - 1:
                time.sleep(BAOSTOCK_SLEEP)

        bs.logout()
        logger.info(f"Baostock K线获取: 成功{success_count}, 失败{fail_count}, "
                    f"耗时{time.time()-baostock_start:.1f}s")

    except Exception as e:
        logger.warning(f"Baostock 批量获取异常: {e}")
        try:
            bs.logout()
        except Exception:
            pass

    return kline_map


def compute_refined_factors(coarse_factor_df: pd.DataFrame,
                             kline_map: dict) -> pd.DataFrame:
    """用真实K线数据重新计算技术面因子，替换粗筛因子值

    Args:
        coarse_factor_df: Pass 1 的粗筛因子 DataFrame
        kline_map: {code: kline DataFrame}

    Returns:
        更新后的因子 DataFrame（基本面/资金面保持粗筛值，技术面被替换）
    """
    refined = coarse_factor_df.copy()
    updated_count = 0

    for code in refined.index:
        if code not in kline_map:
            continue

        kline_df = kline_map[code]
        if kline_df.empty or len(kline_df) < 20:
            continue

        try:
            tech_factors = compute_all_technical_factors(kline_df)
            for factor_name, value in tech_factors.items():
                if factor_name in refined.columns and not np.isnan(value):
                    refined.at[code, factor_name] = value
            updated_count += 1
        except Exception as e:
            logger.debug(f"计算 {code} 技术因子失败: {e}")
            continue

    logger.info(f"精算因子更新: {updated_count}/{len(refined)} 只股票的技术因子已用真实K线替换")
    return refined


# ============================================================
# 选股主逻辑（两阶段）
# ============================================================

def run_quant_screen():
    """执行两阶段量化筛选

    Stage 1: 粗筛 — 全池股票用快照因子打分，取 Top N
    Stage 2: 精算 — 对 Top N 获取K线计算真实技术因子，重新打分精选
    """
    logger.info("=" * 50)
    logger.info("📊 多因子量化选股 — 中证800+上证优选策略（两阶段）")
    logger.info("=" * 50)

    # ── 1. 获取成分股池 ──
    pool_codes = fetch_pool_stocks()
    logger.info(f"股票池: {len(pool_codes)}只（中证800+上证）")

    # ── 2. 获取市场数据 ──
    spot, breadth, index_df, code_col, name_col, pct_col, vol_col, price_col = fetch_market_data()

    # 关键列名完整性检查
    missing_cols = []
    if not code_col:
        missing_cols.append("代码/symbol")
    if not name_col:
        missing_cols.append("名称/name")
    if not price_col:
        missing_cols.append("最新价/close")
    if missing_cols:
        logger.error(f"行情数据缺少关键列: {', '.join(missing_cols)}，可能AkShare升级导致列名变化")
        return None

    # ── 3. 限定股票池 ──
    spot["_symbol"] = spot[code_col].astype(str).str.replace("sh","").str.replace("sz","").str.replace("bj","").str.replace(".SH","").str.replace(".SZ","").str.replace(".BJ","").str.zfill(6)
    spot = spot[spot["_symbol"].isin(pool_codes)]
    logger.info(f"限定池后: {len(spot)}只")

    if len(spot) < 10:
        logger.error("股票池过小，退出")
        return None

    # ── 4. 硬性过滤 ──
    filtered = apply_all_hard_filters(
        spot, name_col=name_col, pct_col=pct_col,
        volume_col=vol_col, list_date_col="list_date",
    )
    logger.info(f"硬性过滤后: {len(filtered)}只")

    if filtered.empty:
        logger.error("过滤后无可用股票")
        return None

    # ── 5. 市场择时 ──
    timing = MarketTiming(index_df).composite_signal()
    logger.info(f"市场择时: 趋势={timing.get('trend')}, "
                f"风险={timing.get('risk_level')}, "
                f"仓位={timing.get('position_advice')}")

    # ── 6. 构建映射表 ──
    codes = filtered["_symbol"].values
    price_vals = filtered[price_col].fillna(0).values
    name_vals = filtered[name_col].fillna("").values

    # 行业列（兼容多种列名）
    industry_col = None
    for c in ["行业", "industry", "所属行业"]:
        if c in filtered.columns:
            industry_col = c
            break
    industry_vals = (filtered[industry_col].fillna("其他").values
                     if industry_col
                     else np.full(len(filtered), "其他"))

    # 市值列
    mc_col = None
    for c in ["总市值", "circ_mv", "total_mv"]:
        if c in filtered.columns:
            mc_col = c
            break
    mc_vals = filtered[mc_col].fillna(0).values if mc_col else np.zeros(len(filtered))

    price_map = dict(zip(codes, price_vals))
    name_map = dict(zip(codes, name_vals))
    industry_map = dict(zip(codes, industry_vals))
    market_cap_map = dict(zip(codes, mc_vals))

    # 行业多样性检查: 全为"其他"时跳过中性化，避免 SVD 奇异矩阵
    unique_industries = set(industry_vals)
    if len(unique_industries) <= 1:
        logger.info(f"行业数据不可用（全部为'{list(unique_industries)[0] if unique_industries else '未知'}'），跳过行业/市值中性化")
        industry_series_for_screen = None
        mc_series_for_screen = None
    else:
        industry_series_for_screen = pd.Series(industry_map)
        mc_series_for_screen = pd.Series(market_cap_map)

    # ── Stage 1: 粗筛 ──
    logger.info(f"── Stage 1: 粗筛 ({len(filtered)}只 → Top {COARSE_TOP_N}) ──")
    coarse_factors = compute_coarse_factors(filtered, pct_col)

    screener_config = {
        "strategy": {
            "weights": {"technical": 0.40, "fundamental": 0.35, "capital_flow": 0.25},
            "screening": {"top_n": COARSE_TOP_N, "recommend_n": 6},
        }
    }
    screener = StockScreener(screener_config)

    # 粗筛打分
    coarse_scored = screener.score_stocks(
        coarse_factors,
        industries=industry_series_for_screen,
        market_caps=mc_series_for_screen,
    )

    if coarse_scored.empty:
        logger.error("粗筛无有效打分")
        return None

    logger.info(f"粗筛 Top 10: {[(r['ts_code'], round(r['total_score'], 3)) for _, r in coarse_scored.head(10).iterrows()]}")

    # ── Stage 2: 精算 ──
    coarse_top_codes = coarse_scored.head(COARSE_TOP_N)["ts_code"].tolist()
    logger.info(f"── Stage 2: 精算 (对 {len(coarse_top_codes)} 只候选股获取K线) ──")

    # 获取历史K线
    kline_map = fetch_kline_batch_baostock(coarse_top_codes, days=FINE_KLINE_DAYS)

    if kline_map:
        # 用真实K线重新计算技术因子
        # 构建仅含候选股的因子 DataFrame（从 coarse_factors 提取）
        candidate_factors = coarse_factors.loc[
            coarse_factors.index.isin(coarse_top_codes)
        ].copy()
        refined_factors = compute_refined_factors(candidate_factors, kline_map)
        logger.info(f"精算因子: 用真实K线更新了 {len(kline_map)} 只股票的技术因子")
    else:
        logger.warning("Baostock 未获取到K线数据，使用粗筛因子作为最终结果")
        refined_factors = coarse_factors

    # ── 最终筛选 ──
    # 熊市减半推荐
    recommend_n = 6
    if timing.get("reduce_recommend"):
        recommend_n = 3
        logger.info(f"熊市/高风险信号，推荐数减至 {recommend_n}")

    recommendations = screener.screen(
        refined_factors,
        price_map=price_map,
        name_map=name_map,
        industry_map=industry_map,
        market_cap_map=market_cap_map,
        top_n=min(50, len(refined_factors)),
        recommend_n=recommend_n,
    )

    return {
        "recommendations": recommendations,
        "market_breadth": breadth,
        "timing": timing,
        "spot_count": len(spot),
        "filtered_count": len(filtered),
        "kline_fetched": len(kline_map),
        "coarse_top_n": COARSE_TOP_N,
    }


# ============================================================
# 报告生成（Markdown格式）
# ============================================================

def format_market_cap(mv: float) -> str:
    if mv >= 1e8:
        return f"{mv/1e8:.0f}亿"
    elif mv >= 1e4:
        return f"{mv/1e4:.0f}万"
    return f"{mv:.0f}"


def generate_markdown_report(result: dict, report_date: str) -> str:
    """生成 Markdown 格式报告"""
    recs = result["recommendations"]
    breadth = result["market_breadth"]
    timing = result["timing"]

    lines = []
    lines.append(f"# 📈 每日量化精选 — {report_date}")
    lines.append("")
    lines.append(f"> **策略**: 中证800+上证多因子量化 | 技术面40% + 基本面35% + 资金面25%")
    lines.append(f"> **数据来源**: AkShare公开数据 + Baostock历史K线 | 仅供研究参考，不构成投资建议")
    lines.append("")

    # 市场概览
    lines.append("## 一、今日市场概览")
    lines.append("")
    lines.append(f"| 指标 | 数值 |")
    lines.append(f"|------|------|")
    lines.append(f"| 上涨/下跌 | {breadth['up_count']} / {breadth['down_count']} |")
    lines.append(f"| 涨停/跌停 | {breadth['limit_up']} / {breadth['limit_down']} |")
    lines.append(f"| 全市场成交额 | {breadth['total_amount']:.0f}亿 |")
    lines.append(f"| 上涨占比 | {breadth['breadth_ratio']*100:.1f}% |")
    lines.append("")

    # 择时信号
    trend_map = {"bull": "🐂 多头排列", "bear": "🐻 空头排列", "range": "↔️ 震荡整理"}
    lines.append(f"- **市场趋势**: {trend_map.get(timing.get('trend', ''), '震荡')}")
    lines.append(f"- **风险等级**: {timing.get('risk_level', '中')}")
    lines.append(f"- **择时信号**: {timing.get('signal', '谨慎')}")
    lines.append(f"- **仓位建议**: {timing.get('position_advice', '4-6成')}")
    lines.append(f"- **波动率(年化)**: {timing.get('volatility', 0)}")
    lines.append("")

    # 推荐股票
    lines.append("## 二、今日量化精选")
    lines.append("")
    stage_info = f"两阶段筛选（粗筛{result.get('coarse_top_n', 50)}只 → K线精算{result.get('kline_fetched', 0)}只 → 精选{len(recs)}只）"
    lines.append(f"共筛选 **{result['filtered_count']}** 只（中证800+上证池经硬性过滤后），{stage_info}：")
    lines.append("")

    for i, rec in enumerate(recs):
        lines.append(f"### #{rec.rank} {rec.name}（{rec.ts_code}）")
        lines.append("")
        lines.append(f"| 维度 | 得分 | 说明 |")
        lines.append(f"|------|------|------|")
        lines.append(f"| 📊 技术面 | {rec.tech_score:.2f} | MA趋势、RSI、MACD、布林带等综合判断 |")
        lines.append(f"| 📋 基本面 | {rec.fund_score:.2f} | PE/PB估值分位、ROE、利润增速 |")
        lines.append(f"| 💰 资金面 | {rec.flow_score:.2f} | 主力资金、北向资金动向 |")
        lines.append(f"| 🏆 **综合得分** | **{rec.total_score:.2f}** | |")
        lines.append("")
        lines.append(f"- **推荐理由**: {rec.reason}")
        lines.append(f"- **当前价格**: {rec.rec_price:.2f}元 | **市值**: {format_market_cap(rec.market_cap)}")
        lines.append(f"- **目标区间**: {rec.target_low:.2f} ~ {rec.target_high:.2f}元")
        lines.append(f"- **止损价**: {rec.stop_loss:.2f}元")
        lines.append(f"- **行业**: {rec.industry}")
        if rec.risk_note != "—":
            lines.append(f"- ⚠️ {rec.risk_note}")
        lines.append("")

    # 得分对比
    lines.append("## 三、推荐得分对比")
    lines.append("")
    lines.append("| 排名 | 代码 | 名称 | 总分 | 技术面 | 基本面 | 资金面 |")
    lines.append("|------|------|------|------|--------|--------|--------|")
    for rec in recs:
        lines.append(f"| {rec.rank} | {rec.ts_code} | {rec.name} | {rec.total_score:.2f} | {rec.tech_score:.2f} | {rec.fund_score:.2f} | {rec.flow_score:.2f} |")
    lines.append("")

    # 策略说明
    lines.append("## 四、策略说明")
    lines.append("")
    lines.append("本策略基于中证800+上证成分股池（沪深300+中证500+上证50+上证180），采用12因子多维度打分体系：")
    lines.append("")
    lines.append("- **技术面(40%)**: MA趋势强度、RSI超卖修复、成交量异动、MACD金叉信号、布林带位置")
    lines.append("  - 粗筛使用实时行情快照估算 → 精算使用Baostock历史K线精确计算")
    lines.append("- **基本面(35%)**: PE/PB估值分位、ROE同比变化、营收增长率、净利润增速")
    lines.append("- **资金面(25%)**: 主力资金净流入、北向资金占比变化、融资余额变化")
    lines.append("")
    lines.append("因子经MAD去极值→行业市值中性化→Z-score标准化后加权打分。")
    lines.append("同一行业最多推荐2只，确保行业分散。")
    lines.append("")

    # 免责声明
    lines.append("---")
    lines.append("")
    lines.append("⚠️ **免责声明**: 本报告由量化策略自动生成，仅供研究参考，不构成任何投资建议。")
    lines.append("股票市场存在风险，过往表现不代表未来收益。投资决策请结合个人风险承受能力。")
    lines.append(f"")
    lines.append(f"*生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 多因子量化策略 v2.0*")

    return "\n".join(lines)


# ============================================================
# 主入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="多因子量化选股（两阶段精算）")
    parser.add_argument("--publish", action="store_true", help="生成后润色并发布到公众号草稿箱")
    parser.add_argument("--dry-run", action="store_true", help="仅测试数据采集，不执行选股")
    parser.add_argument("--top", type=int, default=6, help="推荐数量（默认6）")
    parser.add_argument("--skip-kline", action="store_true", help="跳过Baostock K线精算，仅用粗筛因子")
    args = parser.parse_args()

    # 初始化
    project_root = Path("/root/daily_stock_analysis")
    load_env(project_root)
    log_dir = project_root / "logs"
    global logger
    logger = setup_script_logging("quant_picks", log_dir)

    today_str = date.today().strftime("%Y%m%d")
    report_path = project_root / "reports" / f"quant_picks_{today_str}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info("=" * 50)
    logger.info("🚀 多因子量化选股 v2.0 启动")
    logger.info(f"📅 日期: {today_str}")
    if args.skip_kline:
        logger.info("⚠️  --skip-kline 模式：跳过K线精算")
    logger.info("=" * 50)

    try:
        start_time = time.time()

        if args.dry_run:
            pool = fetch_pool_stocks()
            spot, breadth, _, _, _, _, _, _ = fetch_market_data()
            logger.info(f"✅ 数据采集测试通过: 池{len(pool)}只, 全市场{len(spot)}只, "
                        f"成交额{breadth['total_amount']:.0f}亿")
            return 0

        # ── 执行选股 ──
        if args.skip_kline:
            # 全局跳过 K 线精算
            global fetch_kline_batch_baostock, compute_refined_factors
            _orig_fetch = fetch_kline_batch_baostock
            fetch_kline_batch_baostock = lambda codes, days=90: {}
            _orig_refine = compute_refined_factors
            compute_refined_factors = lambda cf, km: cf

        try:
            result = run_quant_screen()
        finally:
            if args.skip_kline:
                fetch_kline_batch_baostock = _orig_fetch
                compute_refined_factors = _orig_refine

        if result is None or not result["recommendations"]:
            logger.warning("今日无推荐结果（可能市场环境不理想）")
            return 0

        elapsed = time.time() - start_time
        logger.info(f"选股完成，总耗时 {elapsed:.1f}s, 推荐 {len(result['recommendations'])} 只")

        # 打印结果
        for rec in result["recommendations"]:
            logger.info(f"  #{rec.rank} {rec.name}({rec.ts_code}) "
                        f"总{rec.total_score:.2f} 技{rec.tech_score:.2f} "
                        f"基{rec.fund_score:.2f} 资{rec.flow_score:.2f}")

        # ── 生成报告 ──
        report_md = generate_markdown_report(result, date.today().strftime("%Y年%m月%d日"))
        report_path.write_text(report_md, encoding="utf-8")
        logger.info(f"报告已生成: {report_path} ({len(report_md)} 字符)")

        # ── --publish: 润色 + 发布 ──
        if args.publish:
            # AI 润色
            logger.info("调用 DeepSeek 润色报告...")
            deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
            if deepseek_key:
                polish_system = (
                    "你是一名专业的财经编辑。请润色以下量化选股报告："
                    "保留所有数据和表格不变，优化文字表达使其更专业流畅。"
                    "直接返回润色后的完整Markdown。"
                )
                polished = call_deepseek(report_md, deepseek_key, system=polish_system, temperature=0.5)
                if polished:
                    content = polished.get("content", [])
                    text = ""
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text += block.get("text", "")
                    if text.strip():
                        # 备份原文
                        raw_path = Path(str(report_path).replace(".md", ".raw.md"))
                        raw_path.write_text(report_md, encoding="utf-8")
                        report_path.write_text(text.strip(), encoding="utf-8")
                        logger.info(f"润色完成，原文备份至 {raw_path}")
                    else:
                        logger.warning("DeepSeek返回空内容，使用原文")
                else:
                    logger.warning("DeepSeek调用失败，使用原文")
            else:
                logger.warning("DEEPSEEK_API_KEY未配置，跳过润色")

            # 发布到公众号草稿箱
            logger.info("发布到公众号草稿箱...")
            import subprocess
            pub_script = project_root / "scripts" / "wechat_mp_publish.py"
            title = f"每日量化精选 — {date.today().strftime('%m月%d日')}"
            result_pub = subprocess.run(
                ["python3", str(pub_script), "--report", str(report_path), "--title", title],
                capture_output=True, text=True, timeout=600,
                cwd=str(project_root),
            )
            if result_pub.returncode == 0:
                logger.info("✅ 发布成功")
            else:
                logger.error(f"发布失败 (exit={result_pub.returncode}): {result_pub.stderr[:500]}")
        else:
            logger.info("未指定 --publish，仅生成报告")

        logger.info("=" * 50)
        logger.info("✅ 量化选股完成")
        logger.info(f"📄 报告: {report_path}")
        logger.info(f"⏱  总耗时: {elapsed:.1f}s")
        logger.info("=" * 50)
        return 0

    except Exception as e:
        logger.error(f"❌ 执行异常: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    exit(main())
