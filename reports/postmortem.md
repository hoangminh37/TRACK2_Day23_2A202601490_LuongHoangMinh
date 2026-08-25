# Postmortem — DR Drill Lab 23

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là "hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

## 1. Timeline (mọi dòng phải có evidence path:line)

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 2026-08-25T09:39:38 | Outage bắt đầu (netblock Region A) | `chaos/chaos-events.jsonl:2` |
| 2026-08-25T09:39:38 | User đầu tiên bị ảnh hưởng (request fail) | `reports/drill-2-withdr.jsonl:25` |
| 2026-08-25T09:39:53 | Health check alert (Region A UNHEALTHY) | `reports/health-events.jsonl:2` |
| 2026-08-25T09:39:53 | Operator confirm cutover & bắt đầu restore | `reports/failover-events.jsonl:1` |
| 2026-08-25T09:39:53 | Snapshot restore xong | `reports/failover-events.jsonl:2` |
| 2026-08-25T09:39:59 | Region B GPU pool warm-up hoàn tất & ready | `reports/failover-events.jsonl:4` |
| 2026-08-25T09:39:59 | DNS/LB cutover sang Region B | `reports/failover-events.jsonl:5` |
| 2026-08-25T09:40:00 | Resolved (request đầu tiên OK từ Region B) | `reports/drill-2-withdr.jsonl:36` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: `22.2s` · gap: `-277.8s` (Đạt và vượt mục tiêu)
- RPO mục tiêu: 300s · đo được: `4.01s` (`2` doc bị mất) · gap: `-295.99s` (Đạt và vượt mục tiêu)
- **Bước tốn nhiều giây nhất:** `Health-check detect floor (15.0s)` — vì hệ thống cấu hình `interval=5s` và `threshold=3` liên tiếp để chống flapping, dẫn đến thời gian tối thiểu để xác nhận outage là $5 \times 3 = 15.0s$ (~67.5% tổng RTO).

## 3. Root cause (5 whys)

1. **Why 1:** Tại sao người dùng không thể thực hiện inference? → Region A bị cô lập mạng hoàn toàn (netblock / dropped packet simulation).
2. **Why 2:** Tại sao traffic không chuyển sang Region B ngay lập tức? → Vì Region B là Standby region (Passive), khởi đầu rỗng dữ liệu và model weights, chưa được scale full.
3. **Why 3:** Tại sao mất 15s hệ thống mới bắt đầu quy trình failover? → Health-checker cần 3 lần thăm dò thất bại liên tiếp với chu kỳ 5s để đảm bảo không bị false-alarm.
4. **Why 4:** Tại sao sau khi restore dữ liệu mất thêm hơn 6s mới cutover? → GPU pool ở Region B cần 6.0s warm-up để khởi tạo model context vào bộ nhớ trước khi sẵn sàng nhận tải.
5. **Why 5:** *Nếu đây là outage thật, bước nào trong runbook có nguy cơ thất bại nhất?* → Bước `2_restore_snapshot` nếu object store chứa snapshot bị nghẽn băng thông xuyên vùng (cross-region network bottleneck) hoặc lệch version embedding model giữa backup và runtime serving.

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Áp dụng cơ chế Change Data Capture (CDC) / Streaming Replication cho Vector DB thay vì batch snapshot 30s | Data Platform Lead | 2026-09-30 | Giảm RPO từ 4.01s xuống < 0.5s |
| 2 | Duy trì GPU pool ở trạng thái Warm Standby (đã load sẵn model weights vào GPU VRAM ở Region B) | MLOps Infra Lead | 2026-09-30 | Giảm RTO thêm ~6.2s (loại bỏ thời gian warm-up) |
| 3 | Tối ưu DNS Failover bằng Anycast routing / Global Server Load Balancing (GSLB) với TTL = 1s | Network SRE Lead | 2026-10-15 | Giảm RTO thêm ~0.8s (DNS cache convergence) |

## 5. Ba câu hỏi bắt buộc trả lời

1. **`interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?**
   - Giá trị: $5.0s \times 3 = 15.0s$.
   - Tỷ trọng: Chiếm $\frac{15.0}{22.2} \approx 67.57\%$ tổng thời gian phục hồi (RTO).
2. **Nếu hạ interval xuống 1s, RTO giảm mấy giây — và bạn trả giá gì (§4 flapping)?**
   - RTO sẽ giảm $4s \times 3 = 12.0s$ (tổng RTO giảm từ 22.2s xuống còn ~10.2s).
   - **Cái giá phải trả (Trade-off):** Tăng đột biến nguy cơ **Flapping (False-Positive Failover)** khi mạng chỉ bị nghẽn tạm thời trong vài giây hoặc jitter mạng. Khi đó hệ thống sẽ kích hoạt failover không cần thiết, làm gián đoạn kết nối của người dùng và gây tốn kém chi phí scale/warm-up GPU 2 chiều liên tục.
3. **Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` của bạn có nghĩa gì với khách hàng?**
   - Con số `docs_lost = 2` tương ứng với 2 tài liệu / truy vấn người dùng được ingest trong khoảng 4.01s trước thời điểm outage nhưng chưa kịp snapshot lên replica object store.
   - Đối với khách hàng, điều này đồng nghĩa với 2 giao dịch/ticket vừa tạo sẽ bị mất nếu không có cơ chế log replay từ Message Queue (Kafka/EventStream). Do đó, trong kiến trúc DR thực tế cần kết hợp Vector DB Snapshot với Write-Ahead Logging (WAL) / Event Sourcing để đạt RPO $\approx 0$.
