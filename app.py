"""VPN 换线 / MTU 黑洞演练 + 夜间备份双路排班 —— Flask 入口。

单实例内存状态 + 一把 RLock：所有“校验 → 整班排布 → 提交”都在锁内完成，
因此交错到达的加单与改参请求被线性化，每个请求都按“届时”已提交的线路
参数与预约排布；排不下时抛错且不动任何状态（原演练、原预约保留）。
部署见 Dockerfile / compose.yaml（gunicorn 固定 1 个 worker、多线程）。
"""

from __future__ import annotations

import threading

from flask import Flask, jsonify, render_template, request

from drill_engine import Drill, DrillConfig, DrillError
from schedule_engine import (
    MAX_ORDERS_PER_SHIFT,
    ScheduleError,
    ShiftBoard,
    parse_order,
    plan_shift,
)

app = Flask(__name__)

_LOCK = threading.RLock()
_DRILL = Drill()
# 排班板的线路参数与当前 Drill 配置共享同一组 LineConfig 对象。
_BOARD = ShiftBoard(_DRILL.cfg.lines)


def _json_body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _full_state() -> dict:
    """演练状态 + 排班状态。调用方必须持有 _LOCK（或接受即时快照）。"""
    state = _DRILL.state_dict()
    state["schedule"] = _BOARD.state_dict()
    return state


@app.errorhandler(DrillError)
def handle_drill_error(err: DrillError):
    return jsonify({"ok": False, "error": err.message}), err.status


@app.errorhandler(ScheduleError)
def handle_schedule_error(err: ScheduleError):
    return jsonify({
        "ok": False,
        "error": err.message,
        "reasons": err.reasons,
        "plan": err.plan.to_dict() if err.plan is not None else None,
        # 失败时也回传当前（未改动的）状态，便于前端/调用方核对
        "state": _safe_state(),
    }), err.status


def _safe_state():
    with _LOCK:
        return _full_state()


@app.errorhandler(404)
def handle_404(_err):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "接口不存在"}), 404
    return "Not Found", 404


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def index():
    return render_template("index.html")


# ======================================================================
# 整体状态
# ======================================================================

@app.get("/api/state")
def get_state():
    with _LOCK:
        return jsonify({"ok": True, "state": _full_state()})


# ======================================================================
# 线路参数 / 演练重置
# ======================================================================

def _commit_new_config(data: dict):
    """解析新演练参数；先让排班板按新线路参数安置全部已接单，成功后才
    替换 Drill（重置发包演练）。任一步失败都不改变现有 Drill / Board。"""
    global _DRILL
    # 解析是纯函数，可能以 400 失败，此时尚未触碰任何状态。
    cfg = DrillConfig.from_raw(data if data else None)
    with _LOCK:
        # 可能抛 ScheduleError(409)：参数不兼容已接预约，状态原样保留。
        plan = _BOARD.apply_lines(cfg.lines)
        _DRILL = Drill(cfg)  # 排布已通过，这里之后视为原子生效
        return cfg, plan


@app.post("/api/reset")
def reset_drill():
    data = _json_body()
    _cfg, plan = _commit_new_config(data)
    with _LOCK:
        return jsonify({"ok": True, "plan": plan.to_dict(),
                        "state": _full_state()})


@app.post("/api/line-params")
def apply_line_params():
    """语义化别名：应用 A/B 新参数。请求体可带完整演练参数（同 /api/reset），
    也可只发 {"A": {...}, "B": {...}} 或 {"lines": {...}}。
    成功后同样重置发包演练。"""
    data = _json_body()
    if "lines" not in data and ("A" in data or "B" in data):
        data = {"lines": data}
    _cfg, plan = _commit_new_config(data)
    with _LOCK:
        return jsonify({"ok": True, "plan": plan.to_dict(),
                        "state": _full_state()})


# ======================================================================
# 夜间备份预约 / 整班排线
# ======================================================================

@app.get("/api/schedule")
def get_schedule():
    with _LOCK:
        return jsonify({"ok": True, "schedule": _BOARD.state_dict()})


@app.get("/api/orders")
def list_orders():
    with _LOCK:
        board = _BOARD.state_dict()
        return jsonify({"ok": True, "orders": board["orders"],
                        "schedule": board})


@app.post("/api/schedule/plan")
def preview_schedule():
    """试排（dry-run，绝不提交）。

    请求体任意字段可缺省：
    * {"order": {...}}       —— 把这张候选单加入当前班后整班重排；
    * {"orders": [...]}      —— 用整份候选单列表试排（高级用法）；
    * {"lines": {A,B}}       —— 同时试一套候选线路参数；
    * 空体                    —— 按当前参数把当前班无偏好重排一次。
    无论结果如何，当前已提交安排都不变，可随时 GET /api/schedule 核对。
    """
    data = _json_body()
    with _LOCK:
        # 候选线路参数：缺省用当前参数；给了就走同一套解析/校验。
        lines = _BOARD.lines
        raw_lines = data.get("lines")
        if isinstance(raw_lines, dict):
            lines = DrillConfig.from_raw({"lines": raw_lines}).lines

        if isinstance(data.get("orders"), list):
            candidate = [parse_order(d, i + 1)
                         for i, d in enumerate(data["orders"])]
            if len(candidate) > MAX_ORDERS_PER_SHIFT:
                plan = plan_shift(candidate, lines, None)
            else:
                plan = plan_shift(candidate, lines, _BOARD.assignment)
        elif isinstance(data.get("order"), dict):
            if len(_BOARD.orders) >= MAX_ORDERS_PER_SHIFT:
                raise ScheduleError(
                    f"单班最多 {MAX_ORDERS_PER_SHIFT} 单，已接满", status=409)
            new_order = parse_order(data["order"], _BOARD.next_id)
            plan = plan_shift([*_BOARD.orders, new_order], lines,
                              _BOARD.assignment)
        else:
            plan = plan_shift(_BOARD.orders, lines, preferred=None)

        return jsonify({"ok": True, "plan": plan.to_dict(),
                        "schedule_before": _BOARD.state_dict()})


@app.post("/api/orders")
@app.post("/api/backups")
@app.post("/api/schedule/orders")
@app.post("/api/schedule")
def add_order():
    data = _json_body()
    with _LOCK:
        order, plan = _BOARD.add_order(data)
        return jsonify({
            "ok": True,
            "order": order.to_dict(_BOARD.assignment.get(order.id)),
            "plan": plan.to_dict(),
            "state": _full_state(),
        })


@app.delete("/api/orders/<int:oid>")
@app.post("/api/orders/<int:oid>/delete")
def delete_order(oid: int):
    with _LOCK:
        plan = _BOARD.remove_order(oid)
        return jsonify({"ok": True, "plan": plan.to_dict(),
                        "state": _full_state()})


@app.post("/api/schedule/replan")
def replan_schedule():
    with _LOCK:
        plan = _BOARD.replan()
        return jsonify({"ok": True, "plan": plan.to_dict(),
                        "state": _full_state()})


@app.post("/api/schedule/clear")
def clear_schedule():
    with _LOCK:
        _BOARD.clear()
        return jsonify({"ok": True, "state": _full_state()})


@app.post("/api/schedule/demo")
def demo_schedule():
    with _LOCK:
        plan = _BOARD.load_demo()
        return jsonify({"ok": True, "plan": plan.to_dict(),
                        "state": _full_state()})


# ======================================================================
# 发包演练操作（响应里始终带最新排班状态）
# ======================================================================

@app.post("/api/send-next")
def send_next():
    with _LOCK:
        pkt = _DRILL.send_next()
        return jsonify({"ok": True, "packet": pkt.to_dict(),
                        "state": _full_state()})


@app.post("/api/send-burst")
def send_burst():
    data = _json_body()
    count = data.get("count")
    with _LOCK:
        pkts = _DRILL.send_burst(None if count in (None, "", 0, "all") else count)
        return jsonify({"ok": True, "packets": [p.to_dict() for p in pkts],
                        "state": _full_state()})


@app.post("/api/resend-missing")
def resend_missing():
    with _LOCK:
        pkts = _DRILL.resend_missing(kind="missing")
        return jsonify({"ok": True, "packets": [p.to_dict() for p in pkts],
                        "state": _full_state()})


@app.post("/api/switch")
def switch_line():
    line = _json_body().get("line", "")
    with _LOCK:
        result = _DRILL.switch_line(str(line).upper())
        return jsonify({"ok": True, "result": result,
                        "state": _full_state()})


@app.post("/api/packets/<int:pid>/ack")
def ack_packet(pid: int):
    with _LOCK:
        _pkt, added = _DRILL.acknowledge(pid)
        return jsonify({"ok": True, "added_bytes": added,
                        "state": _full_state()})


@app.post("/api/packets/<int:pid>/timeout")
def timeout_packet(pid: int):
    with _LOCK:
        result = _DRILL.trigger_timeout(pid)
        return jsonify({"ok": True, "result": result,
                        "state": _full_state()})


@app.post("/api/packets/<int:pid>/deliver-notice")
def deliver_notice(pid: int):
    with _LOCK:
        result = _DRILL.deliver_notice(pid)
        return jsonify({"ok": True, "result": result,
                        "state": _full_state()})


if __name__ == "__main__":
    # 本机直接 `python app.py` 时使用；容器内走 gunicorn。
    app.run(host="0.0.0.0", port=8000, threaded=True)
