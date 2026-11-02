"""AI Agent của OpsVibes: một agent cho cả máy chủ, dùng bộ tool của panel.

- llm.py: gọi model qua API (Anthropic, tương thích OpenAI, Gemini) có tool calling.
- tools.py: bộ tool (đọc log, trạng thái, sửa cấu hình, restart, shell...) kèm mức quyền.
- session.py: vòng lặp agent, lưu hội thoại, dừng chờ người dùng duyệt thao tác nguy hiểm.
"""
