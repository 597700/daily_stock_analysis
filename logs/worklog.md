# 工作日志

> 最后更新: 2026-06-29 20:15 CST

---

## 2026-06-29 问题记录与修复

### 已修复
| # | 问题 | 严重度 | 模块 | 修复 |
|---|------|--------|------|------|
| 1 | BrokenPipeError: tqdm 非TTY崩溃 | 高 | quant_daily_picks | TQDM_DISABLE=1 |
| 2 | BrokenPipeError: 同上 | 高 | daily_hotspot | 同上 |
| 3 | BrokenPipeError: 同上 | 高 | zgnb_daily_picks | 同上 |
| 4 | DeepSeek 润色返回非JSON | 中 | polish_report | markdown检测fallback |

### 待处理
| # | 问题 | 严重度 | 模块 | 现象 |
|---|------|--------|------|------|
| 5 | SVD 不收敛 | 低 | quant | 行业中性化x10次/run |
| 6 | Baostock K线慢 | 中 | quant | 80只~149s |
| 7 | DLASCL 参数错误 | 低 | quant | LAPACK警告x22 |
| 8 | 限定池偶发0只 | 中 | quant | 代码格式匹配不稳定 |
| 9 | zgnb干跑超时 | 低 | validate | 阈值偏紧 |

### 备份
- scripts/quant_daily_picks.py.bak
- scripts/polish_report.py.bak

### 今日状态
- Cron 19:00 正常，4份报告全生成
- 公众号草稿箱全部发布
- 磁盘31G / 内存1.1G
