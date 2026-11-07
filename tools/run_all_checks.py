#!/usr/bin/env python3
"""全量纯标准库校验聚合器（2.0 之后「梯队 0」治理能力）。

背景：CI 的 verify job 只显式接入 14 个脚本，而仓库里实际有 171 个
``tests/*.py`` + ``tools/*.py``；在纯标准库环境下实测有 142 个可直接运行，
其余 29 个因依赖 pytest / anyio / PIL / fastapi 应用依赖而无法在 verify job 运行。
验证面因此长期停留在 14/142，新增校验脚本若忘记手动接线就完全不会被跑。

本脚本把「自动发现」变成默认能力：任何新增的 tests/tools 脚本都会自动进入
全量验证面，只有写入 EXCLUDED 白名单的才会跳过（且必须注明原因）。

用法::

    python tools/run_all_checks.py [ROOT]                 # 全量运行
    python tools/run_all_checks.py [ROOT] --list          # 只打印发现 / 排除清单
    python tools/run_all_checks.py [ROOT] --audit-excluded  # 复核排除项是否仍不可跑

设计约束：
  * 只依赖标准库；CI 的 verify job 无需安装任何包。
  * 自身必须排除，否则递归调用自己。
  * 每个子进程设置 ``PYTHONDONTWRITEBYTECODE=1``，不污染工作区。
  * 单脚本超时 90s，超时视为失败并打印诊断，不静默吞掉。
  * 排除名单里出现「已不存在的文件」时报错，防止名单腐烂。
"""
from __future__ import annotations

import glob
import os
import subprocess
import sys
import time

SELF = "tools/run_all_checks.py"
TIMEOUT = 90

# 开发机 / CI 专有的绝对路径前缀。测试脚本里若写死这类路径，本机（macOS 上
# /tmp 是 /private/tmp 的符号链接）可能碰巧通过，而 CI checkout 在完全不同的
# 目录下必然在 importlib 加载阶段失败。2.1 发版时 tests/test_tgtdrive_monitor.py
# 即因此失败；此处把「路径可移植」变成全量网的硬门禁。
NONPORTABLE_PREFIXES = ("/tmp/ep_repo", "/private/tmp/ep_repo", "/home/runner/work/")

# 全量基线采集（2026-11-07，171 个脚本 / 142 可跑 / 29 失败）中退出码非 0 的
# 精确清单。失败原因全部是缺第三方依赖，不是断言失败：
#   pytest / anyio / PIL / fastapi 应用配置依赖（app.core.config 等）。
# CI verify job 刻意不装这些包（保持纯标准库、快、稳），故不纳入本网。
# 待 C 批工程环境（pytest + FastAPI + 全依赖）建成后统一接入，届时清空本表。
EXCLUDED = frozenset({
    "tests/test_image_validator.py",
    "tests/test_notification_bot_auto_finish_request_service_boundary.py",
    "tests/test_notification_bot_channel_service_boundary.py",
    "tests/test_notification_bot_check_command_service_boundary.py",
    "tests/test_notification_bot_command_registration_service_boundary.py",
    "tests/test_notification_bot_delivery_service_boundary.py",
    "tests/test_notification_bot_emby_restart_command_service_boundary.py",
    "tests/test_notification_bot_info_command_service_boundary.py",
    "tests/test_notification_bot_latest_command_service_boundary.py",
    "tests/test_notification_bot_media_helper_service_boundary.py",
    "tests/test_notification_bot_media_quality_service_boundary.py",
    "tests/test_notification_bot_message_center_callback_service_boundary.py",
    "tests/test_notification_bot_message_dispatch_service_boundary.py",
    "tests/test_notification_bot_playback_command_service_boundary.py",
    "tests/test_notification_bot_search_command_service_boundary.py",
    "tests/test_notification_bot_stats_command_service_boundary.py",
    "tests/test_notification_bot_wecom_service_boundary.py",
    "tests/test_notification_bot_whois_command_service_boundary.py",
    "tests/test_path_validator.py",
    "tests/test_plugin_scheduler_stop_events.py",
    "tests/test_pwa_router_public_auth_facade_boundary.py",
    "tests/test_rate_limiter_middleware.py",
    "tests/test_regression_fixes.py",
    "tests/test_report_film_strip_poster_layout_service_boundary.py",
    "tests/test_report_poster_fetcher_service_boundary.py",
    "tests/test_risk_router_public_auth_facade_boundary.py",
    "tests/test_system_api_tokens_public_auth_facade_boundary.py",
    "tests/test_system_repair_schema_registry.py",
    "tests/user_bot_worker_boundary.py",
})

# CI verify job 里另有单独 step 的脚本；这里仍会全量再跑一次（成本约 1 分钟），
# 换取「一个不漏」的兜底，同时保留逐项 step 的快速失败与清晰输出。
CI_EXPLICIT = (
    "tools/check-no-secrets.py",
    "tools/verify_patches.py",
    "tests/test_refactor_2_0_equivalence.py",
    "tests/test_b1_library_common_equivalence.py",
    "tests/test_b_batch_equivalence.py",
    "tools/check_security_contracts.py",
    "tests/test_sanitize_rich_html.py",
    "tools/check_session_cookie_isolation.py",
    "tests/test_tg_whitelist_sync.py",
    "tests/test_bark_push.py",
    "tools/check_bark_channel_contract.py",
    "tests/test_webhook_events.py",
    "tests/test_user_notify.py",
    "tools/check_notification_bot_subscription.py",
)


def parse_args(argv):
    root, flags = None, set()
    for arg in argv:
        if arg.startswith("--"):
            flags.add(arg)
        elif root is None:
            root = arg
    return os.path.abspath(root or "."), flags


def discover(root):
    paths = sorted(glob.glob(os.path.join(root, "tests", "*.py"))) + \
        sorted(glob.glob(os.path.join(root, "tools", "*.py")))
    return [os.path.relpath(p, root).replace(os.sep, "/") for p in paths]


def check_portability(root, scripts):
    bad = []
    for rel in scripts:
        with open(os.path.join(root, rel), "r", encoding="utf-8", errors="replace") as fp:
            src = fp.read()
        for token in NONPORTABLE_PREFIXES:
            if token in src:
                bad.append("%s -> %s" % (rel, token))
    return bad


def run_one(root, rel, env):
    argv = [sys.executable, rel]
    if rel.startswith("tools/"):
        argv.append(".")  # 与 CI 对 tools 脚本的调用方式一致
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            argv, cwd=root, env=env, capture_output=True,
            text=True, timeout=TIMEOUT, errors="replace",
        )
        elapsed = time.perf_counter() - started
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-12:]
        return proc.returncode, elapsed, " | ".join(x.strip() for x in tail)[:800]
    except subprocess.TimeoutExpired:
        return -9, TIMEOUT, "TIMEOUT(%ds)" % TIMEOUT
    except Exception as exc:  # noqa: BLE001
        return -1, 0.0, repr(exc)


def main():
    root, flags = parse_args(sys.argv[1:])
    if not os.path.isdir(os.path.join(root, "tests")):
        print("FAIL: ROOT 不是仓库根目录: %s" % root)
        return 1

    all_scripts = discover(root)
    missing = sorted(p for p in EXCLUDED if p not in all_scripts)
    if missing:
        print("FAIL: 排除名单里的文件已不存在（名单腐烂，请更新 EXCLUDED）:")
        for p in missing:
            print("  - " + p)
        return 1

    to_run = [p for p in all_scripts if p != SELF and p not in EXCLUDED]

    nonportable = check_portability(root, to_run)
    if nonportable:
        print("FAIL: 待运行脚本含开发机 / CI 专有绝对路径，CI checkout 路径不同必然失败:")
        for item in nonportable:
            print("  - " + item)
        return 1

    print("== 全量纯标准库校验网 ==")
    print("ROOT      : %s" % root)
    print("discovered: %d  excluded: %d  self: 1  to_run: %d"
          % (len(all_scripts), len(EXCLUDED), len(to_run)))
    print("CI 单独 step: %d（仍在本网全量再跑一次）" % len(CI_EXPLICIT))

    if "--list" in flags:
        for p in to_run:
            print("  RUN      " + p)
        for p in sorted(EXCLUDED):
            print("  EXCLUDED " + p)
        return 0

    if "--audit-excluded" in flags:
        print("\n-- 复核排除项（能跑的应移出 EXCLUDED）--")
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        revived = []
        for p in sorted(EXCLUDED):
            rc, _, err = run_one(root, p, env)
            if rc == 0:
                revived.append(p)
                print("  NOW OK   %-70s %s" % (p, err[:100]))
        print("\n可移出排除名单: %d / %d" % (len(revived), len(EXCLUDED)))
        return 0

    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    failed = []
    print("")
    for idx, rel in enumerate(to_run, 1):
        rc, elapsed, err = run_one(root, rel, env)
        if rc == 0:
            print("[%3d/%3d] OK   %5.2fs  %s" % (idx, len(to_run), elapsed, rel))
        else:
            failed.append((rel, rc, err))
            print("[%3d/%3d] FAIL %5.2fs  %s   rc=%s" % (idx, len(to_run), elapsed, rel, rc))
        sys.stdout.flush()

    print("\npassed: %d  failed: %d  excluded: %d"
          % (len(to_run) - len(failed), len(failed), len(EXCLUDED)))
    if failed:
        print("\n-- 失败诊断 --")
        for rel, rc, err in failed:
            print("\n%s (rc=%s)" % (rel, rc))
            print("  " + err.replace("\n", "\n  "))
        return 1
    print("ALL PURE-STDLIB CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
