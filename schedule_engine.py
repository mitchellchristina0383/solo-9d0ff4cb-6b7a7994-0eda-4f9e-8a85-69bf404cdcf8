"""夜间备份预约 · A/B 双线路整班排布引擎。

与 Web 框架无关：Flask 路由只做 JSON 解析与序列化，所有状态变更都先在
“候选状态”上完成整班枚举排布，成功后才提交，因此失败不会触动在班预约。

业务约束（与题目一一对应）：
* 单班最多 ``MAX_ORDERS_PER_SHIFT = 12`` 单；
* 每单（一个分支的一次备份）记录起止分钟、固定报文载荷、保底速率；
* 每单全程只走一条线（A 或 B）；
* MTU 约束：``payload + overhead_line <= mtu_line``；
* 带宽约束：同一条线在任意时刻，重叠预约的保底速率之和
  ``<= bandwidth_line``（区间左闭右开，端点相接不算重叠）。

排布器是规模固定的小问题：最多 12 单 → 至多 2^12=4096 种走线分配，
直接全枚举，保证“排得下就一定找得到”，而不是启发式碰运气。
在所有可行方案中按确定性偏好打分：尽量少改动当前走线（稳定），
其次让两线峰值利用率尽量均衡，最后用掩码做唯一平局裁决。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

# ---- 规模 / 取值限制 ----------------------------------------------------
MAX_ORDERS_PER_SHIFT = 12
MIN_MINUTE = 0
MAX_MINUTE = 24 * 60          # 一天 1440 分钟；夜班也用当天分钟表示
MAX_PAYLOAD = 65_535
MAX_RATE = 10_000_000.0       # Mbps，兜底上限，防止误输入
DEFAULT_BANDWIDTH = 100.0     # Mbps，与页面输入框默认值一致
EPS = 1e-9                    # 浮点速率比较容差


class ScheduleError(Exception):
    """排班业务错误。status 为建议 HTTP 状态码，reasons 为可展示的原因。"""

    def __init__(self, message: str, status: int = 409,
                 reasons: Optional[list[str]] = None, plan: Optional["Plan"] = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.reasons = reasons or [message]
        self.plan = plan


# ---- 输入解析（兼容常见别名字段，便于对接不同调用方） -------------------

def _pick(data: dict, *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def as_int(raw: Any, label: str, lo: int, hi: int) -> int:
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ScheduleError(f"参数 {label} 必须是整数，收到：{raw!r}", status=400)
    if isinstance(raw, float) and not float(raw).is_integer():
        raise ScheduleError(f"参数 {label} 必须是整数，收到：{raw!r}", status=400)
    if not lo <= value <= hi:
        raise ScheduleError(f"参数 {label} 必须在 {lo}~{hi} 之间，当前为 {value}",
                            status=400)
    return value


def as_rate(raw: Any, label: str = "保底速率") -> float:
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ScheduleError(f"参数 {label} 必须是数字，收到：{raw!r}", status=400)
    if value != value or value in (float("inf"), float("-inf")):
        raise ScheduleError(f"参数 {label} 必须是有限数字，收到：{raw!r}", status=400)
    if not EPS < value <= MAX_RATE:
        raise ScheduleError(f"参数 {label} 必须为正数且不超过 {MAX_RATE:g}，"
                            f"当前为 {value:g}", status=400)
    return value


def as_bandwidth(raw: Any, default: float = DEFAULT_BANDWIDTH) -> float:
    if raw in (None, ""):
        return default
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ScheduleError(f"线路可用带宽必须是数字，收到：{raw!r}", status=400)
    if not EPS < value <= MAX_RATE:
        raise ScheduleError(f"线路可用带宽必须为正数，当前为 {value:g}", status=400)
    return value


# ---- 展示工具 -----------------------------------------------------------

def fmt_minute(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def fmt_span(start: int, end: int) -> str:
    return f"{start}–{end} 分钟（{fmt_minute(start)}–{fmt_minute(end)}）"


def fmt_num(value: float) -> str:
    """去掉无意义的 .0。"""
    if abs(value - round(value)) < EPS:
        return str(int(round(value)))
    return f"{value:.2f}".rstrip("0").rstrip(".")


# ---- 预约单 -------------------------------------------------------------

@dataclass
class BackupOrder:
    id: int
    branch: str
    start: int
    end: int
    payload: int
    rate: float

    def to_dict(self, line: Optional[str] = None) -> dict:
        return {
            "id": self.id,
            "branch": self.branch,
            "start_minute": self.start,
            "end_minute": self.end,
            # 同时给短别名，方便不同前端/脚本读取
            "start": self.start,
            "end": self.end,
            "payload": self.payload,
            "rate": self.rate,
            "guaranteed_rate": self.rate,
            "span_label": fmt_span(self.start, self.end),
            "line": line,
        }


def parse_order(data: Any, oid: int) -> BackupOrder:
    if not isinstance(data, dict):
        raise ScheduleError("备份预约必须是 JSON 对象", status=400)

    branch_raw = _pick(data, "branch", "site", "name", "branch_name")
    branch = str(branch_raw).strip() if branch_raw is not None else ""
    if not branch:
        raise ScheduleError("请填写分支名称（branch）", status=400)
    if len(branch) > 40:
        raise ScheduleError("分支名称最长 40 个字符", status=400)

    start_raw = _pick(data, "start_minute", "start", "start_time", "begin")
    end_raw = _pick(data, "end_minute", "end", "end_time", "finish")
    if start_raw is None or end_raw is None:
        raise ScheduleError("请填写备份的起始分钟 start_minute 和结束分钟 end_minute",
                            status=400)
    start = as_int(start_raw, "起始分钟", MIN_MINUTE, MAX_MINUTE)
    end = as_int(end_raw, "结束分钟", MIN_MINUTE, MAX_MINUTE)
    if start >= end:
        raise ScheduleError(
            f"结束分钟（{end}）必须晚于起始分钟（{start}）；"
            "若备份跨午夜，请把夜班起点记为 0 并换算成连续分钟",
            status=400,
        )

    payload_raw = _pick(data, "payload", "fixed_payload", "payload_bytes",
                        "packet_payload")
    if payload_raw is None:
        raise ScheduleError("请填写固定报文载荷 payload（字节）", status=400)
    payload = as_int(payload_raw, "固定报文载荷", 1, MAX_PAYLOAD)

    rate_raw = _pick(data, "rate", "guaranteed_rate", "min_rate",
                     "reserved_rate", "bandwidth_reserve")
    if rate_raw is None:
        raise ScheduleError("请填写保底速率 rate（与线路可用带宽同单位）", status=400)
    rate = as_rate(rate_raw)

    return BackupOrder(oid, branch, start, end, payload, rate)


# ---- 负载分段 / 可行性 ---------------------------------------------------

def line_allows_mtu(order: BackupOrder, line: Any) -> bool:
    """固定载荷 + 该线封装开销不得超过 MTU。"""
    return order.payload + int(line.overhead) <= int(line.mtu)


def build_segments(items: list[tuple[int, int, int, str, float]]
                   ) -> list[dict]:
    """把若干 [start,end) 速率区间切成原子时段并汇总速率。

    items: (start, end, order_id, branch, rate)。
    同一时刻的所有增减先合并，因此 ``[0,60)`` 与 ``[60,120)`` 不会重叠。
    """
    if not items:
        return []
    times = sorted({t for s, e, *_ in items for t in (s, e)})
    segments: list[dict] = []
    for left, right in zip(times, times[1:]):
        if right <= left:
            continue
        active = [it for it in items if it[0] <= left < it[1]]
        if not active:
            continue
        used = sum(it[4] for it in active)
        segments.append({
            "start": left,
            "end": right,
            "start_label": fmt_minute(left),
            "end_label": fmt_minute(right),
            "span_label": fmt_span(left, right),
            "used": used,
            "used_text": fmt_num(used),
            "order_ids": [it[2] for it in active],
            "branches": [it[3] for it in active],
        })
    return segments


def profile_line(line_key: str,
                 assigned: list[BackupOrder],
                 line: Any) -> dict:
    bw = float(line.bandwidth)
    items = [(o.start, o.end, o.id, o.branch, o.rate) for o in assigned]
    segments = build_segments(items)
    peak = max((seg["used"] for seg in segments), default=0.0)
    for seg in segments:
        seg["bandwidth"] = bw
        seg["overload"] = max(0.0, seg["used"] - bw)
        seg["fit"] = seg["used"] <= bw + EPS
    return {
        "line": line_key,
        "mtu": int(line.mtu),
        "overhead": int(line.overhead),
        "max_payload": int(line.mtu) - int(line.overhead),
        "bandwidth": bw,
        "bandwidth_text": fmt_num(bw),
        "peak": peak,
        "peak_text": fmt_num(peak),
        "peak_ratio": (peak / bw) if bw > EPS else float("inf"),
        "segments": segments,
    }


# ---- 排布结果 -----------------------------------------------------------

@dataclass
class Plan:
    feasible: bool
    assignment: dict[int, str]
    reasons: list[str]
    checked: int
    profiles: dict[str, dict]
    changes: int = 0

    def to_dict(self) -> dict:
        return {
            "feasible": self.feasible,
            "assignment": {str(k): v for k, v in sorted(self.assignment.items())},
            "reasons": self.reasons,
            "checked_assignments": self.checked,
            "changes": self.changes,
            "profiles": self.profiles,
        }


def _assignment_for_mask(orders: list[BackupOrder], mask: int) -> dict[int, str]:
    return {o.id: ("B" if (mask >> i) & 1 else "A")
            for i, o in enumerate(orders)}


def diagnose(orders: list[BackupOrder],
             lines: dict[str, Any],
             mtu_ok: dict[int, dict[str, bool]],
             best_bad: Optional[dict]) -> list[str]:
    """枚举失败后给出人类可读的具体原因（尽量指出时段/线路/单号）。"""
    reasons: list[str] = []

    # 1) 两条线 MTU 都放不下的单 —— 无解的硬原因
    for o in orders:
        ok_a, ok_b = mtu_ok[o.id]["A"], mtu_ok[o.id]["B"]
        if not ok_a and not ok_b:
            la, lb = lines["A"], lines["B"]
            reasons.append(
                f"#{o.id}（{o.branch}）固定载荷 {o.payload} 字节："
                f"A 线 {o.payload}+{la.overhead}={o.payload + la.overhead}"
                f" > MTU {la.mtu}；B 线 {o.payload}+{lb.overhead}"
                f"={o.payload + lb.overhead} > MTU {lb.mtu}；两条线都放不下"
            )

    # 2) MTU 强制只能走一条线的单，若在该线互相挤爆带宽则明确指出
    for forced_line, other in (("A", "B"), ("B", "A")):
        forced = [o for o in orders
                  if mtu_ok[o.id][forced_line] and not mtu_ok[o.id][other]]
        profile = profile_line(forced_line, forced, lines[forced_line])
        for seg in profile["segments"]:
            if seg["used"] > float(lines[forced_line].bandwidth) + EPS:
                names = "、".join(f"#{i}" for i in seg["order_ids"])
                reasons.append(
                    f"受 MTU 限制只能走 {forced_line} 线的预约，在 "
                    f"{seg['span_label']} 同时需要 {seg['used_text']} Mbps"
                    f"（{names}），超过 {forced_line} 线可用带宽 "
                    f"{fmt_num(float(lines[forced_line].bandwidth))} Mbps"
                )

    # 3) 两线带宽之和都不够的时段 —— 与怎么分配无关
    all_items = [(o.start, o.end, o.id, o.branch, o.rate) for o in orders]
    total_bw = float(lines["A"].bandwidth) + float(lines["B"].bandwidth)
    for seg in build_segments(all_items):
        if seg["used"] > total_bw + EPS:
            names = "、".join(f"#{i}({b})"
                              for i, b in zip(seg["order_ids"], seg["branches"]))
            reasons.append(
                f"{seg['span_label']} 全班保底速率合计 {seg['used_text']} Mbps"
                f"（{names}），超过 A+B 两线可用带宽之和 {fmt_num(total_bw)} Mbps，"
                "无论怎样分配走线都排不下"
            )

    # 4) 枚举中“最接近成功”的方案，指出最紧的瓶颈
    if best_bad is not None and not reasons:
        seg = best_bad["segment"]
        line_key = best_bad["line"]
        assign_text = "，".join(
            f"#{o.id}→{best_bad['assignment'][o.id]}" for o in orders)
        reasons.append(
            f"已枚举全部 {best_bad['checked']} 种走线分配，均不满足带宽；"
            f"最接近的方案（{assign_text}）中，{line_key} 线 "
            f"{seg['span_label']} 需 {seg['used_text']} Mbps > 可用带宽 "
            f"{fmt_num(float(lines[line_key].bandwidth))} Mbps，"
            f"涉及 {('、'.join('#' + str(i) for i in seg['order_ids']))}"
        )
    elif not reasons:
        reasons.append(
            f"已枚举全部 {1 << len(orders)} 种走线分配，"
            "不存在同时满足 MTU 与两线带宽约束的方案"
        )
    return reasons


def plan_shift(orders: list[BackupOrder],
               lines: dict[str, Any],
               preferred: Optional[dict[int, str]] = None) -> Plan:
    """对整班 orders 做全枚举排布。纯函数，不修改任何入参状态。

    性能：最多 12 单。先在全部订单的原子时段上预计算每个子集掩码的
    逐段速率和/峰值（子集 DP，2^n × 时段数），枚举 4096 种分配时只需
    O(1) 判定两线带宽，避免在热循环里反复建分段对象。
    """
    preferred = preferred or {}
    n = len(orders)

    if n > MAX_ORDERS_PER_SHIFT:
        return Plan(False, {}, [f"单班最多 {MAX_ORDERS_PER_SHIFT} 单，"
                                f"当前候选 {n} 单"], 0, {})

    # MTU 兼容性矩阵 + 禁位掩码
    mtu_ok: dict[int, dict[str, bool]] = {}
    a_forbidden = 0
    b_forbidden = 0
    for i, o in enumerate(orders):
        ok_a = line_allows_mtu(o, lines["A"])
        ok_b = line_allows_mtu(o, lines["B"])
        mtu_ok[o.id] = {"A": ok_a, "B": ok_b}
        if not ok_a:
            a_forbidden |= 1 << i
        if not ok_b:
            b_forbidden |= 1 << i

    # 预计算全局事件增量：每个时间点上若干 (订单下标, +rate/-rate)。
    # 枚举掩码时一次扫描即可同时得到 A/B 两线的峰值速率。
    event_deltas = _event_deltas(orders)
    bw_a = float(lines["A"].bandwidth)
    bw_b = float(lines["B"].bandwidth)
    full = (1 << n) - 1

    # 当前走线偏好掩码（bit=1 表示 B）；preferred 中没有的单不计改动
    pref_mask = 0
    pref_known = 0
    for i, o in enumerate(orders):
        if o.id in preferred:
            pref_known |= 1 << i
            if preferred[o.id] == "B":
                pref_mask |= 1 << i

    best: Optional[tuple] = None
    best_mask: Optional[int] = None
    bad: Optional[tuple] = None   # (excess, mask, line_key)
    checked = 0

    for mask in range(1 << n):
        # bit=1 -> B，bit=0 -> A
        if (mask & b_forbidden) != 0:
            continue
        if ((full ^ mask) & a_forbidden) != 0:
            continue
        checked += 1

        cur_a = cur_b = 0.0
        peak_a = peak_b = 0.0
        for deltas in event_deltas:
            for idx, delta in deltas:
                if (mask >> idx) & 1:
                    cur_b += delta
                else:
                    cur_a += delta
            if cur_a > peak_a:
                peak_a = cur_a
            if cur_b > peak_b:
                peak_b = cur_b

        ratio_a = peak_a / bw_a if bw_a > EPS else float("inf")
        ratio_b = peak_b / bw_b if bw_b > EPS else float("inf")
        worst_ratio = max(ratio_a, ratio_b)

        if worst_ratio > 1.0 + EPS:
            excess = worst_ratio - 1.0
            line_key = "A" if ratio_a >= ratio_b else "B"
            if bad is None or excess < bad[0]:
                bad = (excess, mask, line_key)
            continue

        changes = (mask ^ pref_mask) & pref_known
        changes = changes.bit_count() if hasattr(changes, "bit_count") \
            else bin(changes).count("1")
        balance = worst_ratio
        square = ratio_a * ratio_a + ratio_b * ratio_b
        score = (changes, round(balance, 12), round(square, 12), mask)
        if best is None or score < best:
            best = score
            best_mask = mask

    if best_mask is not None:
        assignment = _assignment_for_mask(orders, best_mask)
        profiles = assignment_profiles(orders, assignment, lines)
        return Plan(True, assignment, [], checked, profiles,
                    changes=best[0] if best else 0)

    best_bad = None
    if bad is not None:
        _excess, bad_mask, line_key = bad
        assignment = _assignment_for_mask(orders, bad_mask)
        bad_orders = [o for o in orders if assignment[o.id] == line_key]
        prof = profile_line(line_key, bad_orders, lines[line_key])
        seg = max(prof["segments"],
                  key=lambda s: s["used"] / float(lines[line_key].bandwidth),
                  default=None)
        best_bad = {"assignment": assignment, "checked": checked,
                    "line": line_key, "segment": seg}

    reasons = diagnose(orders, lines, mtu_ok, best_bad)
    return Plan(False, {}, reasons, checked, {})


def _event_deltas(orders: list[BackupOrder]) -> list[list[tuple[int, float]]]:
    """按时间点聚合的速率增量：每个事件点一组 (订单下标, +rate/-rate)。

    同一时刻的开始/结束放在同一组里，枚举时整组应用后再记峰值，
    因此端点相接（[0,60) 与 [60,120)）不会被误判为重叠。
    """
    by_time: dict[int, list[tuple[int, float]]] = {}
    for i, o in enumerate(orders):
        by_time.setdefault(o.start, []).append((i, o.rate))
        by_time.setdefault(o.end, []).append((i, -o.rate))
    return [by_time[t] for t in sorted(by_time)]


def assignment_profiles(orders: list[BackupOrder],
                        assignment: dict[int, str],
                        lines: dict[str, Any]) -> dict[str, dict]:
    return {
        "A": profile_line("A", [o for o in orders if assignment.get(o.id) == "A"],
                          lines["A"]),
        "B": profile_line("B", [o for o in orders if assignment.get(o.id) == "B"],
                          lines["B"]),
    }


# ---- 班次的已提交状态 ---------------------------------------------------

class ShiftBoard:
    """当前已接预约 + 已提交走线。所有变更方法都遵循“先排后交、失败不动”。"""

    def __init__(self, lines: dict[str, Any]):
        self.lines = lines
        self.orders: list[BackupOrder] = []
        self.next_id = 1
        self.assignment: dict[int, str] = {}
        self.last_plan: Optional[Plan] = None

    # -- 查询 --
    def get(self, oid: int) -> BackupOrder:
        for o in self.orders:
            if o.id == oid:
                return o
        raise ScheduleError(f"找不到备份预约单 #{oid}", status=404)

    def state_dict(self) -> dict:
        profiles = assignment_profiles(self.orders, self.assignment, self.lines)
        return {
            "max_orders": MAX_ORDERS_PER_SHIFT,
            "count": len(self.orders),
            "orders": [o.to_dict(self.assignment.get(o.id)) for o in self.orders],
            "assignment": {str(k): v for k, v in sorted(self.assignment.items())},
            "profiles": profiles,
            "lines": {
                k: {
                    "mtu": int(v.mtu),
                    "overhead": int(v.overhead),
                    "bandwidth": float(v.bandwidth),
                    "max_payload": int(v.mtu) - int(v.overhead),
                }
                for k, v in self.lines.items()
            },
        }

    # -- 试排（不落任何状态） --
    def preview_add(self, data: Any) -> Plan:
        if len(self.orders) >= MAX_ORDERS_PER_SHIFT:
            raise ScheduleError(
                f"单班最多 {MAX_ORDERS_PER_SHIFT} 单，已接满；"
                "请先删除或调整既有预约", status=409)
        candidate = parse_order(data, self.next_id)
        return plan_shift([*self.orders, candidate], self.lines, self.assignment)

    def preview_with(self, candidate_orders: Optional[list[BackupOrder]] = None,
                     lines: Optional[dict[str, Any]] = None) -> Plan:
        return plan_shift(candidate_orders if candidate_orders is not None
                          else self.orders,
                          lines or self.lines, self.assignment)

    # -- 提交：加单 --
    def add_order(self, data: Any) -> tuple[BackupOrder, Plan]:
        if len(self.orders) >= MAX_ORDERS_PER_SHIFT:
            raise ScheduleError(
                f"单班最多 {MAX_ORDERS_PER_SHIFT} 单，已接满 "
                f"{MAX_ORDERS_PER_SHIFT} 单；请先删除或调整既有预约",
                status=409)
        order = parse_order(data, self.next_id)
        plan = plan_shift([*self.orders, order], self.lines, self.assignment)
        if not plan.feasible:
            # 关键：不 append、不推进 next_id、不改 assignment
            raise ScheduleError(
                f"新单 #{order.id}（{order.branch}）加入后整班排不下，"
                "已保留原安排：" + "；".join(plan.reasons),
                status=409, reasons=plan.reasons, plan=plan)
        self.orders.append(order)
        self.next_id += 1
        self.assignment = dict(plan.assignment)
        self.last_plan = plan
        return order, plan

    # -- 提交：新线路参数（必须安置届时已接的单） --
    def apply_lines(self, new_lines: dict[str, Any]) -> Plan:
        plan = plan_shift(self.orders, new_lines, self.assignment)
        if not plan.feasible:
            raise ScheduleError(
                "新的 A/B 线路参数放不下当前已接预约，参数未生效、"
                "发包演练与原排线均保留：" + "；".join(plan.reasons),
                status=409, reasons=plan.reasons, plan=plan)
        self.lines = new_lines
        self.assignment = dict(plan.assignment)
        self.last_plan = plan
        return plan

    # -- 删除 / 重排 / 清空 / 演示 --
    def remove_order(self, oid: int) -> Plan:
        order = self.get(oid)
        remaining = [o for o in self.orders if o.id != oid]
        plan = plan_shift(remaining, self.lines, self.assignment)
        if not plan.feasible:  # 删单只会放松约束，理论上不可能发生
            raise ScheduleError(
                f"删除 #{oid} 后重排失败（不应发生）："
                + "；".join(plan.reasons), status=409, plan=plan)
        self.orders = remaining
        self.assignment = dict(plan.assignment)
        self.last_plan = plan
        return plan

    def replan(self) -> Plan:
        """不受当前走线偏好约束，从全局重新选最均衡方案并提交。"""
        plan = plan_shift(self.orders, self.lines, preferred=None)
        if not plan.feasible:
            raise ScheduleError("当前班次无法排布：" + "；".join(plan.reasons),
                                status=409, reasons=plan.reasons, plan=plan)
        self.assignment = dict(plan.assignment)
        self.last_plan = plan
        return plan

    def clear(self) -> None:
        self.orders = []
        self.assignment = {}
        self.next_id = 1
        self.last_plan = None

    def load_demo(self) -> Plan:
        """灌入一组在默认参数下可展示 A/B 分工的演示单（覆盖当前班次）。"""
        demo_raw = [
            {"branch": "甲分支", "start_minute": 0, "end_minute": 120,
             "payload": 1400, "rate": 60},
            {"branch": "乙分支", "start_minute": 0, "end_minute": 120,
             "payload": 1200, "rate": 30},
            {"branch": "丙分支", "start_minute": 60, "end_minute": 180,
             "payload": 1000, "rate": 50},
            {"branch": "丁分支", "start_minute": 120, "end_minute": 240,
             "payload": 1300, "rate": 40},
        ]
        demo_orders = [parse_order(d, i + 1) for i, d in enumerate(demo_raw)]
        plan = plan_shift(demo_orders, self.lines, preferred=None)
        if not plan.feasible:
            raise ScheduleError("当前线路参数下演示班次不可行，请先恢复默认参数",
                                status=409, reasons=plan.reasons, plan=plan)
        self.orders = demo_orders
        self.next_id = len(demo_orders) + 1
        self.assignment = dict(plan.assignment)
        self.last_plan = plan
        return plan
