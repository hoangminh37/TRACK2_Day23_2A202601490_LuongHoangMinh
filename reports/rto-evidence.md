# RTO/RPO Evidence — Lab 23

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). `pytest tests/test_rto_evidence.py` sẽ mở từng file ra kiểm tra.
Con số không có evidence = trượt, bất kể các phần khác.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-08-25T09:39:02` | chaos kill | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | `+0.0s` | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:11` |
| Request thành công sau đó | không có | không có dòng `ok:true` nào sau t_outage | `reports/measure-drill-1.json:1` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py` | `reports/measure-drill-1.json:1` |

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0s | `action:kill` | `chaos/chaos-events.jsonl:2` |
| User thấy lỗi đầu tiên | +0.1s | dòng `ok:false` đầu | `reports/drill-2-withdr.jsonl:25` |
| Health check phát hiện | +15.0s | `to:UNHEALTHY, region:a` | `reports/health-events.jsonl:2` |
| Snapshot restore xong | +15.2s | `step:2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region phụ ready | +21.4s | `step:4_wait_ready` | `reports/failover-events.jsonl:4` |
| DNS cutover | +21.4s | `step:5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | 22.2s | dòng `ok:true` đầu sau lỗi | `reports/drill-2-withdr.jsonl:36` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | `22.2s` | 300s (5 phút) | PASS |
| RPO — Vector DB | `4.01s` / `2` doc | 300s (5 phút) | PASS |

## 3. RTO của tôi gồm những gì (bắt buộc — đây là phần chấm điểm hiểu bài)

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | 15.0s | `interval_s × threshold` trong `reports/health-events.jsonl:2` | Giảm `interval` xuống 2s hoặc `threshold` xuống 2 (đổi lại tăng nguy cơ false-positive flap) |
| Snapshot restore | 0.2s | 2_restore → 3_scale trong `reports/failover-events.jsonl:2` | Tối ưu I/O đĩa, incremental snapshot thay vì full copy |
| GPU pool warm-up | 6.2s | `waited_s` ở `4_wait_ready` trong `reports/failover-events.jsonl:4` | Duy trì pool ở chế độ warm với model đã load sẵn trong VRAM |
| DNS/LB TTL cache | 0.8s | t_recovered − t_cutover trong `reports/drill-2-withdr.jsonl:36` | Giảm TTL của DNS hoặc dùng Anycast Global Load Balancer hỗ trợ instant cutover |
