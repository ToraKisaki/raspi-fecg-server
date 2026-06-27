# Sơ đồ ERD — fECG Server

Sơ đồ quan hệ thực thể (Entity Relationship Diagram) của cơ sở dữ liệu SQLite trong `database.py`.

## Mô hình tổng quát

Chỉ **nhân viên y tế** mới là tài khoản đăng nhập: mỗi nhân viên là 1 dòng trong `users`
(thông tin xác thực) + 1 dòng `staff_profile` (thông tin nghiệp vụ). **Bệnh nhân** là *đối tượng
theo dõi*, không đăng nhập, nên giữ bảng riêng `patients` với khóa chính là chuỗi tự nhiên (vd "P001").

## 8 thực thể (bảng)

| Bảng | Khóa chính | Vai trò |
|------|-----------|---------|
| `users` | `id` | Tài khoản đăng nhập (auth): phone/email/password/role/is_active |
| `staff_profile` | `id` | Hồ sơ nhân viên (họ tên, chuyên khoa, học vị) — 1-1 với `users` |
| `user_login` | `token` | Phiên đăng nhập web (cookie token) |
| `patients` | `id` (TEXT) | Hồ sơ bệnh nhân (thai phụ) — không có tài khoản |
| `sessions` | `id` | Mỗi lần đo / kết nối thiết bị |
| `samples` | `id` | Waveform thô + fECG (đã downsample) |
| `metrics` | `id` | Trend FHR/MHR/SQ/alarm (~1 Hz) |
| `events` | `id` | Báo động / mốc phiên / ghi chú |

## Quan hệ & lực lượng (cardinality)

- `users` **1 — 1** `staff_profile` (mỗi tài khoản nhân viên có đúng một hồ sơ)
- `users` **1 — N** `user_login` (một tài khoản có nhiều token đăng nhập)
- `patients` **1 — N** `sessions` (một bệnh nhân có nhiều phiên đo)
- `sessions` **1 — N** `samples`
- `sessions` **1 — N** `metrics`
- `sessions` **1 — N** `events`
- `patients` **1 — N** `events` (events lưu **cả** `patient_id` lẫn `session_id`)

## Sơ đồ ERD (Mermaid)

```mermaid
erDiagram
    users ||--|| staff_profile : "hồ sơ"
    users ||--o{ user_login : "đăng nhập"
    patients ||--o{ sessions : "có"
    sessions ||--o{ samples : "ghi"
    sessions ||--o{ metrics : "tính"
    sessions ||--o{ events : "phát sinh"
    patients ||--o{ events : "thuộc về"

    users {
        int id PK
        text phone_number UK
        text email UK
        text password "PBKDF2 hash (salt$hash)"
        int role "1=clinician"
        int is_active "0/1"
        real created_at
        real updated_at
    }
    staff_profile {
        int id PK
        int user_id FK "UNIQUE"
        text full_name
        text specialization
        text degree
    }
    user_login {
        text token PK
        int user_id FK
        real created_at
    }
    patients {
        text id PK
        text full_name
        text gender
        text date_of_birth
        text mrn
        text citizen_id
        text address
        text notes
        real created_at
        int archived
    }
    sessions {
        int id PK
        text patient_id FK
        real started_at
        real ended_at
        int sample_rate
    }
    samples {
        int id PK
        int session_id FK
        real t
        real raw
        real fecg
    }
    metrics {
        int id PK
        int session_id FK
        real t
        real fhr
        real mhr
        int sq
        text alarm
    }
    events {
        int id PK
        int session_id FK
        text patient_id FK
        real t
        text kind
        text label
    }
```

## Ghi chú quan trọng

1. **Không có FOREIGN KEY thật trong DB.** Các quan hệ trên là *logic* (cột `*_id` trỏ tới bảng cha),
   nhưng schema không khai báo `FOREIGN KEY ... REFERENCES`. Khi nộp báo cáo nên ghi
   "ràng buộc khóa ngoại ở mức ứng dụng" để chính xác.

2. **Đăng nhập bằng phone HOẶC email.** `users.phone_number` và `users.email` đều UNIQUE;
   `get_user_by_login(identifier)` so khớp với một trong hai, và chỉ chấp nhận `is_active=1`.

3. **`role` để dành cho RBAC sau.** Hiện mọi tài khoản active có quyền như nhau (chưa gate
   endpoint theo role); cột `role` chỉ phân loại sẵn (mặc định 1 = clinician).

4. **Bệnh nhân dùng khóa tự nhiên TEXT.** `patients.id` (vd "P001") là khóa load-bearing:
   xuất hiện trong URL `/patient/{id}`, WebSocket `/ws/live/{id}`, key pub/sub, tên file CSV,
   và `sessions.patient_id`. Không đổi sang surrogate int.
