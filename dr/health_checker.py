"""BƯỚC 3a — SINH VIÊN VIẾT. Health checker cho 2 region.

Yêu cầu (đọc §4 "Kiến Trúc Health-Check-Based Failover" + §2 "DNS Failover"):
  1. Poll /readyz của CẢ HAI region mỗi `interval` giây (mặc định 5s).
     Dùng /readyz, KHÔNG dùng /healthz. /healthz chỉ nói "process còn sống" —
     region có process sống nhưng vector DB rỗng thì vẫn không serve được.
  2. Chỉ đổi trạng thái sau `threshold` lần fail LIÊN TIẾP (mặc định 3).
     Một lần fail không phải outage. Đây là chống flapping (§4 Anti-Patterns).
  3. Ghi 1 dòng JSONL MỖI LẦN ĐỔI TRẠNG THÁI (không ghi mỗi lần poll — log sẽ ngập).
     Dòng bắt buộc có: ts, region, to (HEALTHY|UNHEALTHY), reason,
     interval_s, threshold. Thiếu interval_s/threshold thì tools/measure_rto.py
     không tính được detect floor -> mất điểm.

Chạy:  python dr/health_checker.py --interval 5 --threshold 3 --duration 300 \
              --out reports/health-events.jsonl

CÂU HỎI PHẢI TRẢ LỜI TRƯỚC KHI VIẾT (ghi câu trả lời vào reports/postmortem.md):
  interval=5s, threshold=3 -> sớm nhất bạn có thể phát hiện outage là bao nhiêu giây?
  Con số đó nằm TRONG RTO của bạn. Muốn RTO 5 phút thì được phép chọn interval bao nhiêu?
"""
import argparse
import json
import pathlib
import time

import httpx

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def probe(region: str, timeout: float) -> tuple[bool, str]:
    """Trả về (ready, reason). Timeout PHẢI có — netblock làm request treo mãi."""
    url = f"{URL.get(region, f'http://127.0.0.1:8001')}/readyz"
    try:
        r = httpx.get(url, timeout=timeout)
        if r.status_code == 200:
            return True, "ready"
        try:
            body = r.json()
            reasons = body.get("reasons", [f"status_{r.status_code}"])
            reason_str = ",".join(reasons) if isinstance(reasons, list) else str(reasons)
        except Exception:
            reason_str = f"status_{r.status_code}"
        return False, reason_str
    except Exception as e:
        return False, type(e).__name__


def run(interval: float, timeout: float, threshold: int, duration: float, out: pathlib.Path):
    """Vòng lặp poll + phát hiện transition + ghi JSONL."""
    out.parent.mkdir(parents=True, exist_ok=True)
    status = {
        "a": {"state": "HEALTHY", "fails": 0},
        "b": {"state": "HEALTHY", "fails": 0},
    }
    end_time = time.time() + duration
    while time.time() < end_time:
        t0 = time.time()
        for region in ("a", "b"):
            ready, reason = probe(region, timeout)
            if ready:
                if status[region]["state"] == "UNHEALTHY":
                    status[region]["state"] = "HEALTHY"
                    status[region]["fails"] = 0
                    rec = {
                        "event": "state_change",
                        "ts": time.time(),
                        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                        "region": region,
                        "from": "UNHEALTHY",
                        "to": "HEALTHY",
                        "reason": reason,
                        "interval_s": interval,
                        "threshold": threshold,
                        "consecutive_fails": 0,
                    }
                    with out.open("a") as f:
                        f.write(json.dumps(rec) + "\n")
                    print("HEALTH", json.dumps(rec))
                else:
                    status[region]["fails"] = 0
            else:
                status[region]["fails"] += 1
                if (
                    status[region]["fails"] >= threshold
                    and status[region]["state"] != "UNHEALTHY"
                ):
                    status[region]["state"] = "UNHEALTHY"
                    rec = {
                        "event": "state_change",
                        "ts": time.time(),
                        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                        "region": region,
                        "from": "HEALTHY",
                        "to": "UNHEALTHY",
                        "reason": reason,
                        "interval_s": interval,
                        "threshold": threshold,
                        "consecutive_fails": status[region]["fails"],
                    }
                    with out.open("a") as f:
                        f.write(json.dumps(rec) + "\n")
                    print("HEALTH", json.dumps(rec))
        elapsed = time.time() - t0
        time.sleep(max(0.0, interval - elapsed))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--out", default="reports/health-events.jsonl")
    a = p.parse_args()
    run(a.interval, a.timeout, a.threshold, a.duration, pathlib.Path(a.out))
