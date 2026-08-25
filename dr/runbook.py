"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "step": n,
        "name": name,
        **kw,
    }
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"RUNBOOK step={n} name={name}", json.dumps(rec))
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """Auto=True -> True; ngược lại hỏi y/N."""
    if auto:
        return True
    try:
        ans = input(f"{msg} [y/N]: ").strip().lower()
        return ans in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """7 bước theo runbook tiêu chuẩn."""
    from dr import health_checker as hc

    # 0. Tìm t_outage nếu có
    t_outage = None
    chaos_p = pathlib.Path("chaos/chaos-events.jsonl")
    if chaos_p.exists():
        for l in chaos_p.read_text().splitlines():
            if l.strip():
                try:
                    d = json.loads(l)
                    if d.get("action") == "kill" and d.get("region") == primary:
                        t_outage = d.get("ts")
                except Exception:
                    pass

    # 1. Xác nhận outage (đợi alert từ health_checker hoặc xác nhận outage đủ ngưỡng)
    health_log = pathlib.Path("reports/health-events.jsonl")
    detected = False
    p_reason = "unknown"
    t_start_step1 = time.time()
    while time.time() - t_start_step1 < 30.0:
        if health_log.exists():
            for line in health_log.read_text().splitlines():
                if line.strip():
                    try:
                        ev = json.loads(line)
                        if (
                            ev.get("event") == "state_change"
                            and ev.get("region") == primary
                            and ev.get("to") == "UNHEALTHY"
                            and ev.get("ts", 0) >= (t_outage or 0)
                        ):
                            detected = True
                            p_reason = ev.get("reason", "UNHEALTHY")
                            break
                    except Exception:
                        pass
        if detected:
            break
        p_ok, p_reason = hc.probe(primary, timeout=1.5)
        if not p_ok and t_outage and (time.time() - t_outage >= 16.0):
            detected = True
            break
        time.sleep(1.0)

    t_ok, t_reason = hc.probe(target, timeout=1.5)
    step(
        1,
        "xac_nhan_outage",
        primary=primary,
        primary_ready=not detected,
        primary_reason=p_reason,
        target=target,
        target_ready=t_ok,
        target_reason=t_reason,
    )

    # 2. Thông báo incident
    t_now = time.time()
    operator_delay_s = round(t_now - t_outage, 2) if t_outage else None
    step(
        2,
        "thong_bao_incident",
        primary=primary,
        target=target,
        t_outage=t_outage,
        operator_ts=t_now,
        delay_s=operator_delay_s,
    )

    if not confirm(auto, f"Xác nhận kích hoạt failover từ region-{primary} sang region-{target}?"):
        step(2, "thong_bao_incident_aborted", reason="operator_cancelled")
        return {"ok": False, "aborted": True}

    # 3. Scale GPU pool (gọi failover duy nhất 1 lần)
    fo_res = fo.failover(target=target, backend=backend, wait=60.0)
    step(3, "scale_gpu_pool", target=target, ok=fo_res.get("ok", False), result=fo_res)
    if not fo_res.get("ok"):
        return {"ok": False, "step": 3, "error": "failover_failed", "detail": fo_res}

    # 4. Verify state replica
    st = fo.state_of(target)
    step(
        4,
        "verify_state_replica",
        target=target,
        rpo=fo_res.get("rpo"),
        embed_model_version=fo_res.get("embed_model_version"),
        vector_count=st.get("count"),
        weights_ok=st.get("weights"),
    )

    # 5. DNS cutover
    active_p = pathlib.Path("edge/active_region")
    current_active = active_p.read_text().strip() if active_p.exists() else None
    step(5, "dns_cutover", target=target, active_region=current_active, ok=(current_active == target))

    # 6. Verify golden signals (10 requests)
    latencies = []
    errors = 0
    with httpx.Client(timeout=3.0) as client:
        for i in range(10):
            t_req = time.time()
            try:
                resp = client.get(f"{URL[target]}/v1/infer", params={"q": f"golden signal test {i}"})
                if resp.status_code != 200:
                    errors += 1
            except Exception:
                errors += 1
            lat_ms = (time.time() - t_req) * 1000.0
            latencies.append(lat_ms)
            time.sleep(0.05)

    sorted_lats = sorted(latencies)
    p95 = round(sorted_lats[int(0.95 * len(sorted_lats))], 1) if sorted_lats else None
    err_rate = round(errors / len(latencies), 2) if latencies else 1.0
    step(
        6,
        "verify_golden_signals",
        target=target,
        samples=len(latencies),
        p95_latency_ms=p95,
        error_rate=err_rate,
    )

    # 7. Post incident
    elapsed_s = round(time.time() - t_now, 2)
    measure_cmd = "python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300"
    step(7, "post_incident", elapsed_s=elapsed_s, measure_cmd=measure_cmd)

    return {
        "ok": True,
        "target": target,
        "elapsed_s": elapsed_s,
        "failover": fo_res,
        "golden_signals": {"p95_latency_ms": p95, "error_rate": err_rate},
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
