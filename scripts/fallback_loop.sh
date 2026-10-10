#!/bin/sh
# 每日兜底循环(Docker fallback 容器的 entrypoint)
#
# 每 5 分钟醒一次; 到 19:00 后执行一次当日兜底(脚本自身幂等, 重复调无害),
# 当日标记落在 /tmp/done_<日期>, 因此跨天会重新执行、同天不会重复。
#
# 为什么不用 cron: slim 镜像里没有 cron; 一个 sh 循环 + sleep 少一个依赖,
# 且每次循环都是全新进程, 不会积累状态。
# 为什么 19:00: 内置调度器在每交易日 15:30 跑; 兜底放其后作为当日第二道保障。

while true; do
    d=$(date +%Y%m%d)
    now=$(date +%H%M)
    if [ "$now" -ge 1900 ] && [ ! -f "/tmp/done_${d}" ]; then
        echo "[fallback-loop] $(date '+%F %T') 执行每日兜底"
        python /app/scripts/paper_daily_fallback.py || echo "[fallback-loop] 兜底返回非零, 明日同一时间前不会再跑"
        touch "/tmp/done_${d}"
    fi
    sleep 300
done
