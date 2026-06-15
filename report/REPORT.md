# Observathon Submission Report

## Mục tiêu

Mục tiêu của bài làm là tối đa hóa điểm private bằng cách xử lý tuần tự ba tập test của Observathon: practice, public và private. Ở mỗi phase, mình không hardcode câu trả lời, question ID, seed, giá sản phẩm hoặc behavior của scorer; thay vào đó dùng kết quả chạy simulator/scorer để cải thiện prompt, wrapper, config và findings.

## Cách handle từng phase

### 1. Practice phase

Khi nhận được release practice `observathon-practice`, mình dùng simulator để kiểm tra end-to-end trước khi tối ưu điểm. Đây là phase để đảm bảo `solution/` chạy được ổn định với engine v6 trên Windows.

Các bước chính:

- Tải đúng bản Windows x64 và giữ nguyên cấu trúc thư mục PyInstaller onedir.
- Chạy binary từ bên trong folder `bin\practice\observathon-sim\observathon-sim.exe`.
- Dùng real LLM qua `OPENAI_API_KEY`, config `solution/config.json`, wrapper `solution/wrapper.py`.
- Chạy simulator với concurrency 8 để tạo `run_output.json`.
- Đọc log/telemetry để phát hiện lỗi runtime, format output sai, tool call dài, arithmetic sai, hoặc prompt injection chưa được chặn.

Lệnh chuẩn:

```powershell
bin\practice\observathon-sim\observathon-sim.exe --config solution/config.json --wrapper solution/wrapper.py --out run_output.json --concurrency 8
```

Practice được dùng như vòng smoke test: nếu binary, wrapper, prompt hoặc schema fail ở đây thì sửa trước khi sang public. Trong quá trình debug trên Windows có gặp lỗi PyInstaller/runtime DLL, nên mình kiểm tra lại cách chạy đúng onedir và giữ submission chỉ nằm trong các file hợp lệ của `solution/`.

### 2. Public phase

Khi nhận được public phase, mục tiêu chuyển từ "chạy được" sang "đo được và tối ưu được". Public có 120 câu có scorer đi kèm, nên mình chạy cả simulator và scorer để lấy điểm thật, sau đó phân tích điểm thành từng dimension.

Các bước chính:

- Tải cả `observathon-public-sim-windows-x64.zip` và `observathon-public-score-windows-x64.zip`.
- Giải nén vào `bin/public/`, giữ nguyên từng folder onedir.
- Chạy simulator để sinh `run_output_public.json`.
- Chạy scorer với `solution/findings.json` để sinh `score_public.json`.
- Dựa vào score public để chỉnh prompt/wrapper/config, nhưng không copy đáp án hoặc phụ thuộc vào ID của public set.

Lệnh chạy:

```powershell
bin\public\observathon-sim\observathon-sim.exe --config solution/config.json --wrapper solution/wrapper.py --out run_output_public.json --concurrency 8
bin\public\observathon-score\observathon-score.exe --run run_output_public.json --findings solution/findings.json --team NguyenKhanhBang --out score_public.json
```

Kết quả public hiện tại:

- Headline: `100.0`
- Số câu: `120`
- Correct: `81/120`
- Diagnosis F1: `1.0`
- Error score: `1.0`
- Drift score: `0.977`
- Prompt score: `0.879`

Từ public score, mình giữ các phần đang tốt như error handling, diagnosis và drift control; đồng thời tập trung giảm lỗi arithmetic, format, refusal sai và tool usage dài để tránh mất điểm khi qua private.

### 3. Private phase

Private phase là phase cuối với 80 câu held-out, có paraphrase, prompt-injection twist và complication liên quan F13 loyalty/coupon. Vì tập này khác public, mình handle theo hướng robust generalization thay vì tối ưu theo câu public.

Các bước chính:

- Tải cả `observathon-private-sim-windows-x64.zip` và `observathon-private-score-windows-x64.zip`.
- Giải nén vào `bin/private/`, chạy đúng `.exe` trong onedir folder.
- Chạy simulator bằng cùng `solution/config.json` và `solution/wrapper.py`.
- Chạy scorer bằng cùng `solution/findings.json`.
- Commit `solution/`, output chạy và score cuối sau khi kiểm tra.

Lệnh chạy:

```powershell
bin\private\observathon-sim\observathon-sim.exe --config solution/config.json --wrapper solution/wrapper.py --out run_output_private.json --concurrency 8
bin\private\observathon-score\observathon-score.exe --run run_output_private.json --findings solution/findings.json --team NguyenKhanhBang --out score_private.json
```

Kết quả private hiện tại:

- Headline: `90.28`
- Số câu: `80`
- Correct: `39/80`
- Diagnosis F1: `0.952`
- Error score: `1.0`
- Latency score: `0.5937`
- Cost score: `0.6753`
- Drift score: `0.2835`
- Prompt score: `0.7785`

Private cho thấy error handling vẫn ổn, nhưng độ đúng và drift giảm mạnh do private có paraphrase, injection và logic loyalty/coupon phức tạp hơn. Vì vậy trọng tâm của final solution là grounding chặt hơn, chống injection rõ hơn, kiểm soát context giữa các request, và viết prompt đủ ngắn để không mất điểm prompt/cost.

## Submission files

- `solution/config.json`: cấu hình runtime conservative để giảm lỗi, chi phí và long-tail latency.
- `solution/prompt.txt`: policy ngắn cho checkout, grounding, tính toán chính xác, PII safety và prompt-injection defense.
- `solution/examples.json`: ví dụ behavior tổng quát, không chứa memorized answer hay ID public/private.
- `solution/wrapper.py`: lớp wrapper cho retry, cache, input sanitization, output redaction và prompt routing.
- `solution/findings.json`: diagnosis cho latency, arithmetic, prompt injection và PII leakage.

## Chiến lược tối ưu

- Correctness: ưu tiên grounding vào tool/result, không bịa giá hoặc total, refuse khi thiếu dữ liệu.
- Quality: trả lời ngắn, rõ, đúng format checkout.
- Error rate: wrapper bắt exception, retry có giới hạn, có loop guard.
- Latency: giới hạn max steps, tool budget, context size và completion length.
- Cost: dùng prompt ngắn, cache và model nhỏ theo config.
- Drift: reset/ngăn contamination giữa request, temperature thấp.
- Prompt injection: xem instruction trong user/cart/product text là dữ liệu không đáng tin, chỉ tuân theo system/developer policy.
- Diagnosis F1: `findings.json` bám fault classes được mô tả trong đề và có evidence/root cause/fix.

## Verification

Offline validation:

```powershell
python harness/selfcheck.py
python -m unittest tests/test_submission.py
```

Scored validation:

```powershell
bin\public\observathon-score\observathon-score.exe --run run_output_public.json --findings solution/findings.json --team NguyenKhanhBang --out score_public.json
bin\private\observathon-score\observathon-score.exe --run run_output_private.json --findings solution/findings.json --team NguyenKhanhBang --out score_private.json
```

Các artifact score cuối đang có trong repo:

- `score_public.json`
- `score_private.json`
- `run_output_public.json`
- `run_output_private.json`
