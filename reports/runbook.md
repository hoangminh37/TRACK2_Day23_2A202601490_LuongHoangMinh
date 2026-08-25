# Runbook 1 trang — Region chính down

Runbook phục vụ quy trình xử lý sự cố khi Region chính (Region A) gặp sự cố ngừng hoạt động (outage). Được thiết kế để kỹ sư trực vận hành (On-call Engineer) có thể thực hiện nhanh chóng lúc 3h sáng với các lệnh copy-paste được và dấu hiệu xác nhận hoàn thành rõ ràng.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python3 -c "import httpx; print(httpx.get('http://127.0.0.1:8001/readyz', timeout=1.5).status_code)"` | HTTP response trả về mã != 200 hoặc Timeout/Connection error $\ge 3$ lần | on-call |
| 2 | Mở incident + bấm giờ RTO | `python3 -c "import time, json; print(json.dumps({'event': 'incident_open', 'ts': time.time()}))" >> reports/runbook-run.jsonl` | Timestamp được ghi vào `reports/runbook-run.jsonl`, thông báo cho Incident Commander | on-call |
| 3 | Restore state ở region phụ | `python3 state/snapshot.py get --region b --backend fs` | File `state/region-b/vectors.sqlite` và model weights được restore, manifest trả về `embed_model_version` | on-call |
| 4 | Scale pool warm→full | `echo full > state/region-b/pool_state && curl -s http://127.0.0.1:8002/readyz` | Endpoint `/readyz` của Region B trả về HTTP status 200 OK (sau khi hoàn tất GPU warm-up) | on-call |
| 5 | DNS/LB cutover | `printf b > edge/active_region` | `curl -s http://127.0.0.1:8080/edge/state` trả về `"active_region":"b"` | on-call |
| 6 | Verify golden signals | `for i in {1..10}; do curl -s "http://127.0.0.1:8080/v1/infer?q=test"; echo; done` | p95 latency < 50ms, error rate = 0%, upstream trả về `[b] ...` | on-call |
| 7 | Đo RTO + postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | Output trả về `valid: true`, `rto_verdict: "PASS"`, `rto_measured_s <= 300` | on-call |

> **Automation Script:** Toàn bộ 7 bước trên có thể được thực thi tự động qua câu lệnh:
> ```bash
> python3 dr/runbook.py --primary a --target b --backend fs --auto
> ```

---

## Điều kiện Rollback (Failover ngược về Region A)
1. **Điều kiện kỹ thuật:**
   - Region A đã khôi phục hoạt động bình thường, endpoint `/healthz` và `/readyz` của Region A trả về 200 OK ổn định liên tục ít nhất **15 phút** (không có flapping).
   - Dữ liệu phát sinh tại Region B trong thời gian sự cố đã được đồng bộ ngược (reverse replication) về Region A để tránh data loss / split-brain.
2. **Thẩm quyền quyết định:**
   - **Chỉ Incident Commander (hoặc Lead SRE)** có quyền phê duyệt rollback sau khi kiểm tra trạng thái sức khỏe của Region A và tính toàn vẹn dữ liệu.
   - **Tuyệt đối không tự động rollback (No auto-rollback):** Tránh hiện tượng flap hai chiều (oscillating failover) làm gia tăng RTO và gián đoạn dịch vụ người dùng.
