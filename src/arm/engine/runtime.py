"""Runtime Engine（T11）：感知 → 决策 → 控制 的主循环。

职责（Architecture §1）：
- 周期 poll：读 agent_state（感知层 adapter 写入）、电源/待机/网络快照。
- 状态机：idle 超时判 FINISHED；ProtectionMachine 决定 DISARMED/ARMED/PROTECTING。
- 策略：policy.decide 给出是否保活。
- 控制：根据决策 enter/exit ExecutionStateGuard，并对活跃 agent 进程禁节流。
- 持久化：保护状态写回 store.protection，供 status 跨进程读取。

单实例：通过 single_instance("engine") 锁，避免多个 engine 抢电源控制。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Optional

from arm.control.execution_state import ExecutionStateGuard
from arm.control.modern_standby import disable_throttling, restore_throttling
from arm.core.logging_util import get_logger
from arm.core.single_instance import SingleInstanceError, single_instance
from arm.core.store import Store
from arm.engine.policy import decide
from arm.engine.state_machine import Protection, ProtectionMachine
from arm.sensors import power as power_sensor


def _claude_running() -> bool:
    """进程探测兜底（模块级引用便于测试 mock）。"""
    try:
        from arm.adapters.claude import claude_running

        return claude_running()
    except Exception:
        return False


def _codex_active() -> bool:
    """Codex 信号（rollout busy 或进程在跑）——模块级引用便于测试 mock。"""
    try:
        from arm.sensors.codex_transcript import busy_codex_sessions

        if any(x["busy"] for x in busy_codex_sessions()):
            return True
    except Exception:
        pass
    try:
        from arm.adapters.codex import codex_task_process_running

        return codex_task_process_running()
    except Exception:
        return False


@dataclass
class EngineConfig:
    poll_interval_s: float = 2.0      # 主循环节拍
    finish_grace_s: float = 120.0     # idle 超时判 FINISHED
    away_mode: bool = True            # 是否启用 Away Mode（S0 合盖保活）
    heartbeat_timeout_s: float = 30.0 # 心跳超时：超过则视为 daemon 已死，状态失效
    session_timeout_s: float = 1800.0 # 会话超时：RUNNING 超过此时长无事件 → 僵尸回收
    hooks_check_interval_s: float = 60.0  # hooks 哨兵检查间隔
    prune_interval_s: float = 6 * 3600.0  # 历史数据清理周期（arm.db 无界增长治理）


class RuntimeEngine:
    def __init__(self, store: Optional[Store] = None, config: Optional[EngineConfig] = None,
                 guard: Optional[ExecutionStateGuard] = None) -> None:
        self.store = store or Store()
        self.config = config or EngineConfig()
        self.guard = guard or ExecutionStateGuard(away_mode=self.config.away_mode)
        self.machine = ProtectionMachine()
        self._running = False
        self._throttled_pids: set[int] = set()
        self._last_store_intent: Optional[str] = None  # 跨进程意图同步（_sync_with_store）
        self._last_hooks_check: float = 0.0
        self._hooks_seen_ok: bool = False
        self._last_prune: float = 0.0
        self._last_network_warning: float = 0.0

    # ---- 历史数据清理 ----
    def _maybe_prune(self) -> None:
        """低频清理 arm.db 历史（默认每 6h）：事件/环境事件留 30 天，
        终态会话留 14 天。RUNNING 永不动。防止无界增长拖慢每拍全表读。"""
        now = time.time()
        if now - self._last_prune < self.config.prune_interval_s:
            return
        self._last_prune = now
        try:
            r = self.store.prune()
            if any(r.values()):
                get_logger().info("prune: %s", r)
        except Exception as exc:
            get_logger().warning("prune failed: %s", exc)

    # ---- 状态读取 ----
    def _reap_stale_sessions(self) -> None:
        """回收僵尸会话 + idle 超时判完成（问题#1/#2 修复）。

        两个超时，两个用途：
        - RUNNING/idle 超过 finish_grace_s（120s）无任何事件 → 本任务单元已结束
          （Stop 后用户没继续），置 FINISHED → daemon 释放保护（AC5：完成即释放）。
        - RUNNING/busy 超过 session_timeout_s（30min）无任何事件 → SessionEnd 漏发
          （Claude 崩溃/杀进程），置 STOPPED 防止"永远保护不释放"。

        busy 的正常时长可能远超 30min 吗？Claude 一轮响应通常 < 10min；
        若单轮真超 30min 且无任何 hook 事件，误判代价（需重开会话）远小于
        永不释放的代价。且收到任何新事件都会刷新 updated_ts 重新计时。
        """
        now = time.time()
        for a in self.store.get_agent_states():
            if a["state"] != "RUNNING":
                continue
            age = now - a["updated_ts"]
            if a["substate"] == "idle" and age > self.config.finish_grace_s:
                self.store.upsert_agent_state(
                    session_id=a["session_id"], source=a["source"], state="FINISHED",
                    ts=now,
                )
            elif a["substate"] == "busy" and age > self.config.session_timeout_s:
                self.store.upsert_agent_state(
                    session_id=a["session_id"], source=a["source"], state="STOPPED",
                    ts=now,
                )

    def _any_agent_active(self) -> bool:
        """是否有活跃 agent —— 多信号合并（宁可误保护，不可漏保护）。

        信号 ①hooks（精准）：库里的 agent_state 有 RUNNING。
        信号 ②transcript 忙闲（桌面会话关键）：Desktop 内嵌引擎不触发
             UserPromptSubmit/Stop（2026-09-28 实测：桌面会话只有 SessionStart
             落库）——仅靠 hooks 会在长桌面会话中途漏保护。本轮活跃
             （transcript 静默 <90s）即算活跃。
        信号 ③进程探测（兜底）：hooks 未加载/被冲掉的旧会话（用户"正跑任务
             就要合盖走人"的核心场景）——只要 Claude Code 进程存在就算活跃。
        """
        for a in self.store.get_agent_states():
            if a["state"] == "RUNNING":
                return True
        try:
            from arm.sensors.transcript import busy_sessions

            if any(x["busy"] for x in busy_sessions()):
                return True
        except Exception:
            pass
        # 兜底：Claude Code 进程（排除 Desktop 聊天应用）
        if _claude_running():
            return True
        # 兜底（T16）：Codex rollout 有 BUSY 会话，或 Codex 进程在跑
        return _codex_active()

    def _hooks_sentinel(self) -> None:
        """hooks 自动哨兵（根治"cc-switch 覆盖配置 → hooks 丢失 → 裸奔"）。

        每 hooks_check_interval_s 检查一次 settings.json 的 hooks 是否在位；
        缺失则自动重注入（install 幂等、先备份，安全）。

        "是否该自动修"的判据：用户是否 init 过 —— 以持久证据为准：
        备份文件 settings.arm-backup.json 存在，或 daemon 曾见过 OK。
        （重启后 daemon 内存态丢失，靠备份文件兜底判断。）
        用户跑过 init --undo → 抑制标记在，永远跳过（尊重卸载意图）。
        """
        now = time.time()
        if now - self._last_hooks_check < self.config.hooks_check_interval_s:
            return
        self._last_hooks_check = now
        try:
            from arm.core import claude_hooks as ch

            hk = ch.hooks_installed()
            if hk["installed"]:
                self._hooks_seen_ok = True
                return
            if ch.suppress_marker_path().exists():
                return  # 用户明确卸载过：不再自动修（长驻进程内存学不到，靠标记）
            initialized = self._hooks_seen_ok or ch.default_backup_path(
                ch.default_settings_path()).exists()
            if not initialized:
                # 从未 init 过：不擅自动配置
                get_logger().info("hooks sentinel: 尚未 init 过，跳过自动修复")
                return
            get_logger().warning("hooks sentinel: 检测到 hooks 被移除（第三方覆盖?），自动重注入")
            ch.install(backup=True)
            self._hooks_seen_ok = True
            self.store.record_agent_event(
                source="arm_engine", event="HOOKS_REPAIRED", semantic=None,
                session_id=None,
                payload={"missing": hk["missing"]},
            )
            get_logger().info("hooks sentinel: 重注入完成")
        except Exception as exc:
            get_logger().warning("hooks sentinel error: %s", exc)

    def _active_pids(self) -> list[int]:
        """活跃 agent 的 pid（L3 修复 3.2：防节流接真实 PID）。"""
        try:
            from arm.adapters.claude import detect_claude_processes

            return [p.pid for p in detect_claude_processes()]
        except Exception:
            return []

    def _reconcile_with_transcripts(self) -> None:
        """transcript 旁路对账（T14：Stop 丢失自愈，L2 修复 2.1）。

        hooks 会丢（S0 下实测 Stop 未落库）。transcript 的 mtime 是可靠旁路：
        - RUNNING/idle 会话若 transcript 已静默超过 finish_grace_s → 判 FINISHED
          （修"任务完成了但 hooks Stop 丢失→永远显示运行中"）。
        - RUNNING/busy 会话若 transcript 静默超过 session_timeout_s → 判 STOPPED
          （busy 判定的旁路版，与 hooks 超时同参）。
        transcript mtime 会因任意新消息刷新，所以"静默"语义与 hooks 一致且更可靠。
        """
        try:
            from arm.sensors.transcript import scan_transcripts

            mtime_by_sid = {t.session_id: t.mtime for t in scan_transcripts(max_age_s=7 * 86400)}
        except Exception:
            return
        now = time.time()
        for a in self.store.get_agent_states():
            if a["state"] != "RUNNING" or not a["session_id"]:
                continue
            mt = mtime_by_sid.get(a["session_id"])
            if mt is None:
                continue  # 无 transcript（非 Claude 会话，如 Codex），跳过
            silence = now - mt
            if a["substate"] == "busy" and silence < self.config.session_timeout_s:
                # 桌面会话不发 UserPromptSubmit/Stop，updated_ts 停在 SessionStart；
                # transcript 热着就摸一下心跳，防止 _reap 把活跃会话误收僵尸
                # （必须在 _reap 之前跑，tick 里已调整顺序）
                if now - a["updated_ts"] > self.config.heartbeat_timeout_s:
                    self.store.upsert_agent_state(
                        session_id=a["session_id"], source=a["source"],
                        state="RUNNING", substate="busy", ts=now)
            if a["substate"] == "idle" and silence >= self.config.finish_grace_s:
                get_logger().info("transcript reconcile: %s idle %.0fs -> FINISHED",
                                  a["session_id"][:8], silence)
                self.store.upsert_agent_state(
                    session_id=a["session_id"], source=a["source"], state="FINISHED", ts=now)
            elif a["substate"] == "busy" and silence >= self.config.session_timeout_s:
                get_logger().info("transcript reconcile: %s busy %.0fs -> STOPPED",
                                  a["session_id"][:8], silence)
                self.store.upsert_agent_state(
                    session_id=a["session_id"], source=a["source"], state="STOPPED", ts=now)

    def _sync_with_store(self) -> None:
        """跨进程一致：库里的保护意图是唯一事实源（修"release 后 daemon 又自己保护"）。

        场景：用户在 UI/另一终端 release（写库 DISARMED），但 daemon 内存状态机还在
        ARMED/PROTECTING → 下一拍 tick 会覆盖回 PROTECTING，用户意愿被无视。
        规则：若库reason含"user release"且 daemon 内存≠DISARMED → 采纳释放；
              若库是 ARMED/PROTECTING 且 daemon 内存=DISARMED → 采纳开启。
        判据用"上次见到的库状态"（_last_store_intent）检测变化，避免每拍重复触发。
        """
        row = self.store.get_protection()
        stored = row["state"] if row else "DISARMED"
        reason = (row["reason"] if row else "") or ""
        if stored == self._last_store_intent:
            return  # 无外部变化
        prev = self._last_store_intent
        self._last_store_intent = stored
        if stored == "DISARMED" and "user release" in reason and prev is not None:
            self.machine.disarm()
        elif stored in ("ARMED", "PROTECTING") and self.machine.state == Protection.DISARMED:
            self.machine.arm()

    # ---- 控制执行 ----
    def _apply(self, should_protect: bool, reason: str) -> None:
        if should_protect and not self.guard.active:
            self.guard.enter()
            for pid in self._active_pids():
                if disable_throttling(pid):
                    self._throttled_pids.add(pid)
            # T15 电源组合拳：快照并钉住睡眠/休眠/关屏为"永不"（保活确定性）
            try:
                from arm.control import power_policy

                r = power_policy.apply_pinned()
                get_logger().info("power policy pinned: %s", r)
            except Exception as exc:
                get_logger().warning("power policy pin failed: %s", exc)
        elif not should_protect and self.guard.active:
            for pid in self._throttled_pids:
                restore_throttling(pid)
            self._throttled_pids.clear()
            self.guard.exit()
            # T15：还原用户原电源配置（快照恢复，可逆）
            try:
                from arm.control import power_policy

                r = power_policy.restore_original()
                get_logger().info("power policy restored: %s", r)
            except Exception as exc:
                get_logger().warning("power policy restore failed: %s", exc)

    # ---- 主循环一拍（可单测）----
    def tick(self) -> dict:
        """执行一次 感知→决策→控制。返回决策快照（供 status/日志/测试）。"""
        p = power_sensor.snapshot()
        try:
            from arm.sensors import network as network_sensor

            network = network_sensor.snapshot()
        except Exception:
            network = {"status": "unknown", "up": None, "reason": "网络传感器不可用"}
        if network.get("up") is False and time.time() - self._last_network_warning >= 60.0:
            get_logger().warning("network unavailable: %s", network.get("reason"))
            self._last_network_warning = time.time()
        self._maybe_prune()                  # 历史数据低频清理
        self._hooks_sentinel()               # hooks 哨兵：被第三方工具冲掉自动重注入
        self._reconcile_with_transcripts()   # transcript 旁路对账：先救活/判定
        self._reap_stale_sessions()          # 再回收 transcript 也救不回的僵尸
        self._sync_with_store()              # 采纳其它进程的 arm/release（跨进程一致）
        any_active = self._any_agent_active()
        prot = self.machine.update(any_active)
        d = decide(prot, any_active, power=p, network_up=network.get("up"))

        self._apply(d.should_protect, d.reason)
        prev_prot = self.store.get_protection()
        prev_state = prev_prot["state"] if prev_prot else "DISARMED"
        self.store.set_protection(prot.value, reason=d.reason)
        self.store.beat()  # daemon 心跳：独立于 protection 状态，供 UI/status 判活
        # 保护动作日志：状态变迁时记入事件流 + 文件日志（UI 紫色徽章 / arm.log 回溯）
        log = get_logger()
        if prot.value != prev_state:
            log.info("protection %s -> %s (%s)", prev_state, prot.value, d.reason)
            action = "PROTECT_START" if prot.value == "PROTECTING" else (
                "PROTECT_STOP" if prev_state == "PROTECTING" else
                ("PROTECT_ARM" if prot.value == "ARMED" else "PROTECT_DISARM"))
            self.store.record_agent_event(
                source="arm_engine", event=action,
                semantic=prot.value, session_id=None,
                payload={"from": prev_state, "to": prot.value, "reason": d.reason},
            )
        return {
            "protection": prot.value,
            "should_protect": d.should_protect,
            "guard_active": self.guard.active,
            "any_agent_active": any_active,
            "reason": d.reason,
            "network": network,
            "warnings": list(d.warnings),
        }

    # ---- 对外控制 ----
    def arm(self) -> None:
        self.machine.arm()
        self.tick()

    def release(self) -> None:
        """释放保护：解除保活 + 恢复节流 + 还原电源 + 状态落库。

        电源还原必须在这里做（run() 的 finally 只走本函数，没有下一拍 tick）：
        漏掉会导致引擎正常停止时快照滞留、电源值钉死在"永不"
        （2026-09-28 事故：app 退出后四项电源值滞留全 0）。
        """
        self.machine.disarm()
        for pid in self._throttled_pids:
            restore_throttling(pid)
        self._throttled_pids.clear()
        if self.guard.active:
            self.guard.exit()
        try:
            from arm.control import power_policy

            power_policy.restore_original()
        except Exception:
            pass
        self.store.set_protection(Protection.DISARMED.value, reason="user release")

    def run(self) -> None:
        """长驻主循环（单实例）。Ctrl+C 退出并确保释放。

        启动即 arm（二态模型：app 在=执勤，退出=休息，2026-09-28 定调）。

        释放保障（多层兜底）：
        - finally: 正常循环结束
        - KeyboardInterrupt: 终端 Ctrl+C
        - signal SIGINT/SIGTERM: 被杀信号（Windows 控制台语义下 SIGINT 不一定
          转成 KeyboardInterrupt，故显式注册 handler）
        """
        import signal

        def _on_signal(signum, frame):
            self._running = False

        try:
            signal.signal(signal.SIGINT, _on_signal)
        except Exception:
            pass
        try:
            signal.signal(signal.SIGTERM, _on_signal)
        except Exception:
            pass

        try:
            with single_instance("engine"):
                self.machine.arm()  # 启动即布防（2026-09-28 定调：app 在=执勤，二态模型）
                get_logger().info("daemon started (pid=%s)", os.getpid())
                # T15 崩溃自愈：上次 daemon 硬杀可能留下"已钉住未恢复"的电源快照
                try:
                    from arm.control import power_policy

                    rec = power_policy.recover_if_stranded()
                    if rec.get("recovered"):
                        get_logger().info("power policy: 恢复了滞留的快照（上次异常退出）")
                except Exception:
                    pass
                self._running = True
                consecutive_fail = 0
                try:
                    while self._running:
                        try:
                            self.tick()
                            consecutive_fail = 0
                        except Exception:
                            # tick 级容错：单次异常（如 DB 锁抖动）不终结引擎——
                            # 带病运行好过 90s 保护盲区（等 _ensure_daemon 才接管）
                            consecutive_fail += 1
                            get_logger().warning(
                                "tick 异常（连续第 %s 次）", consecutive_fail, exc_info=True)
                            if consecutive_fail >= 5:
                                time.sleep(30.0)  # 连续失败退避，避免疯狂重试
                                continue
                        time.sleep(self.config.poll_interval_s)
                except KeyboardInterrupt:
                    pass
                finally:
                    get_logger().info("daemon stopping, releasing protection")
                    self.release()
        except SingleInstanceError:
            # 已有 engine 在跑
            return

    def stop(self) -> None:
        self._running = False
