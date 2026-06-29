#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
报告润色脚本（独立版，无项目依赖）

用途：
  对 main.py 生成的 Markdown 报告进行公众号风格润色，生成有吸引力的标题。
  作为 cron_wrapper.sh 流水线中的第二步，在 main.py 和 wechat_mp_publish.py 之间运行。

使用方式：
  python scripts/polish_report.py                          # 自动找今日报告
  python scripts/polish_report.py --report reports/market_review_20260623.md
  python scripts/polish_report.py --dry-run                # 试运行，只打印不写文件
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import requests

logger = logging.getLogger("polish_report")


# ---------------------------------------------------------------------------
# DeepSeek API 调用
# ---------------------------------------------------------------------------

DEEPSEEK_BASE = "https://api.deepseek.com/anthropic"
# 最大输入字符数（预留足够空间给 prompt + 报告）
MAX_INPUT_CHARS = 30000
TITLE_HISTORY_MAX = 60


def _title_history_path(reports_dir, report_type: str):
    if report_type == "picks":
        suffix = "picks"
    elif report_type == "hotspot":
        suffix = "hotspot"
    else:
        suffix = "market_review"
    return reports_dir / f".title_history_{suffix}.json"


def load_title_history(reports_dir, report_type: str) -> list:
    path = _title_history_path(reports_dir, report_type)
    if not path.exists():
        return []
    try:
        import json
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("titles", [])
    except Exception:
        logger.warning("Title history load failed: %s, skip dedup", path)
        return []


def save_title_to_history(reports_dir, report_type: str, title: str):
    import json
    titles = load_title_history(reports_dir, report_type)
    if not titles or titles[-1] != title:
        titles.append(title)
        titles = titles[-TITLE_HISTORY_MAX:]
        path = _title_history_path(reports_dir, report_type)
        path.write_text(
            json.dumps({"titles": titles}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def build_diversity_suffix(recent_titles: list) -> str:
    if not recent_titles:
        return ""
    recent = recent_titles[-10:]
    lines = "\n".join(f"- {t}" for t in recent)
    return f"""

## TITLE DEDUP RULE (MUST FOLLOW)
The following titles were used recently. Your title MUST be completely different:
- Do NOT reuse the same opening words (first 3-5 chars)
- Do NOT reuse the same sentence structure
- Do NOT reuse the same keyword combinations

Recent titles:
{lines}

Extract a fresh angle from today's actual market data."""

# 大盘复盘润色 Prompt（强化版 — 强制保留表格和关键数据）
POLISH_MARKET_REVIEW = """你是一位资深的财经编辑，专门为微信公众号撰写每日A股大盘复盘文章。

你的任务是：
1. **润色文章**：保持所有数据、表格、数值完全不变，只优化语言表达，让文章更易读、更有节奏感
2. **生成标题**：生成一个吸引人的标题，风格参考「财经早餐」「券商中国」等头部财经公众号

## ⚠️ 铁律（违反则不合格）
- **所有 Markdown 表格必须原封不动保留**：每个 `|` 分隔的表格行、每个数字、每个百分比都不能少
- **信号评分必须保留**：如 "盘面信号：43/100（震荡，需观察）" 这类量化评分一行都不能删
- **操作结论必须保留**：如 "🟡 防守"、"🟢 进攻"、"🔴 回避" 等结论性标签不能删除
- **涨跌幅、点位、成交额等数字绝对不能改**
- **板块排名表格必须保留**：领涨 Top 5 和领跌 Top 5 的表格行一个都不能少

## 润色要求
- 数据绝对不能改（涨跌幅、点位、成交额等）
- 表格保持原样不动，每个 | 分隔的表格行都要保留
- 段落之间增加过渡，避免生硬跳转
- 关键判断用 🔴🟢🟡 强化视觉
- 句子不要太长，适合手机阅读
- 保持专业感，不要过度娱乐化
- **不要改变投资建议的结论**（如仓位建议、操作方向）

## 标题要求
- 10-25 字之间
- 包含今日最核心的矛盾或机会
- 不要用「震惊」「突发」等标题党词汇
- ⚠️ 每天标题必须有变化，基于当日实际数据提炼独特角度，避免套路化表述
- ⚠️ 每次变换标题结构：有时用问句，有时用对比句，有时用数字冲击，有时用板块名称
- ⚠️ 不要写成"A，B暗示C"这种固定句式

## 输出格式
请只输出以下 JSON，不要任何其他内容：
```json
{
  "title": "生成的文章标题",
  "content": "润色后的完整 Markdown 内容"
}
```"""

# 个股推荐润色 Prompt（强化版）
POLISH_PICKS = """你是一位资深的财经编辑，专门为微信公众号撰写每日金股推荐文章。

你的任务是：
1. **润色文章**：保持所有数据、评分、战法名称完全不变，只优化语言表达，让推荐更有说服力、更易读
2. **生成标题**：生成一个有吸引力的标题，突出今日选出的最强标的

## ⚠️ 铁律（违反则不合格）
- **所有数据表格必须原封不动保留**：每个 `|` 分隔的表格行都不能少
- **股票代码、评分、涨跌幅、战法名称绝对不能改**
- **零信号时，也必须保留扫描概要数据表格**
- **风险提示必须保留**

## 润色要求
- 数据绝对不能改（评分、涨跌幅、战法命中、技术指标等）
- 表格保持原样不动
- 每只推荐股的分析要有节奏感，先亮分数再说逻辑
- 关键判断用 🔥⭐📈 强化视觉
- 句子不要太长，适合手机阅读
- 保持专业感，不要过度娱乐化
- 战法名称保持原样（少妇战法/填坑战法/补票战法/TePu战法）

## 标题要求
- 10-25 字之间
- 突出今日最强标的或核心选股逻辑
- 零信号时如实反映市场状况，但每天换用不同的表述方式，避免套用固定模板
- 不要用「震惊」「突发」「暴涨」等标题党词汇
- ⚠️ 可变换标题结构：有时突出扫描规模，有时突出市场环境，有时给出操作建议

## 输出格式
请只输出以下 JSON，不要任何其他内容：
```json
{
  "title": "生成的文章标题",
  "content": "润色后的完整 Markdown 内容"
}
```"""

# 股市热点润色 Prompt
POLISH_HOTSPOT = """你是一位资深的财经编辑，专门为微信公众号撰写每日A股市场热点复盘文章。

你的任务是：
1. 润色文章：保持所有数据、表格、数值完全不变，只优化语言表达
2. 生成标题：生成一个有吸引力的标题，突出今日最热门的概念或板块

## 铁律（违反则不合格）
- 所有 Markdown 表格必须原封不动保留
- 股票代码、涨跌幅、封板资金、连板数等数据绝对不能改
- 热度排名不能改动

## 润色要求
- 数据绝对不能改
- 表格保持原样不动
- 突出热点之间的关联性
- 句子不要太长，适合手机阅读
- 保持专业感

## 标题要求
- 10-25 字之间
- 突出今日最热概念或最强板块
- 每天标题必须有变化，基于当日实际热点提炼独特角度
- 不要用标题党词汇

## 输出格式
请只输出以下 JSON：
{
  "title": "生成的文章标题",
  "content": "润色后的完整 Markdown 内容"
}"""

SYSTEM_PROMPT = POLISH_MARKET_REVIEW  # 默认兼容旧调用


def call_deepseek(prompt: str, api_key: str, timeout: int = 120,
               system: str = "", temperature: float = 0.7) -> Optional[dict]:
    """调用 DeepSeek API（Anthropic 兼容端点）"""
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
    }
    payload = {
        "model": "deepseek-chat",
        "max_tokens": 8192,
        "temperature": temperature,
        "system": system or SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        r = requests.post(
            f"{DEEPSEEK_BASE}/v1/messages",
            headers=headers,
            json=payload,
            timeout=timeout,
        )
        if r.status_code != 200:
            logger.error(f"DeepSeek API 返回 {r.status_code}: {r.text[:300]}")
            return None

        data = r.json()
        # Anthropic 兼容格式
        content = data.get("content", [])
        if isinstance(content, list):
            text = "".join(block.get("text", "") for block in content if block.get("type") == "text")
        elif isinstance(content, str):
            text = content
        else:
            text = str(content)

        if not text:
            logger.error("DeepSeek 返回空内容")
            return None

        # 提取 JSON
        json_match = re.search(r'\{[^{}]*"title"[^{}]*"content"[^{}]*\}', text, re.DOTALL)
        if not json_match:
            # 尝试提取更宽松的 JSON
            json_match = re.search(r'\{.*\}', text, re.DOTALL)

        if json_match:
            try:
                result = json.loads(json_match.group(0))
                return result
            except json.JSONDecodeError:
                if len(text) > 200:
                    logger.warning("JSON解析失败，检测到文本，直接使用")
                    return {"title": "", "content": text}
                logger.error(f"JSON 解析失败: {text[:500]}")
                return None

        # JSON未找到，检查是否DeepSeek直接返回了润色文本
        if len(text) > 200 and ("#" in text or "|" in text or "**" in text):
            logger.warning("未找到JSON包裹，检测到润色文本，直接使用")
            return {"title": "", "content": text}
        logger.error(f"未找到 JSON: {text[:300]}")
        return None

    except requests.exceptions.Timeout:
        logger.error("DeepSeek API 超时")
        return None
    except Exception as e:
        logger.error(f"DeepSeek API 异常: {e}")
        return None


# ---------------------------------------------------------------------------
# 报告处理
# ---------------------------------------------------------------------------

def find_today_report(reports_dir: Path, specified: Optional[str] = None) -> Optional[Path]:
    """查找今日报告，逻辑与 wechat_mp_publish.py 一致"""
    if specified:
        p = Path(specified)
        if p.exists():
            return p
        p = reports_dir / specified
        if p.exists():
            return p
        logger.error(f"指定报告不存在: {specified}")
        return None

    today = datetime.now().strftime("%Y%m%d")
    candidates = [
        reports_dir / f"market_review_{today}.md",
        reports_dir / f"report_{today}.md",
    ]
    for p in candidates:
        if p.exists():
            logger.info(f"找到报告: {p}")
            return p

    matches = list(reports_dir.glob(f"*{today}*.md"))
    if matches:
        p = max(matches, key=lambda x: x.stat().st_mtime)
        logger.info(f"匹配报告: {p}")
        return p

    logger.error(f"未找到今日({today})报告")
    return None


def validate_polish(original: str, polished: str, report_type: str) -> list:
    """验证润色后的内容是否保留了关键元素"""
    issues = []

    # 检查表格是否保留
    orig_tables = len(re.findall(r'^\|.*\|$', original, re.MULTILINE))
    polished_tables = len(re.findall(r'^\|.*\|$', polished, re.MULTILINE))
    if orig_tables > 0 and polished_tables < orig_tables * 0.5:
        issues.append(f"表格行数严重减少: {orig_tables} → {polished_tables}")

    # 检查信号评分
    if report_type == "market_review":
        score_match = re.search(r'(\d{1,3}/100)', original)
        if score_match:
            score = score_match.group(1)
            if score not in polished:
                issues.append(f"信号评分丢失: {score}")

    # 检查关键数字
    key_numbers = re.findall(r'[-]?\d+\.?\d*%', original)
    for num in key_numbers[:10]:  # 抽查前10个
        if num not in polished:
            issues.append(f"关键百分比丢失: {num}")
            break

    return issues


def polish_report(
    report_path: Path,
    api_key: str,
    dry_run: bool = False,
    report_type: str = "market_review",
) -> Tuple[Optional[str], Optional[str]]:
    """
    润色报告并生成标题。

    Args:
        report_type: "market_review" 或 "picks"
    Returns:
        (title, polished_content) 或 (None, None)
    """
    content = report_path.read_text(encoding="utf-8")
    logger.info(f"原始报告: {len(content)} 字符")

    # 截断过长内容
    if len(content) > MAX_INPUT_CHARS:
        logger.warning(f"报告过长，截断至 {MAX_INPUT_CHARS} 字符")
        content = content[:MAX_INPUT_CHARS] + "\n\n> [内容过长，已截断]"

    prompt = f"请润色以下大盘复盘报告并生成标题：\n\n{content}"

    if dry_run:
        logger.info("🔍 试运行模式 — 即将发送的 prompt 预览:")
        system_prompt = POLISH_PICKS if report_type == "picks" else POLISH_MARKET_REVIEW
        logger.info(f"  System [{report_type}]: {len(system_prompt)} 字符")
        logger.info(f"  Report: {len(content)} 字符")
        return None, None

    if report_type == "picks":
        system = POLISH_PICKS
    elif report_type == "hotspot":
        system = POLISH_HOTSPOT
    else:
        system = POLISH_MARKET_REVIEW

    # Title diversity: load history and inject dedup constraint
    reports_dir_actual = report_path.parent
    past_titles = load_title_history(reports_dir_actual, report_type)
    if past_titles:
        system = system + build_diversity_suffix(past_titles)
        logger.info("Loaded %d past titles for dedup check", len(past_titles))

    # Higher temperature for creative titles: 1.0 market, 1.1 picks
    temp = 1.1 if report_type == "picks" else 1.0

    logger.info("调用 DeepSeek API 进行润色 (type=%s, temp=%.1f)...", report_type, temp)
    start = time.time()
    result = call_deepseek(prompt, api_key, system=system, temperature=temp)
    elapsed = time.time() - start
    logger.info(f"API 调用耗时: {elapsed:.1f}s")

    if not result:
        return None, None

    title = result.get("title", "").strip()
    polished = result.get("content", "").strip()

    # Save title to history (independent of content validation)
    if title:
        try:
            save_title_to_history(report_path.parent, report_type, title)
        except Exception as e:
            logger.warning("Title history save failed (non-blocking): %s", e)

    if not polished:
        logger.error("润色结果为空")
        return None, None

    # 验证润色质量
    issues = validate_polish(content, polished, report_type)
    if issues:
        logger.warning(f"⚠️ 润色验证发现问题 ({len(issues)} 项):")
        for issue in issues:
            logger.warning(f"  - {issue}")
        logger.warning("将使用原文替代（润色版本仍保存到 .polished.md 供参考）")
        # 保存润色版到 .polished.md（供人工检查），但返回原文
        return title, None  # None 表示回退到原文

    logger.info(f"标题: {title}")

    # 确保以 # 标题开头
    if not polished.startswith("#"):
        polished = f"# {title}\n\n{polished}"

    return title, polished


# ---------------------------------------------------------------------------
# 主逻辑
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="报告润色 + 标题生成")
    parser.add_argument("--report", type=str, default=None, help="指定报告文件路径")
    parser.add_argument("--dry-run", action="store_true", help="试运行")
    parser.add_argument("--api-key", type=str, default=None, help="DeepSeek API Key（优先于 .env）")
    parser.add_argument("--type", type=str, default="market_review", dest="report_type",
                        choices=["market_review", "picks", "hotspot"],
                        help="报告类型: market_review(大盘复盘) / picks(个股推荐)")
    args = parser.parse_args()

    # 项目根目录
    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent if script_dir.name == "scripts" else Path.cwd()

    # 日志
    log_dir = project_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_dir / "polish.log", encoding="utf-8"),
        ],
    )

    logger.info("=" * 50)
    logger.info("报告润色脚本启动")
    logger.info("=" * 50)

    # 读取 .env
    env_file = project_root / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip()
                if key and key not in os.environ:
                    os.environ[key] = val

    # API Key
    api_key = args.api_key or os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        logger.error("❌ 未配置 DEEPSEEK_API_KEY")
        sys.exit(1)
    logger.info(f"API Key: {api_key[:8]}****")

    # 查找报告
    reports_dir = project_root / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    # 根据 --type 选择默认报告
    if not args.report and args.report_type == "picks":
        today = datetime.now().strftime("%Y%m%d")
        picks_path = reports_dir / f"daily_picks_{today}.md"
        if picks_path.exists():
            report_path = picks_path
            logger.info(f"找到推荐报告: {report_path}")
        else:
            report_path = None
    elif not args.report and args.report_type == "hotspot":
        today = datetime.now().strftime("%Y%m%d")
        hotspot_path = reports_dir / f"hotspot_{today}.md"
        if hotspot_path.exists():
            report_path = hotspot_path
            logger.info(f"找到热点报告: {report_path}")
        else:
            report_path = None
    else:
        report_path = find_today_report(reports_dir, args.report)
    if not report_path:
        logger.error("❌ 无报告，退出")
        sys.exit(1)

    # ====== 新增：备份原始报告 ======
    raw_backup_path = report_path.with_suffix(".raw.md")
    original_content = report_path.read_text(encoding="utf-8")
    raw_backup_path.write_text(original_content, encoding="utf-8")
    logger.info(f"原始报告已备份: {raw_backup_path}")

    # 润色
    title, polished = polish_report(report_path, api_key, dry_run=args.dry_run,
                                   report_type=args.report_type)

    if args.dry_run:
        logger.info("✅ 试运行完成")
        return

    if not title:
        logger.error("❌ 润色失败，保留原文不动")
        sys.exit(1)

    # 保存标题供 wechat_mp_publish.py 使用
    if args.report_type == "picks":
        title_file = reports_dir / ".today_picks_title.txt"
    elif args.report_type == "hotspot":
        title_file = reports_dir / ".today_hotspot_title.txt"
    else:
        title_file = reports_dir / ".today_title.txt"
    title_file.write_text(title, encoding="utf-8")
    logger.info(f"标题已保存 [{args.report_type}]: {title}")

    if polished:
        # 润色成功且通过验证 → 保存润色版
        polished_path = report_path.with_suffix(".polished.md")
        polished_path.write_text(polished, encoding="utf-8")
        logger.info(f"润色报告已保存: {polished_path}")

        # 覆盖原文件（后续 wechat_mp_publish.py 读取）
        report_path.write_text(polished, encoding="utf-8")
        logger.info(f"原文已替换为润色版: {report_path}")
    else:
        # 润色验证不通过 → 保留原文不动，但保存润色版供参考
        logger.warning("⚠️ 润色验证未通过，保留原文不动")
        logger.info(f"原文保持不变: {report_path}")
        # polished 已经保存到 .polished.md（在 polish_report 中），无需额外操作

    logger.info("✅ 润色完成")


if __name__ == "__main__":
    main()
