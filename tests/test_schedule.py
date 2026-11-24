"""夜间备份排班 / 双路整班排布 / 交错提交的测试。

覆盖：
* 纯排布引擎：MTU 强制选线、带宽求和、边界相等、相邻时段不重叠、
  超卖不可行、12 单上限、失败原因；
* HTTP/API：加单、试排不落库、删除/清空/演示、参数生效与“失败保留
  原演练和预约”的原子语义；
* 多线程交错提交：并发加单 + 并发改参下不出现 5xx、不超 12 单，
  且最终状态始终满足全部硬约束（线性化后的不变量）。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app import app as flask_app
from drill_engine import DrillConfig
from schedule_engine import (
    MAX_ORDERS_PER_SHIFT,
    BackupOrder,
    ShiftBoard,
    plan_shift,
)


# ---------------------------------------------------------------- fixtures

@pytest.fixture()
def client():
    flask_app.config["TESTING"] = True
    c = flask_app.test_client()
    c.post("/api/reset", json={})
    c.post("/api/schedule/clear")
    return c


def reset_params(client, bw_a=100, bw_b=100, overhead_a=120, overhead_b=60,
                 mtu_a=1500, mtu_b=1500):
    body = {
        "lines": {
            "A": {"mtu": mtu_a, "overhead": overhead_a,
                  "bandwidth": bw_a, "notify": False},
            "B": {"mtu": mtu_b, "overhead": overhead_b,
                  "bandwidth": bw_b, "notify": True},
        }
    }
    rv = client.post("/api/reset", json=body)
    assert rv.status_code == 200, rv.get_json()
    return rv.get_json()["state"]


def order_body(branch="甲分支", s=0, e=60, payload=100, rate=10):
    return {"branch": branch, "start_minute": s, "end_minute": e,
            "payload": payload, "rate": rate}


def add(client, body=None, expect=200):
    rv = client.post("/api/orders", json=body or order_body())
    assert rv.status_code == expect, (rv.status_code, rv.get_json())
    return rv.get_json()


def assert_schedule_invariant(state):
    """对任意已提交状态校验全部硬约束。"""
    sch = state["schedule"]
    lines = state["config"]["lines"]
    assert sch["count"] == len(sch["orders"]) <= MAX_ORDERS_PER_SHIFT

    # 排班板与演练使用同一套已提交线路参数
    for k in ("A", "B"):
        assert abs(sch["lines"][k]["bandwidth"] - lines[k]["bandwidth"]) < 1e-9
        assert sch["lines"][k]["mtu"] == lines[k]["mtu"]
        assert sch["lines"][k]["overhead"] == lines[k]["overhead"]

    for o in sch["orders"]:
        assert o["line"] in ("A", "B")
        lc = lines[o["line"]]
        assert o["payload"] + lc["overhead"] <= lc["mtu"]

    # 逐分钟扫描同线速率和（整数分钟区间，直接逐点最稳妥）
    for line_key in ("A", "B"):
        bw = lines[line_key]["bandwidth"]
        mine = [o for o in sch["orders"] if o["line"] == line_key]
        if not mine:
            continue
        lo = min(o["start_minute"] for o in mine)
        hi = max(o["end_minute"] for o in mine)
        for t in range(lo, hi):
            used = sum(o["rate"] for o in mine
                       if o["start_minute"] <= t < o["end_minute"])
            assert used <= bw + 1e-9, (line_key, t, used, bw)


# ============================================================ 引擎单元

def _lines(**kw):
    return DrillConfig.from_raw({"lines": kw}).lines


def _o(oid, s=0, e=60, payload=100, rate=10, branch=None):
    return BackupOrder(oid, branch or f"b{oid}", s, e, payload, rate)


def test_mtu_forces_line_then_bandwidth_splits():
    lines = _lines()  # A maxpayload 1380，B maxpayload 1440，bw 各 100
    orders = [_o(1, payload=1400, rate=60), _o(2, payload=1200, rate=30)]
    plan = plan_shift(orders, lines)
    assert plan.feasible
    assert plan.assignment[1] == "B"   # 1400+120 超 A 的 MTU
    assert plan.assignment[2] == "A"


def test_rate_sum_equal_bandwidth_is_feasible():
    lines = _lines()
    plan = plan_shift([_o(1, rate=100)], lines)
    assert plan.feasible and plan.assignment[1] == "A"
    plan2 = plan_shift([_o(2, rate=100 + 1e-6)], lines)
    assert not plan2.feasible and plan2.reasons


def test_adjacent_intervals_share_line_without_overlap():
    lines = _lines()
    orders = [_o(1, 0, 60, rate=100), _o(2, 60, 120, rate=100)]
    plan = plan_shift(orders, lines, preferred={})
    assert plan.feasible
    # 不重叠，两单都可以在 A；均衡/掩码打分下保持同线
    assert plan.assignment == {1: "A", 2: "A"}


def test_three_overlapping_60_on_two_100_lines_infeasible():
    lines = _lines()
    orders = [_o(i, 0, 60, rate=60) for i in range(1, 4)]
    plan = plan_shift(orders, lines)
    assert not plan.feasible
    assert any("带宽" in r for r in plan.reasons)


def test_total_rate_over_both_bandwidth_sum_reason():
    lines = _lines()
    orders = [_o(i, 0, 60, rate=150) for i in range(1, 3)]  # 300 > 200
    plan = plan_shift(orders, lines)
    assert not plan.feasible
    assert any("两线可用带宽之和" in r for r in plan.reasons)


def test_payload_oversized_on_both_lines_reason():
    lines = _lines()
    plan = plan_shift([_o(1, payload=1450)], lines)  # A:1570 B:1510
    assert not plan.feasible
    assert any("两条线都放不下" in r for r in plan.reasons)


def test_more_than_12_rejected_by_engine_and_board():
    lines = _lines()
    plan = plan_shift([_o(i) for i in range(1, 14)], lines)
    assert not plan.feasible and any("12" in r for r in plan.reasons)

    board = ShiftBoard(lines)
    for i in range(12):
        o, p = board.add_order(order_body(branch=f"b{i}"))
        assert p.feasible
    with pytest.raises(Exception) as ei:
        board.add_order(order_body(branch="overflow"))
    assert ei.value.status == 409 and "12" in ei.value.message


def test_failed_add_does_not_consume_id_or_move_assignment():
    board = ShiftBoard(_lines())
    board.add_order(order_body(rate=60))
    board.add_order(order_body(rate=60, branch="二"))
    snap = dict(board.assignment)
    next_id = board.next_id
    with pytest.raises(Exception):
        board.add_order(order_body(rate=60, branch="三"))
    assert board.next_id == next_id
    assert board.assignment == snap
    assert len(board.orders) == 2


# ============================================================ API 基础

def test_page_contains_schedule_inputs(client):
    html = client.get("/").get_data(as_text=True)
    for text in ("夜间备份", "分支", "起始分钟", "结束分钟",
                 "固定报文载荷", "保底速率", "可用带宽"):
        assert text in html, text


def test_state_exposes_schedule(client):
    st = client.get("/api/state").get_json()["state"]
    assert st["schedule"]["max_orders"] == 12
    assert st["schedule"]["count"] == 0


def test_add_order_returns_line_and_lists_it(client):
    data = add(client, order_body(payload=1400, rate=60))
    line = data["order"]["line"]
    assert line == "B"  # 1400 载荷在默认 A(开销120) 超 MTU
    rows = client.get("/api/schedule").get_json()["schedule"]["orders"]
    assert rows[0]["line"] == "B"
    assert_schedule_invariant(data["state"])


def test_invalid_orders_are_400_and_not_counted(client):
    bads = [
        {"branch": "", "start_minute": 0, "end_minute": 60,
         "payload": 100, "rate": 10},
        {"branch": "x", "start_minute": 60, "end_minute": 60,
         "payload": 100, "rate": 10},
        {"branch": "x", "start_minute": 900, "end_minute": 1500,
         "payload": 100, "rate": 10},
        {"branch": "x", "start_minute": 0, "end_minute": 60,
         "payload": "abc", "rate": 10},
        {"branch": "x", "start_minute": 0, "end_minute": 60,
         "payload": 100, "rate": -1},
    ]
    for bad in bads:
        rv = client.post("/api/orders", json=bad)
        assert rv.status_code == 400, bad
        assert rv.get_json()["ok"] is False
    assert client.get("/api/schedule").get_json()["schedule"]["count"] == 0


def test_12_orders_ok_13th_conflict_state_intact(client):
    reset_params(client, bw_a=100_000, bw_b=100_000)
    for i in range(12):
        add(client, order_body(branch=f"b{i}", s=0, e=30 + i))
    rv = client.post("/api/orders", json=order_body(branch="第13单"))
    assert rv.status_code == 409
    body = rv.get_json()
    assert "12" in body["error"]
    assert body["ok"] is False
    st = client.get("/api/state").get_json()["state"]
    assert st["schedule"]["count"] == 12
    assert_schedule_invariant(st)


# ============================================================ 试排

def test_preview_feasible_and_infeasible_never_commits(client):
    add(client, order_body(branch="甲", rate=40))
    before = client.get("/api/schedule").get_json()["schedule"]

    # 可行的候选：返回方案，但不落库
    rv = client.post("/api/schedule/plan",
                     json={"order": order_body(branch="候选", rate=5)})
    assert rv.status_code == 200
    assert rv.get_json()["plan"]["feasible"] is True
    after = client.get("/api/schedule").get_json()["schedule"]
    assert after["count"] == before["count"] == 1
    assert after["assignment"] == before["assignment"]

    # 不可行候选：feasible=false + 原因，当前安排原样可查
    rv = client.post("/api/schedule/plan",
                     json={"order": order_body(branch="撑爆", rate=99999)})
    plan = rv.get_json()["plan"]
    assert plan["feasible"] is False and plan["reasons"]
    stable = client.get("/api/schedule").get_json()["schedule"]
    assert stable == before


def test_preview_candidate_lines_without_applying(client):
    add(client, order_body(rate=90))  # 默认走线 A
    rv = client.post("/api/schedule/plan",
                     json={"lines": {"A": {"bandwidth": 10},
                                    "B": {"bandwidth": 10}}})
    plan = rv.get_json()["plan"]
    assert plan["feasible"] is False  # 90 > 10，单线也装不下
    # 只是试排，线上带宽仍是 100
    sch = client.get("/api/schedule").get_json()["schedule"]
    assert sch["lines"]["A"]["bandwidth"] == 100


# ============================================================ 提交原子性

def test_infeasible_add_keeps_original_bookings(client):
    reset_params(client, bw_a=10, bw_b=10)
    add(client, order_body(branch="甲", rate=6))
    add(client, order_body(branch="乙", rate=6))
    before = client.get("/api/schedule").get_json()["schedule"]
    assert before["assignment"] == {"1": "A", "2": "B"}

    rv = client.post("/api/orders", json=order_body(branch="丙", rate=6))
    assert rv.status_code == 409
    body = rv.get_json()
    assert body["reasons"] and isinstance(body["reasons"], list)
    assert body["state"]["schedule"]["count"] == 2

    after = client.get("/api/schedule").get_json()["schedule"]
    assert after["count"] == 2
    assert after["assignment"] == before["assignment"]
    assert_schedule_invariant(client.get("/api/state").get_json()["state"])


def _make_drill_marker(client):
    """发一个包，返回用于判断演练是否被重置的标记。"""
    rv = client.post("/api/send-next", json={})
    assert rv.status_code == 200
    st = rv.get_json()["state"]
    return {"packets": len(st["packets"]), "round": st["round"],
            "cur_payload": st["cur_payload"],
            "first_id": st["packets"][0]["id"]}


def test_successful_param_apply_replans_and_resets_drill(client):
    # A 载荷上限 1380 / B 1440
    add(client, order_body(branch="甲", payload=1400, rate=60))  # 强制 B
    add(client, order_body(branch="乙", payload=1200, rate=30))  # A/B 均可
    marker = _make_drill_marker(client)
    assert marker["packets"] == 1

    # 新参数：A 开销 1499 -> 任何正常载荷都不能走 A；B 带宽 100 能装 90
    rv = client.post("/api/reset", json={"lines": {
        "A": {"mtu": 1500, "overhead": 1499, "bandwidth": 100},
        "B": {"mtu": 1500, "overhead": 60, "bandwidth": 100},
    }})
    assert rv.status_code == 200, rv.get_json()
    st = rv.get_json()["state"]

    # 演练被重置
    assert st["round"] == 1 and st["packets"] == []
    # 两单都改走 B
    assert {o["id"]: o["line"] for o in st["schedule"]["orders"]} == {
        1: "B", 2: "B"}
    assert st["config"]["lines"]["A"]["overhead"] == 1499
    assert_schedule_invariant(st)


def test_failed_param_apply_preserves_drill_and_bookings(client):
    add(client, order_body(branch="甲", rate=40))
    add(client, order_body(branch="乙", rate=40))
    before = client.get("/api/state").get_json()["state"]
    assert before["schedule"]["assignment"] == {"1": "A", "2": "B"}
    marker = _make_drill_marker(client)

    # 两线带宽都改成 10：总速率 80 > 20，必排不下
    rv = client.post("/api/reset", json={"lines": {
        "A": {"bandwidth": 10}, "B": {"bandwidth": 10}}})
    assert rv.status_code == 409
    assert rv.get_json()["reasons"]

    after = client.get("/api/state").get_json()["state"]
    # 原演练保留：同一个包、同一轮次/载荷
    assert len(after["packets"]) == marker["packets"] == 1
    assert after["packets"][0]["id"] == marker["first_id"]
    assert after["round"] == marker["round"]
    assert after["cur_payload"] == marker["cur_payload"]
    # 原预约与原走线保留，线路参数也没变
    assert after["schedule"]["assignment"] == {"1": "A", "2": "B"}
    assert after["config"]["lines"]["A"]["bandwidth"] == 100
    assert after["schedule"]["lines"]["A"]["bandwidth"] == 100
    assert_schedule_invariant(after)


def test_line_params_alias_resets_drill_on_success(client):
    add(client, order_body(rate=10))
    _make_drill_marker(client)
    rv = client.post("/api/line-params", json={
        "A": {"bandwidth": 200}, "B": {"bandwidth": 300}})
    assert rv.status_code == 200, rv.get_json()
    st = rv.get_json()["state"]
    assert st["round"] == 1 and st["packets"] == []
    assert st["config"]["lines"]["A"]["bandwidth"] == 200
    assert st["config"]["lines"]["B"]["bandwidth"] == 300
    assert_schedule_invariant(st)


def test_delete_and_replan_endpoints(client):
    reset_params(client, bw_a=100_000, bw_b=100_000)
    for i in range(4):
        add(client, order_body(branch=f"b{i}"))
    rv = client.delete("/api/orders/2")
    assert rv.status_code == 200
    st = rv.get_json()["state"]
    assert [o["id"] for o in st["schedule"]["orders"]] == [1, 3, 4]

    rv = client.post("/api/schedule/replan", json={})
    assert rv.status_code == 200
    assert rv.get_json()["plan"]["feasible"]
    assert_schedule_invariant(rv.get_json()["state"])

    rv = client.post("/api/schedule/clear", json={})
    assert rv.status_code == 200
    assert rv.get_json()["state"]["schedule"]["count"] == 0


def test_demo_scenario_is_feasible_and_listed(client):
    rv = client.post("/api/schedule/demo", json={})
    assert rv.status_code == 200, rv.get_json()
    st = rv.get_json()["state"]
    assert st["schedule"]["count"] == 4
    lines = {o["branch"]: o["line"] for o in st["schedule"]["orders"]}
    assert lines["甲分支"] == "B"   # 1400 载荷被 A 的 MTU 逼到 B
    assert_schedule_invariant(st)


# ============================================================ 交错提交

def test_concurrent_adds_respect_cap_and_invariant(client):
    reset_params(client, bw_a=100_000, bw_b=100_000)
    barrier = threading.Barrier(8)
    results = []
    results_lock = threading.Lock()

    def worker(wid):
        c = flask_app.test_client()
        barrier.wait()
        for k in range(2):
            rv = c.post("/api/orders",
                        json=order_body(branch=f"w{wid}-{k}", s=0, e=30 + k))
            with results_lock:
                results.append(rv.status_code)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(8)))

    assert all(code in (200, 409) for code in results), results
    assert results.count(200) == 12
    assert results.count(409) == 4
    st = client.get("/api/state").get_json()["state"]
    assert st["schedule"]["count"] == 12
    assert_schedule_invariant(st)


def test_interleaved_adds_and_param_commits_no_5xx(client):
    reset_params(client, bw_a=100_000, bw_b=100_000)
    stop = threading.Event()
    codes = []
    codes_lock = threading.Lock()

    def record(code):
        with codes_lock:
            codes.append(code)

    def adder(wid):
        c = flask_app.test_client()
        i = 0
        while not stop.is_set():
            rv = c.post("/api/orders",
                        json=order_body(branch=f"a{wid}-{i}", rate=1,
                                        s=0, e=20 + (i % 40)))
            record(("add", rv.status_code))
            i += 1

    def param_flip():
        c = flask_app.test_client()
        toggle = 0
        while not stop.is_set():
            toggle += 1
            # 两套参数都能容纳 rate=1 的小单，都应成功并重置演练
            bw = 100_000 if toggle % 2 else 50_000
            rv = c.post("/api/line-params",
                        json={"A": {"bandwidth": bw}, "B": {"bandwidth": bw}})
            record(("param", rv.status_code))

    def reader():
        c = flask_app.test_client()
        while not stop.is_set():
            rv = c.get("/api/schedule")
            record(("read", rv.status_code))

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(adder, i) for i in range(4)]
        futures += [pool.submit(param_flip) for _ in range(2)]
        futures += [pool.submit(reader) for _ in range(2)]
        # 让写操作交错跑约 1 秒
        threading.Event().wait(1.0)
        stop.set()
        for f in futures:
            f.result(timeout=10)

    assert codes, "没有收集到任何请求结果"
    assert all(code == 200 for kind, code in codes if kind == "read"), codes
    assert all(code in (200, 409) for kind, code in codes
               if kind in ("add", "param")), codes
    assert not any(code >= 500 for _k, code in codes), codes

    st = client.get("/api/state").get_json()["state"]
    assert_schedule_invariant(st)


def test_adding_order_can_reshuffle_existing_lines(client):
    """新单挤爆当前走线时，允许（且必须）移动既有单，而不是只做贪心追加。"""
    reset_params(client, bw_a=10, bw_b=10)
    add(client, order_body(branch="o1", rate=6))
    add(client, order_body(branch="o2", rate=3))
    before = client.get("/api/schedule").get_json()["schedule"]["assignment"]
    assert before == {"1": "A", "2": "B"}

    # o3 被 MTU 强制到 B（1400+120>1500 for A），速率 8：
    # B 现有 3，3+8=11>10；唯一可行是把 o2 移到 A（6+3=9），o3 独占 B。
    data = add(client, order_body(branch="o3", payload=1400, rate=8))
    assert data["plan"]["changes"] >= 1
    assign = {int(k): v for k, v in data["state"]["schedule"]["assignment"].items()}
    assert assign == {1: "A", 2: "A", 3: "B"}
    assert_schedule_invariant(data["state"])


def test_scripted_interleaving_commit_time_semantics(client):
    """同一组候选操作，按两种交错顺序提交，验证“按届时状态排布”。"""
    # 顺序一：先加单成功，再把 B 带宽调到 80（甲 60 仍在 B 放得下）
    reset_params(client)
    add(client, order_body(branch="甲", payload=1400, rate=60))  # B
    add(client, order_body(branch="乙", payload=1200, rate=30))  # A
    rv = client.post("/api/line-params", json={
        "A": {"bandwidth": 100}, "B": {"bandwidth": 80}})
    assert rv.status_code == 200
    st = rv.get_json()["state"]
    assert st["schedule"]["assignment"] == {"1": "B", "2": "A"}

    # 顺序二（全新状态）：先清空顺序一留下的预约（/api/reset 按设计保留预约），
    # 再把 B 调到 50 —— 此时无单，成功；
    # 再加被 MTU 强制到 B、速率 60 的甲单，必须按“届时” bwB=50 排布并失败。
    reset_params(client)
    assert client.post("/api/schedule/clear").status_code == 200
    rv = client.post("/api/line-params", json={
        "A": {"bandwidth": 100}, "B": {"bandwidth": 50}})
    assert rv.status_code == 200
    rv = client.post("/api/orders",
                     json=order_body(branch="甲", payload=1400, rate=60))
    assert rv.status_code == 409
    assert any("B" in r and "带宽" in r for r in rv.get_json()["reasons"])
    assert client.get("/api/schedule").get_json()["schedule"]["count"] == 0
