#!/bin/bash
#
# daily_stock_analysis cron wrapper
# 提供日志管理、进程锁、内存检查、超时保护
# 三模块: 大盘复盘 + 股市热点 + 量化精选

set -euo pipefail

PROJECT_DIR="/root/daily_stock_analysis"
LOG_DIR="${PROJECT_DIR}/logs"
LOCK_FILE="/tmp/daily_stock_analysis.lock"
MAX_RUNTIME=1800
MIN_FREE_MEM_MB=200

mkdir -p "${LOG_DIR}"
LOG_FILE="${LOG_DIR}/cron_$(date +%Y%m%d).log"
ERROR_LOG="${LOG_DIR}/error.log"

log()    { echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] $*" | tee -a "${LOG_FILE}"; }
FEISHU_WEBHOOK="${FEISHU_WEBHOOK:-https://open.feishu.cn/open-apis/bot/v2/hook/78dc37ba-f6b1-424c-810b-1eaba08317d5}"
send_feishu() {
    local msg="[StockBot] $(date '+%m/%d %H:%M') $*"
    curl -s -X POST "${FEISHU_WEBHOOK}" \
        -H "Content-Type: application/json" \
        -d "$(printf '{"msg_type":"text","content":{"text":"%s"}}' "${msg}")" \
        > /dev/null 2>&1 || true
}

log_err(){ echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] ERROR: $*" | tee -a "${LOG_FILE}" "${ERROR_LOG}"; }

# 进程锁
exec 200>"${LOCK_FILE}"
if ! flock -n 200; then
    log "另一个实例正在运行，本次退出。"
    exit 0
fi

# 清理旧日志
find "${LOG_DIR}" -name "cron_*.log" -mtime +30 -delete 2>/dev/null || true

# 内存检查
free_mem=$(awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo 2>/dev/null || echo "0")
[ "${free_mem}" -eq 0 ] && free_mem=$(free -m | awk '/^Mem:/ {print $4+$6+$7}')
if [ "${free_mem}" -lt "${MIN_FREE_MEM_MB}" ]; then
    log_err "可用内存不足 (${free_mem}MB < ${MIN_FREE_MEM_MB}MB)，退出以避免 OOM。"
    send_feishu "FAIL: 可用内存不足"
    exit 1
fi
log "内存检查通过: ${free_mem}MB 可用"

log "========== 开始执行每日任务 =========="
send_feishu "Daily task started"

# ── 模块1: 大盘复盘 ──
log "--- 1/7 Market Review ---"
if timeout ${MAX_RUNTIME} python3 "${PROJECT_DIR}/main.py" --market-review --force-run >> "${LOG_FILE}" 2>&1; then
    log "main.py OK"
else
    ec=$?
    [ ${ec} -eq 124 ] && log_err "main.py timeout" || log_err "main.py failed (exit=${ec})"
    exit ${ec}
fi

log "--- 2/7 Polish market review ---"
if timeout 300 python3 "${PROJECT_DIR}/scripts/polish_report.py" >> "${LOG_FILE}" 2>&1; then
    log "polish (market_review) OK"
else
    ec2=$?
    [ ${ec2} -eq 124 ] && log_err "polish (market_review) timeout" || log "polish (market_review) failed (exit=${ec2}), continue with raw"
fi

log "--- 3/7 Publish market review ---"
if timeout 600 python3 "${PROJECT_DIR}/scripts/wechat_mp_publish.py" >> "${LOG_FILE}" 2>&1; then
    log "market review published OK"
else
    ec=$?
    [ ${ec} -eq 124 ] && log_err "publish (market_review) timeout" || log_err "publish (market_review) failed (exit=${ec})"
    exit ${ec}
fi

# ── 模块2: 股市热点 ──
today=$(date +%Y%m%d)
hotspot_report="${PROJECT_DIR}/reports/hotspot_${today}.md"

log "--- 4/7 Hotspot ---"
if timeout 300 python3 "${PROJECT_DIR}/scripts/daily_hotspot.py" >> "${LOG_FILE}" 2>&1; then
    log "daily_hotspot.py OK"
else
    ec4=$?
    [ ${ec4} -eq 124 ] && log_err "daily_hotspot.py timeout" || log "daily_hotspot.py failed (exit=${ec4})"
fi

if [ -f "${hotspot_report}" ]; then
    log "--- 5/7 Polish hotspot ---"
    if timeout 300 python3 "${PROJECT_DIR}/scripts/polish_report.py" --type hotspot >> "${LOG_FILE}" 2>&1; then
        log "polish (hotspot) OK"
    else
        ec5=$?
        [ ${ec5} -eq 124 ] && log_err "polish (hotspot) timeout" || log "polish (hotspot) failed (exit=${ec5})"
    fi

    log "--- 6/7 Publish hotspot ---"
    if timeout 600 python3 "${PROJECT_DIR}/scripts/wechat_mp_publish.py" --report "${hotspot_report}" >> "${LOG_FILE}" 2>&1; then
        log "hotspot published OK"
    else
        ec6=$?
        [ ${ec6} -eq 124 ] && log_err "publish (hotspot) timeout" || log_err "publish (hotspot) failed (exit=${ec6})"
    fi
else
    log "hotspot report not found, skip publish"
fi

# ── 模块3: 量化精选 ──
log "--- 7/7 Quant picks ---"
if timeout 300 python3 "${PROJECT_DIR}/scripts/quant_daily_picks.py" --publish >> "${LOG_FILE}" 2>&1; then
    log "quant_daily_picks OK"
else
    ec7=$?
    [ ${ec7} -eq 124 ] && log_err "quant picks timeout" || log_err "quant picks failed (exit=${ec7})"
fi

log "========== 每日任务完成 =========="
send_feishu "All tasks completed"
